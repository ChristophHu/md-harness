"""Tests for the engine orchestration boundary."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.config import ConfigError, PersistenceMode
from harness.engine.orchestrator import Orchestrator
from harness.engine.result import EngineResult, NextAction, ResultStatus
from harness.storage.transaction import TransactionError, TransactionManager


class StubContextBuilder:
    def __init__(self, context):
        self.context = context
        self.task_ids = []

    def build(self, task_id):
        self.task_ids.append(task_id)
        return self.context


def test_orchestrator_builds_context_and_waits_for_engine_components():
    context = object()
    builder = StubContextBuilder(context)
    orchestrator = Orchestrator(builder)

    assert orchestrator.build_context(7) is context
    result = orchestrator.run(8)

    assert builder.task_ids == [7, 8]
    assert result.status is ResultStatus.WAITING


@pytest.mark.parametrize("stage", ["planning", "execution", "validation"])
def test_orchestrator_stops_without_persisting_when_stage_loses_ownership(stage):
    class Planner:
        def plan(self, _context):
            if stage == "planning":
                raise TransactionError("lost")
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, *_args):
            if stage == "execution":
                raise TransactionError("lost")
            return EngineResult.success("executed", execution="execution")

    class Validator:
        def validate(self, *_args):
            raise TransactionError("lost")

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(1)
    assert result.status is ResultStatus.WAITING
    assert "ownership was lost" in result.message


def test_record_replan_uses_fenced_transaction_when_available():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE replans (task_id INTEGER, version INTEGER)")

    class Tasks:
        def __init__(self, connection):
            self.connection = connection

        def record_replan(self, task_id, _reason, version, **_kwargs):
            self.connection.execute(
                "INSERT INTO replans VALUES (?, ?)", (task_id, version)
            )

    tasks = Tasks(connection)
    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=tasks,
        transaction_manager=TransactionManager(connection, tasks),
    )
    orchestrator._record_replan(1, SimpleNamespace(version=2), None, "retry")
    assert connection.execute("SELECT * FROM replans").fetchone() == (1, 3)


def test_resume_returns_waiting_when_claim_is_revoked_during_dispatch():
    class Checkpoint:
        def get(self, _task_id):
            return {"next_action": "wait", "reason": "external information"}

        def claim_resume_lease(self, _task_id, **kwargs):
            return kwargs["token"]

        def release_resume_lease(self, *_args):
            return True

    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=Checkpoint()
    )

    def revoke(*_args):
        raise TransactionError("revoked")

    orchestrator._resume_wait = revoke
    result = orchestrator.resume(1)
    assert result.status is ResultStatus.WAITING
    assert "ownership was lost" in result.message


def test_resume_reports_cancellation_when_claim_is_revoked_during_dispatch():
    state = {"status": "waiting"}

    class TaskStore:
        def get(self, _task_id):
            return state

    class Checkpoint:
        def get(self, _task_id):
            return {"next_action": "wait", "reason": "external information"}

        def claim_resume_lease(self, _task_id, **kwargs):
            return kwargs["token"]

        def release_resume_lease(self, *_args):
            return True

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=TaskStore(),
        checkpoint_store=Checkpoint(),
    )

    def revoke(*_args):
        state["status"] = "cancelled"
        raise TransactionError("revoked")

    orchestrator._resume_wait = revoke
    result = orchestrator.resume(1)
    assert result.status is ResultStatus.FAILED
    assert "cancelled" in result.message


def test_resumed_validation_propagates_claim_revocation():
    from harness.engine.plan import ExecutionPlan

    class Validator:
        def validate(self, *_args):
            raise TransactionError("revoked")

    orchestrator = Orchestrator(StubContextBuilder(object()), validator=Validator())
    checkpoint = {
        "context_data": {"execution": {"status": "success", "steps": []}},
    }
    with pytest.raises(TransactionError, match="revoked"):
        orchestrator._resume_validation(1, checkpoint, ExecutionPlan("goal"))


def test_orchestrator_passes_context_through_all_stages():
    context = object()
    builder = StubContextBuilder(context)
    calls = []

    class Planner:
        def plan(self, received):
            calls.append(("plan", received))
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, received, plan):
            calls.append(("execute", received, plan))
            return EngineResult.success("executed", execution="execution")

    class Validator:
        def validate(self, received, plan, execution):
            calls.append(("validate", received, plan, execution))
            return EngineResult.success("valid")

    result = Orchestrator(
        builder,
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(3)

    assert result.successful is True
    assert calls == [
        ("plan", context),
        ("execute", context, "plan"),
        ("validate", context, "plan", "execution"),
    ]


def test_orchestrator_routes_explicit_validate_action_to_validator():
    calls = []

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed",
                execution=ExecutionResult(
                    ExecutionStatus.SUCCESS, next_action="validate"
                ),
            )

    class Validator:
        def validate(self, *_args):
            calls.append(True)
            return EngineResult.success("validated")

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(21)

    assert result.status is ResultStatus.SUCCESS
    assert calls == [True]


def test_orchestrator_returns_planning_failure_without_execution():
    context = object()

    class Planner:
        def plan(self, received):
            assert received is context
            return EngineResult.failure("invalid context")

    class UnexpectedStage:
        def execute(self, *_args):
            raise AssertionError("executor must not run")

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=UnexpectedStage(),
        validator=UnexpectedStage(),
    ).run(4)

    assert result.status is ResultStatus.FAILED


@pytest.mark.parametrize("error", [RuntimeError("broken"), TimeoutError("slow")])
def test_orchestrator_persists_unexpected_planner_failure_and_recovery(error):
    events = []

    class Events:
        def record(self, task_id, event_type, payload=None):
            events.append((task_id, event_type, payload))

    class Planner:
        def plan(self, _context):
            raise error

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=object(),
        validator=object(),
        event_store=Events(),
    ).run(44)

    assert result.status is ResultStatus.FAILED
    assert result.errors == [type(error).__name__]
    failure = result.data["failure"]
    assert failure.error_class == type(error).__name__
    assert failure.retryable is isinstance(error, TimeoutError)
    assert any(event[1] == "task.planning.exception" for event in events)


def test_orchestrator_returns_executor_failure_without_validation():
    context = object()

    class Planner:
        def plan(self, _received):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _received, _plan):
            return EngineResult.failure("execution failed")

    class UnexpectedValidator:
        def validate(self, *_args):
            raise AssertionError("validator must not run")

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=Executor(),
        validator=UnexpectedValidator(),
    ).run(5)

    assert result.status is ResultStatus.FAILED


def test_orchestrator_waits_after_validation_and_persists_transitions():
    context = object()
    transitions = []
    events = []

    class Store:
        def transition(self, task_id, status):
            transitions.append((task_id, status))

    class Events:
        def record(self, task_id, event_type, payload=None):
            events.append((task_id, event_type))

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.WAITING,
                data={"next_action": "wait"},
                wait_reason="approval",
            )

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=Store(),
        event_store=Events(),
    ).run(6)

    assert result.status is ResultStatus.WAITING
    assert transitions == [
        (6, "planning"),
        (6, "executing"),
        (6, "validating"),
        (6, "waiting"),
    ]
    assert events[-1] == (6, "task.waiting")


def test_orchestrator_replans_until_success_and_marks_done():
    context = object()
    transitions = []
    plan_calls = []
    validation_calls = []

    class Store:
        def transition(self, _task_id, status):
            transitions.append(status)

    class Planner:
        def plan(self, _context):
            plan_calls.append(True)
            return EngineResult.success("planned", plan=f"plan-{len(plan_calls)}")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            validation_calls.append(True)
            if len(validation_calls) == 1:
                return EngineResult(ResultStatus.FAILED, data={"next_action": "replan"})
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "stop"})

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=Store(),
    ).run(7)

    assert result.status is ResultStatus.SUCCESS
    assert len(plan_calls) == 2
    assert transitions[-1] == "done"


def test_orchestrator_stops_after_replan_limit():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "replan",
                execution=ExecutionResult(
                    ExecutionStatus.SUCCESS, next_action="replan"
                ),
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
        max_replans=0,
    ).run(70)
    assert result.message == "Maximum replans exceeded."


def test_orchestrator_serializes_dataclasses_and_replanning_feedback():
    from dataclasses import dataclass

    from harness.engine.validator import ValidationResult

    @dataclass
    class Value:
        value: int

    assert Orchestrator._serialize(Value(1)) == {"value": 1}
    assert Orchestrator._serialize("plain") == "plain"

    payload = Orchestrator._replanning_payload(
        Value(2),
        EngineResult(
            ResultStatus.FAILED,
            message="needs changes",
            data={"validation": ValidationResult()},
        ),
    )
    assert payload["previous_plan_version"] is None
    assert payload["next_plan_version"] == 1


def test_orchestrator_can_construct_executor_from_registry():
    context = object()
    registry = object()
    orchestrator = Orchestrator(StubContextBuilder(context), tool_registry=registry)

    assert orchestrator.executor is not None
    assert orchestrator.executor.tool_registry is registry


def test_orchestrator_wires_one_registry_into_context_builder_and_executor():
    context = object()
    builder = StubContextBuilder(context)
    builder.tool_registry = None
    registry = object()

    orchestrator = Orchestrator(builder, tool_registry=registry)

    assert builder.tool_registry is registry
    assert orchestrator.executor.registry is registry


def test_orchestrator_rejects_different_builder_registry():
    builder = StubContextBuilder(object())
    builder.tool_registry = object()

    with pytest.raises(ValueError, match="share one tool registry"):
        Orchestrator(builder, tool_registry=object())


def test_orchestrator_rejects_nonpositive_max_cycles():
    with pytest.raises(ValueError, match="max_cycles"):
        Orchestrator(StubContextBuilder(object()), max_cycles=0)


def test_orchestrator_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        Orchestrator(StubContextBuilder(object()), max_retries=-1)


def test_orchestrator_rejects_negative_max_replans():
    with pytest.raises(ValueError, match="max_replans"):
        Orchestrator(StubContextBuilder(object()), max_replans=-1)


def test_orchestrator_requires_all_stores_in_required_persistence_mode():
    with pytest.raises(ConfigError, match="required persistence"):
        Orchestrator(
            StubContextBuilder(object()), persistence_mode=PersistenceMode.REQUIRED
        )


def test_orchestrator_accepts_disabled_persistence_mode():
    orchestrator = Orchestrator(
        StubContextBuilder(object()), persistence_mode=PersistenceMode.DISABLED
    )

    assert orchestrator.persistence_mode is PersistenceMode.DISABLED


def test_orchestrator_claims_ready_and_rejects_active_or_missing_tasks():
    class Tasks:
        def __init__(self, status):
            self.status = status
            self.claimed = False

        def get(self, _task_id):
            return None if self.status == "missing" else {"status": self.status}

        def claim(self, _task_id):
            self.claimed = True
            return True

    ready = Tasks("ready")
    orchestrator = Orchestrator(StubContextBuilder(object()), task_store=ready)
    assert orchestrator._claim_task(1) is True
    assert ready.claimed is True

    active = Tasks("executing")
    active_orchestrator = Orchestrator(StubContextBuilder(object()), task_store=active)
    assert active_orchestrator._claim_task(1) is False
    assert active_orchestrator.run(1).status is ResultStatus.WAITING
    missing = Tasks("missing")
    assert (
        Orchestrator(StubContextBuilder(object()), task_store=missing)._claim_task(1)
        is None
    )


def test_orchestrator_rejects_invalid_persistence_mode():
    with pytest.raises(ConfigError, match="persistence_mode"):
        Orchestrator(StubContextBuilder(object()), persistence_mode="invalid")


def test_orchestrator_rejects_stores_on_different_connections():
    class Store:
        def __init__(self):
            self.connection = object()

    with pytest.raises(TransactionError, match="share"):
        Orchestrator(
            StubContextBuilder(object()),
            transaction_manager=TransactionManager(object()),
            task_store=Store(),
        )


def test_orchestrator_uses_failure_transaction_after_persistence_error():
    class Manager:
        def __init__(self):
            self.fallback = False

        def _validate_connections(self, _stores):
            return None

        def atomic(self):
            class BrokenContext:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    raise RuntimeError("write failed")

            return BrokenContext()

        def record_failure(self, *_args):
            self.fallback = True

    manager = Manager()
    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=object(),
        event_store=object(),
        transaction_manager=manager,
    )
    orchestrator._fail_task(1, "broken", "task.failed")
    assert manager.fallback is True


def test_orchestrator_retries_execution_without_replanning():
    context = object()
    execution_calls = []

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            execution_calls.append(True)
            if len(execution_calls) == 1:
                return EngineResult(
                    ResultStatus.FAILED,
                    data={"next_action": "retry_execution"},
                )
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "stop"})

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(10)

    assert result.status is ResultStatus.SUCCESS
    assert len(execution_calls) == 2


def test_orchestrator_stops_on_unknown_next_action():
    context = object()

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.FAILED, data={"next_action": "stop"})

    result = Orchestrator(
        StubContextBuilder(context),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(11)

    assert result.status is ResultStatus.FAILED


def test_orchestrator_persists_invalid_execution_action_as_failure():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed",
                execution=ExecutionResult(ExecutionStatus.SUCCESS, next_action="bad"),
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
    ).run(22)

    assert result.status is ResultStatus.FAILED
    assert result.data["error"]["error_type"] == "invalid_next_action"


def test_orchestrator_persists_invalid_validation_action_as_failure():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "bad"})

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    ).run(23)

    assert result.status is ResultStatus.FAILED
    assert result.data["error"]["error_type"] == "invalid_next_action"


def test_action_accepts_existing_next_action_enum():
    from harness.engine.result import NextAction

    assert Orchestrator._action(NextAction.VALIDATE) is NextAction.VALIDATE


def test_resume_marks_checkpoint_and_runs():
    from harness.engine.result import NextAction

    class Checkpoint:
        def __init__(self, value):
            self.value = value
            self.marked = False

        def get(self, _task_id):
            return self.value

        def mark_resumed(self, _task_id):
            self.marked = True
            return True

        def save(self, *_args, **_kwargs):
            return 1

    checkpoint = Checkpoint({"resumed_at": None})
    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=checkpoint
    )
    orchestrator.run = lambda _task_id, **_kwargs: EngineResult.success("resumed")
    orchestrator._save_checkpoint(
        1, "waiting", NextAction.WAIT, 4, "approval", wait_reason="approval"
    )
    assert orchestrator.resume(1).successful is True
    assert checkpoint.marked is True


def test_resume_without_checkpoint_falls_back_to_run():
    checkpoint = type("Checkpoint", (), {"get": lambda self, _task_id: None})()
    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=checkpoint
    )
    orchestrator.run = lambda _task_id, **_kwargs: EngineResult.success("replanned")
    assert orchestrator.resume(1).message == "replanned"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_resume_does_not_run_terminal_task(status):
    class Tasks:
        def get(self, _task_id):
            return {"status": status}

    class Checkpoints:
        def get_active(self, _task_id):
            raise AssertionError("terminal task must not load a checkpoint")

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=Tasks(),
        checkpoint_store=Checkpoints(),
    )
    result = orchestrator.resume(1)
    assert result.status is (
        ResultStatus.SUCCESS if status == "done" else ResultStatus.FAILED
    )
    if status == "done":
        assert result.data["already_completed"] is True


def test_resume_returns_completed_when_task_finishes_during_claim():
    class Tasks:
        calls = 0

        def get(self, _task_id):
            self.calls += 1
            return {"status": "waiting" if self.calls == 1 else "done"}

    class Checkpoints:
        def get_active(self, _task_id):
            return {"next_action": "wait"}

        def claim_resume(self, _task_id):
            return False

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=Tasks(),
        checkpoint_store=Checkpoints(),
    )
    assert orchestrator.resume(1).data["already_completed"] is True


def test_done_fallback_retires_checkpoint_without_unit_of_work():
    calls = []

    class Tasks:
        def complete_attempt(self, attempt_id, status, _error=None):
            calls.append(("attempt", attempt_id, status))

        def transition(self, task_id, status):
            calls.append(("task", task_id, status))

    class Events:
        def record(self, task_id, event_type, _payload=None):
            calls.append(("event", task_id, event_type))

    class Checkpoints:
        def invalidate(self, task_id):
            calls.append(("checkpoint", task_id))

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=Tasks(),
        event_store=Events(),
        checkpoint_store=Checkpoints(),
    )
    orchestrator._complete_done(1, 7)
    assert calls == [
        ("attempt", 7, "completed"),
        ("task", 1, "done"),
        ("event", 1, "task.done"),
        ("checkpoint", 1),
    ]


def test_resume_restores_persisted_retry_plan():
    plan = {
        "goal": "resume",
        "version": 2,
        "steps": [
            {
                "id": "step",
                "description": "step",
                "action": "read",
                "tool": None,
                "arguments": {},
                "acceptance_criteria": [],
                "test_criteria": [],
                "depends_on": [],
                "metadata": {},
            }
        ],
        "assumptions": [],
        "risks": [],
    }

    class Checkpoint:
        def __init__(self):
            self.marked = False

        def get(self, _task_id):
            return {
                "resumed_at": None,
                "next_action": "retry_execution",
                "context_data": json.dumps({"plan": plan}),
            }

        def mark_resumed(self, _task_id):
            self.marked = True

    checkpoint = Checkpoint()
    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=checkpoint
    )
    captured = []
    orchestrator.run = lambda _task_id, **kwargs: (
        captured.append(kwargs) or EngineResult.success("resumed")
    )
    result = orchestrator.resume(1)
    assert result.successful
    assert captured[0]["_resume_action"] is NextAction.RETRY_EXECUTION
    assert captured[0]["_resume_plan"].goal == "resume"
    assert checkpoint.marked is True


def test_plan_deserialization_rejects_invalid_steps():
    assert Orchestrator._deserialize_plan(None) is None
    assert (
        Orchestrator._deserialize_plan({"goal": "x", "steps": [{"bad": True}]}) is None
    )


def test_resume_ignores_corrupt_plan_payload():
    class Checkpoint:
        def get(self, _task_id):
            return {"resumed_at": None, "next_action": "replan", "context_data": "{"}

        def mark_resumed(self, _task_id):
            pass

    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=Checkpoint()
    )
    orchestrator.run = lambda _task_id, **_kwargs: EngineResult.success("replanned")
    assert orchestrator.resume(1).successful


@pytest.mark.parametrize("action", [NextAction.RETRY_EXECUTION, NextAction.VALIDATE])
def test_resume_dispatches_explicit_plan_actions(action):
    orchestrator = Orchestrator(StubContextBuilder(object()))
    captured = []
    orchestrator.run = lambda task_id, **kwargs: (
        captured.append((task_id, kwargs)) or EngineResult.success("ok")
    )
    plan = object()
    result = orchestrator._resume_from_checkpoint(1, {"reason": "test"}, action, plan)
    assert result.successful
    if action is NextAction.RETRY_EXECUTION:
        assert captured[0][1]["_resume_plan"] is plan


def test_resume_wait_keeps_unapproved_task_waiting():
    class Tasks:
        def get(self, _task_id):
            return {"status": "waiting", "approval_status": "pending"}

    orchestrator = Orchestrator(StubContextBuilder(object()), task_store=Tasks())
    result = orchestrator._resume_wait(1, {"reason": "approval required"})
    assert result.status is ResultStatus.WAITING


def test_resume_validation_deserializes_and_calls_validator():
    class Checkpoint:
        def get(self, _task_id):
            return {
                "context_data": json.dumps(
                    {"execution": {"status": "success", "steps": []}}
                )
            }

    class Validator:
        def validate(self, context, plan, execution):
            assert context == "context"
            assert plan == "plan"
            assert execution.status.value == "success"
            return EngineResult.success("validated")

    orchestrator = Orchestrator(
        StubContextBuilder("context"),
        validator=Validator(),
        checkpoint_store=Checkpoint(),
    )
    assert orchestrator._resume_validation(1, "plan").successful


def test_resume_dispatches_invalid_action_as_failure():
    orchestrator = Orchestrator(StubContextBuilder(object()))
    result = orchestrator._resume_from_checkpoint(1, {}, NextAction.STOP, None)
    assert result.status is ResultStatus.FAILED


def test_resume_retry_without_plan_replans():
    orchestrator = Orchestrator(StubContextBuilder(object()))
    orchestrator.run = lambda task_id, **_kwargs: EngineResult.success(str(task_id))
    assert orchestrator._resume_retry_execution(3, None).successful


def test_resume_validation_without_plan_replans():
    orchestrator = Orchestrator(StubContextBuilder(object()))
    orchestrator.run = lambda task_id, **_kwargs: EngineResult.success(str(task_id))
    assert orchestrator._resume_validation(3, None).successful


def test_resume_wait_transitions_non_waiting_task():
    class Tasks:
        def get(self, _task_id):
            return {"status": "ready", "approval_status": "pending"}

        def transition(self, _task_id, status):
            assert status == "waiting"

    orchestrator = Orchestrator(StubContextBuilder(object()), task_store=Tasks())
    assert (
        orchestrator._resume_wait(1, {"reason": "external"}).status
        is ResultStatus.WAITING
    )


def test_execution_result_deserialization_accepts_valid_payload():
    execution = Orchestrator._deserialize_execution(
        {
            "status": "success",
            "steps": [{"step_id": "s1", "status": "success"}],
            "next_action": "validate",
            "changed_files": ["a.py"],
        }
    )
    assert execution is not None
    assert execution.steps[0].step_id == "s1"


def test_execution_result_deserialization_rejects_invalid_payload():
    assert Orchestrator._deserialize_execution(None) is None
    assert Orchestrator._deserialize_execution({"status": "invalid"}) is None


def test_orchestrator_stops_after_maximum_cycles():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.FAILED, data={"next_action": "replan"})

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        max_cycles=1,
    ).run(12)

    assert result.status is ResultStatus.FAILED
    assert "Maximum" in result.message


def test_orchestrator_enforces_retry_limit_from_executor():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            return EngineResult(
                ResultStatus.FAILED, data={"next_action": "retry_execution"}
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
        max_retries=0,
    ).run(13)

    assert result.status is ResultStatus.FAILED
    assert "retries" in result.message


def test_orchestrator_enforces_retry_limit_from_validator():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.FAILED, data={"next_action": "retry_execution"}
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        max_retries=0,
    ).run(14)

    assert result.status is ResultStatus.FAILED
    assert "retries" in result.message


def test_orchestrator_records_validation_retry_event():
    events = []
    calls = []

    class Events:
        def record(self, _task_id, event_type, _payload=None):
            events.append(event_type)

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            calls.append(True)
            if len(calls) == 1:
                return EngineResult(
                    ResultStatus.FAILED,
                    message="retry",
                    data={"next_action": "retry_execution"},
                )
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "stop"})

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        event_store=Events(),
        max_retries=1,
    ).run(15)

    assert result.status is ResultStatus.SUCCESS
    assert "task.retrying" in events


def test_orchestrator_manages_attempt_helpers():
    calls = []

    class Attempts:
        def record_attempt(self, task_id, status):
            calls.append(("start", task_id, status))
            return 9

        def complete_attempt(self, attempt_id, status, error):
            calls.append(("finish", attempt_id, status, error))

    orchestrator = Orchestrator(StubContextBuilder(object()), task_store=Attempts())

    attempt = orchestrator._start_attempt(4)
    orchestrator._finish_attempt(attempt, "failed", "error")

    assert calls == [("start", 4, "running"), ("finish", 9, "failed", "error")]


def test_orchestrator_attempt_helper_uses_transaction_manager():
    connection = sqlite3.connect(":memory:")

    class Attempts:
        def __init__(self):
            self.connection = connection

        def record_attempt(self, _task_id, _status):
            return 7

    store = Attempts()
    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=store,
        transaction_manager=TransactionManager(connection, store),
    )
    assert orchestrator._start_attempt(4) == 7


def test_orchestrator_ignores_missing_artifact_payload():
    class Artifacts:
        def register(self, *_args):
            raise AssertionError("no artifact should be registered")

    orchestrator = Orchestrator(
        StubContextBuilder(object()), artifact_store=Artifacts()
    )

    orchestrator._register_artifacts(1, EngineResult.success("no execution"))
    orchestrator._persist_execution(1, EngineResult.success("no execution"))


def test_orchestrator_registers_artifact_payload_without_unit_of_work():
    registered = []

    class Artifacts:
        def register(self, *args):
            registered.append(args)

    from harness.engine.result import ExecutionResult, ExecutionStatus, StepExecution

    result = EngineResult.success(
        "executed",
        execution=ExecutionResult(
            ExecutionStatus.SUCCESS,
            artifacts=["a.txt"],
            steps=[StepExecution("step", ExecutionStatus.SUCCESS, artifacts=["b.txt"])],
        ),
    )
    Orchestrator(
        StubContextBuilder(object()), artifact_store=Artifacts()
    )._register_artifacts(1, result)
    assert [item[1] for item in registered] == ["a.txt", "b.txt"]


@pytest.mark.parametrize(
    "next_action, expected",
    [("wait", ResultStatus.WAITING), ("stop", ResultStatus.SUCCESS)],
)
def test_orchestrator_honors_execution_terminal_actions(next_action, expected):
    transitions = []

    class Store:
        def transition(self, _task_id, status):
            transitions.append(status)

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult(
                ResultStatus.SUCCESS,
                message="executed",
                data={
                    "execution": ExecutionResult(
                        ExecutionStatus.SUCCESS, next_action=next_action
                    )
                },
                wait_reason="external_information" if next_action == "wait" else None,
            )

    class Validator:
        def validate(self, *_args):
            raise AssertionError("terminal execution action must skip validation")

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=Store(),
    ).run(16)

    assert result.status is expected
    assert transitions[-1] == ("waiting" if next_action == "wait" else "done")


def test_orchestrator_replans_after_successful_execution_request():
    plans = []

    class Planner:
        def plan(self, _context):
            plans.append(True)
            return EngineResult.success("planned", plan=len(plans))

    class Executor:
        def execute(self, _context, plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            action = "replan" if plan == 1 else "stop"
            return EngineResult.success(
                "executed",
                execution=ExecutionResult(ExecutionStatus.SUCCESS, next_action=action),
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
    ).run(17)

    assert result.status is ResultStatus.SUCCESS
    assert len(plans) == 2


def test_orchestrator_stops_after_validation_replan_limit():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.FAILED, message="bad", data={"next_action": "replan"}
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        max_replans=0,
    ).run(71)
    assert result.message == "Maximum replans exceeded."


def test_orchestrator_retries_after_successful_execution_request():
    executions = []

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            executions.append(True)
            action = "retry_execution" if len(executions) == 1 else "stop"
            return EngineResult.success(
                "executed",
                execution=ExecutionResult(ExecutionStatus.SUCCESS, next_action=action),
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
    ).run(18)

    assert result.status is ResultStatus.SUCCESS
    assert len(executions) == 2


@pytest.mark.parametrize("next_action", ["wait", "stop", "validate"])
def test_orchestrator_honors_failed_execution_terminal_actions(next_action):
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            return EngineResult(
                ResultStatus.FAILED,
                message="blocked",
                data={"next_action": next_action},
                wait_reason="external_information" if next_action == "wait" else None,
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
    ).run(19)

    assert result.status is (
        ResultStatus.WAITING if next_action == "wait" else ResultStatus.FAILED
    )


def test_orchestrator_limits_successful_execution_retry():
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed",
                execution=ExecutionResult(
                    ExecutionStatus.SUCCESS, next_action="retry_execution"
                ),
            )

    result = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=object(),
        max_retries=0,
    ).run(20)

    assert result.status is ResultStatus.FAILED


def test_execution_wait_uses_atomic_unit_of_work():
    calls = []

    class UnitOfWork:
        def execution_wait(self, *args):
            calls.append(args)

    plan = SimpleNamespace(version=3, steps=[SimpleNamespace(id="next")])
    orchestrator = Orchestrator(StubContextBuilder(object()), checkpoint_store=object())
    orchestrator.unit_of_work = UnitOfWork()
    orchestrator._persist_execution_wait(
        1,
        4,
        plan,
        EngineResult.waiting("blocked", "external_information"),
    )
    assert calls[0][0:2] == (1, 4)
    assert calls[0][2]["next_step_id"] == "next"


def test_resume_rejects_checkpoint_claim_conflict():
    class Checkpoint:
        def get(self, _task_id):
            return {"resumed_at": None, "next_action": "replan", "context_data": None}

        def claim_resume(self, _task_id):
            return False

    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=Checkpoint()
    )
    assert orchestrator.resume(1).status is ResultStatus.WAITING


def test_resume_rejects_checkpoint_already_resumed():
    class Checkpoint:
        def get(self, _task_id):
            return {"resumed_at": "now", "next_action": "replan", "context_data": None}

    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=Checkpoint()
    )
    assert orchestrator.resume(1).status is ResultStatus.WAITING


def test_cancel_fallback_without_task_cancel_method():
    transitions = []
    events = []

    class Tasks:
        def transition(self, _task_id, status):
            transitions.append(status)

    class Events:
        def record(self, _task_id, event_type, _payload=None):
            events.append(event_type)

    orchestrator = Orchestrator(
        StubContextBuilder(object()), task_store=Tasks(), event_store=Events()
    )
    assert orchestrator.cancel(1).successful
    assert transitions == ["cancelled"]
    assert events == ["task.cancelled"]


def test_execution_wait_fallback_persists_checkpoint_without_unit_of_work():
    calls = []

    class Checkpoint:
        def save(self, task_id, **payload):
            calls.append((task_id, payload))

    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=Checkpoint()
    )
    orchestrator._persist_execution_wait(
        1,
        None,
        SimpleNamespace(version=1, steps=[]),
        EngineResult.waiting("blocked", "external_information"),
    )
    assert calls[0][1]["reason"] == "blocked"


def test_cancel_fallback_invalidates_checkpoint_and_uses_task_cancel():
    calls = []

    class Tasks:
        def cancel(self, task_id, reason):
            calls.append(("cancel", task_id, reason))

    class Checkpoint:
        def invalidate(self, task_id, reason):
            calls.append(("invalidate", task_id, reason))

    class Events:
        def record(self, *_args):
            calls.append(("event",))

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        task_store=Tasks(),
        checkpoint_store=Checkpoint(),
        event_store=Events(),
    )
    orchestrator.cancel(1, "stop")
    assert calls == [("cancel", 1, "stop"), ("invalidate", 1, "stop"), ("event",)]


@pytest.mark.parametrize(
    ("status", "next_action", "expected"),
    [
        (ResultStatus.SUCCESS, "stop", ResultStatus.SUCCESS),
        (ResultStatus.WAITING, "wait", ResultStatus.WAITING),
        (ResultStatus.FAILED, "retry_execution", ResultStatus.SUCCESS),
        (ResultStatus.FAILED, "replan", ResultStatus.SUCCESS),
        (ResultStatus.FAILED, "stop", ResultStatus.FAILED),
        (ResultStatus.FAILED, "invalid", ResultStatus.FAILED),
    ],
)
def test_validation_resume_routes_each_decision(status, next_action, expected):
    from harness.engine.plan import ExecutionPlan
    from harness.engine.result import ExecutionStatus

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                status,
                "validation result",
                data={"next_action": next_action},
                wait_reason="external_information" if next_action == "wait" else None,
            )

    class Tasks:
        def complete_attempt(self, *_args):
            return None

        def transition(self, *_args):
            return None

    class Events:
        def record(self, *_args):
            return None

    plan = ExecutionPlan("goal")
    checkpoint = {
        "attempt_id": 9,
        "context_data": json.dumps(
            {"execution": {"status": ExecutionStatus.SUCCESS.value, "steps": []}}
        ),
    }
    orchestrator = Orchestrator(
        StubContextBuilder("context"),
        validator=Validator(),
        task_store=Tasks(),
        event_store=Events(),
        checkpoint_store=type("Checkpoints", (), {"save": lambda *_a, **_k: 1})(),
    )
    if next_action in {"retry_execution", "replan"}:
        orchestrator.run = lambda *_args, **_kwargs: EngineResult.success("continued")
    result = orchestrator._resume_validation(1, checkpoint, plan)
    assert result.status is expected


@pytest.mark.parametrize(
    ("reason", "code", "expected"),
    [
        ("Approval required", "approval", "approval"),
        ("workspace missing", "workspace_missing", "workspace_missing"),
        ("temporary timeout", "temporary_error", "temporary_error"),
        ("please replan", "manual_replan", "manual_replan"),
        ("waiting for details", "external_information", "external_information"),
    ],
)
def test_waiting_reason_codes_are_classified(reason, code, expected):
    assert Orchestrator._waiting_reason_code(reason) == expected == code


def test_resume_uses_and_releases_resume_lease():
    calls = []

    class Checkpoint:
        def get(self, _task_id):
            return {
                "resumed_at": None,
                "next_action": "wait",
                "reason": "external info needed",
                "context_data": None,
            }

        def claim_resume_lease(self, _task_id, **kwargs):
            calls.append(("claim", kwargs["lease_seconds"]))
            return kwargs["token"]

        def release_resume_lease(self, _task_id, _token):
            calls.append(("release",))
            return True

    checkpoint = Checkpoint()
    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        checkpoint_store=checkpoint,
        resume_lease_seconds=19,
    )
    result = orchestrator.resume(1)
    assert result.status is ResultStatus.WAITING
    assert calls == [("claim", 19), ("release",)]


def test_resume_validation_dispatches_from_checkpoint():
    from harness.engine.plan import ExecutionPlan
    from harness.engine.result import ExecutionStatus

    plan = ExecutionPlan("goal")

    class Checkpoint:
        def get(self, _task_id):
            return {
                "resumed_at": None,
                "next_action": "validate",
                "attempt_id": 5,
                "context_data": json.dumps(
                    {
                        "plan": Orchestrator._serialize(plan),
                        "execution": {
                            "status": ExecutionStatus.SUCCESS.value,
                            "steps": [],
                        },
                    }
                ),
            }

        def claim_resume(self, _task_id):
            return True

        def mark_resumed(self, _task_id):
            return True

    class Validator:
        def validate(self, _context, resumed_plan, execution):
            assert resumed_plan.goal == "goal"
            assert execution.status is ExecutionStatus.SUCCESS
            return EngineResult.success("validated", next_action="stop")

    orchestrator = Orchestrator(
        StubContextBuilder("context"),
        validator=Validator(),
        checkpoint_store=Checkpoint(),
    )
    assert orchestrator.resume(1).successful


def test_resume_validation_records_unexpected_validator_exception():
    from harness.engine.plan import ExecutionPlan
    from harness.engine.result import ExecutionStatus

    checkpoint = {
        "attempt_id": 7,
        "context_data": json.dumps(
            {"execution": {"status": ExecutionStatus.SUCCESS.value, "steps": []}}
        ),
    }

    class Validator:
        def validate(self, *_args):
            raise RuntimeError("validator failure")

    class Tasks:
        def complete_attempt(self, *_args):
            return None

        def transition(self, *_args):
            return None

    class Events:
        def record(self, *_args):
            return None

    orchestrator = Orchestrator(
        StubContextBuilder("context"),
        validator=Validator(),
        task_store=Tasks(),
        event_store=Events(),
    )

    class UnitOfWork:
        def start_cycle(self, *_args):
            return 8

    orchestrator.unit_of_work = UnitOfWork()
    result = orchestrator._resume_validation(1, checkpoint, ExecutionPlan("goal"))
    assert result.status is ResultStatus.FAILED
    assert "validator failure" in result.message


def test_resume_wait_replans_malformed_stored_payload():
    orchestrator = Orchestrator(StubContextBuilder(object()))
    orchestrator.run = lambda *_args, **_kwargs: EngineResult.success("replanned")
    result = orchestrator._resume_wait(
        1,
        {
            "reason": "timeout; retry later",
            "waiting_reason_code": "temporary_error",
            "context_data": "{malformed",
        },
    )
    assert result.successful


def test_resume_wait_replans_when_saved_plan_is_invalid():
    orchestrator = Orchestrator(StubContextBuilder(object()))
    orchestrator.run = lambda *_args, **_kwargs: EngineResult.success("replanned")
    result = orchestrator._resume_wait(
        1,
        {
            "reason": "temporary timeout",
            "waiting_reason_code": "temporary_error",
            "context_data": json.dumps({"plan": {"invalid": True}}),
        },
    )
    assert result.successful


def test_resume_validation_replans_from_serialized_plan():
    from harness.engine.plan import ExecutionPlan
    from harness.engine.result import ExecutionStatus

    plan = ExecutionPlan("goal")

    class Checkpoint:
        def get(self, _task_id):
            return {
                "context_data": json.dumps(
                    {
                        "plan": Orchestrator._serialize(plan),
                        "execution": {
                            "status": ExecutionStatus.SUCCESS.value,
                            "steps": [],
                        },
                    }
                )
            }

    class Validator:
        def validate(self, *_args):
            return EngineResult.success("valid")

    orchestrator = Orchestrator(
        StubContextBuilder("context"),
        validator=Validator(),
        checkpoint_store=Checkpoint(),
    )
    assert orchestrator._resume_validation(1, None).successful


def test_orchestrator_rejects_nonpositive_resume_lease():
    with pytest.raises(ValueError, match="resume_lease_seconds"):
        Orchestrator(StubContextBuilder(object()), resume_lease_seconds=0)


def test_orchestrator_starts_unclaimed_cycle_through_unit_of_work():
    from contextlib import nullcontext

    calls = []

    class UnitOfWork:
        phase = staticmethod(nullcontext)

        def start_cycle(self, task_id, status, event_type):
            calls.append((task_id, status, event_type))
            return 3

        def validation_decision(self, write):
            calls.append((write.task_id, write.attempt_id, write.outcome))

    class Tasks:
        def record_attempt(self, *_args):
            return 3

        def complete_attempt(self, *_args):
            return None

        def transition(self, *_args):
            return None

    class Events:
        def record(self, *_args):
            return None

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, *_args):
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult.success("validated")

    orchestrator = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=Tasks(),
        event_store=Events(),
        artifact_store=object(),
    )
    orchestrator.unit_of_work = UnitOfWork()
    assert orchestrator.run(1).successful
    assert calls == [(1, "planning", "task.planning"), (1, 3, "done")]


def test_waiting_task_is_not_claimed_and_resolution_requires_uow():
    class Tasks:
        def get(self, _task_id):
            return {"status": "waiting"}

        def claim(self, _task_id, **_kwargs):
            raise AssertionError("waiting tasks must not be claimed")

    engine = Orchestrator(StubContextBuilder(object()), task_store=Tasks())
    assert engine._claim_task(1) is None
    with pytest.raises(ConfigError, match="transactional SQLite"):
        engine.resolve_external_wait(1, "token", "actor", "answer.txt")


@pytest.mark.parametrize("stage", ["execution", "validation"])
@pytest.mark.parametrize("action", [None, "wait"])
def test_new_wait_rejects_missing_action_or_reason(stage, action):
    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan="plan")

    class Executor:
        def execute(self, *_args):
            if stage == "execution":
                return EngineResult(
                    ResultStatus.WAITING, "blocked", data={"next_action": action}
                )
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.WAITING, "blocked", data={"next_action": action}
            )

    engine = Orchestrator(
        StubContextBuilder(object()),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
    )
    result = engine.run(1)
    assert result.status is ResultStatus.FAILED
    assert "requires" in result.message


def test_resume_reloads_checkpoint_after_claim_and_handles_retirement():
    class Checkpoint:
        calls = 0

        def get_active(self, _task_id):
            self.calls += 1
            return {"next_action": "wait"} if self.calls == 1 else None

    class Uow:
        released = False

        def claim_resume_lease(self, *_args, **_kwargs):
            return True

        def release_resume_lease(self, *_args):
            self.released = True

    engine = Orchestrator(StubContextBuilder(object()), checkpoint_store=Checkpoint())
    engine.unit_of_work = Uow()
    result = engine.resume(1)
    assert result.status is ResultStatus.FAILED
    assert "retired" in result.message
    assert engine.unit_of_work.released


def test_validation_resume_rejects_wait_without_reason():
    from harness.engine.plan import ExecutionPlan

    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.WAITING, "blocked", data={"next_action": "wait"}
            )

    class Tasks:
        def transition(self, *_args):
            return None

        def complete_attempt(self, *_args):
            return None

    engine = Orchestrator(
        StubContextBuilder(object()),
        validator=Validator(),
        task_store=Tasks(),
    )
    checkpoint = {"context_data": {"execution": {"status": "success", "steps": []}}}
    result = engine._resume_validation(1, checkpoint, ExecutionPlan("goal"))
    assert result.status is ResultStatus.FAILED
    assert "WAIT requires a valid WaitReason" in result.message


def test_validation_decision_rejects_conflicting_success_and_exhausted_retry():
    engine = Orchestrator(StubContextBuilder(object()), max_cycles=1)
    conflicting = engine._validation_decision(
        EngineResult(ResultStatus.SUCCESS, data={"next_action": "replan"}),
        retries=0,
        replans=0,
        cycle=0,
    )
    assert conflicting.outcome == "failed"
    assert "conflicting" in conflicting.result.message
    exhausted = engine._validation_decision(
        EngineResult(ResultStatus.FAILED, data={"next_action": "retry_execution"}),
        retries=0,
        replans=0,
        cycle=0,
    )
    assert exhausted.outcome == "failed"
    assert "cycles" in exhausted.result.message


def test_resume_with_exhausted_cycle_budget_fails_without_running_stages():
    engine = Orchestrator(
        StubContextBuilder(object()),
        planner=object(),
        executor=object(),
        validator=object(),
        max_cycles=1,
    )
    result = engine.run(1, _resume_claimed=True, _resume_cycles=1)
    assert result.status is ResultStatus.FAILED
    assert "cycles" in result.message


def test_done_uses_unit_of_work_when_available():
    calls = []

    class Work:
        def complete_done(self, task_id, attempt_id):
            calls.append((task_id, attempt_id))

    engine = Orchestrator(StubContextBuilder(object()))
    engine.unit_of_work = Work()
    engine._complete_done(2, 3)
    assert calls == [(2, 3)]


def test_record_replan_uses_plan_version():
    calls = []

    class Tasks:
        def record_replan(self, *args, **kwargs):
            calls.append((args, kwargs))

    engine = Orchestrator(StubContextBuilder(object()), task_store=Tasks())
    engine._record_replan(
        2, SimpleNamespace(version=4), 9, "execution_requested_replan"
    )
    assert calls == [
        (
            (2, "execution_requested_replan", 5),
            {"parent_plan_version": 4, "attempt_id": 9},
        )
    ]
