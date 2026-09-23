"""Tests for the engine orchestration boundary."""

import json
import sqlite3

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

        def save(self, *_args, **_kwargs):
            return 1

    checkpoint = Checkpoint({"resumed_at": None})
    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=checkpoint
    )
    orchestrator.run = lambda _task_id: EngineResult.success("resumed")
    orchestrator._save_checkpoint(1, "waiting", NextAction.WAIT, 4, "approval")
    assert orchestrator.resume(1).successful is True
    assert checkpoint.marked is True


def test_resume_without_checkpoint_falls_back_to_run():
    checkpoint = type("Checkpoint", (), {"get": lambda self, _task_id: None})()
    orchestrator = Orchestrator(
        StubContextBuilder(object()), checkpoint_store=checkpoint
    )
    orchestrator.run = lambda _task_id: EngineResult.success("replanned")
    assert orchestrator.resume(1).message == "replanned"


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

            return EngineResult.success(
                "executed",
                execution=ExecutionResult(
                    ExecutionStatus.SUCCESS, next_action=next_action
                ),
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
