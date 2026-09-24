"""SQLite-backed tests for persistent structured human interactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from harness.config import ExecutionConfig
from harness.engine.context_builder import ContextBuilder
from harness.engine.orchestrator import Orchestrator
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    NextAction,
    ResultStatus,
    StepExecution,
    WaitReason,
)
from harness.engine.validator import Validator
from harness.storage.database import connect, initialize_database
from harness.storage.factory import StoreFactory
from harness.storage.project_store import ProjectStore


def _application(
    database_path, workspace, *, planner, executor, mode="minimal", task_id=None
):
    initialized = (
        database_path if database_path.exists() else initialize_database(database_path)
    )
    connection = connect(initialized)
    stores = StoreFactory.create(connection, workspace)
    projects = ProjectStore(connection)
    if task_id is None:
        project_id = projects.create("interaction-test", str(workspace))
        task_id = stores.task_store.create("Task", project_id=project_id)
        stores.task_store.transition(task_id, "ready")
        stores.task_store.approve(task_id, "owner")
    builder = ContextBuilder.from_stores(stores, projects, hitl_mode=mode)
    orchestrator = Orchestrator(
        builder,
        planner=planner,
        executor=executor,
        validator=Validator(),
        stores=stores,
        execution_config=ExecutionConfig(persistence_mode="required", hitl_mode=mode),
    )
    return connection, orchestrator, task_id


class _CountingExecutor:
    def __init__(self):
        self.contexts = []
        self.calls = 0

    def execute(self, context, plan):
        self.calls += 1
        self.contexts.append(context)
        return EngineResult.success(
            "executed",
            execution=ExecutionResult(
                ExecutionStatus.SUCCESS,
                steps=[StepExecution(plan.steps[0].id, ExecutionStatus.SUCCESS)],
                next_action=NextAction.VALIDATE,
            ),
        )


def test_plan_review_survives_restart_and_requires_answer_before_execution(tmp_path):
    plan = ExecutionPlan("ship feature", [PlanStep("step-1", "Do it", "do")])

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan=plan)

    executor = _CountingExecutor()
    db = tmp_path / "hitl-review.sqlite"
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=executor, mode="interactive"
    )
    waiting = orchestrator.run(task_id)
    assert waiting.status is ResultStatus.WAITING
    assert waiting.wait_reason is WaitReason.PLAN_REVIEW
    assert executor.calls == 0
    interaction = orchestrator.list_human_interactions(task_id)[0]
    interaction_id = interaction["interaction_id"]
    connection.close()

    executor_after_restart = _CountingExecutor()
    connection, restarted, _ = _application(
        db,
        tmp_path,
        planner=Planner(),
        executor=executor_after_restart,
        mode="interactive",
    )
    pending = restarted.list_human_interactions(task_id)
    assert pending[0]["interaction_id"] == interaction_id
    assert restarted.answer_human_interaction(
        task_id, interaction_id, "reviewer", {"decision": "approved"}
    )
    assert not restarted.answer_human_interaction(
        task_id, interaction_id, "reviewer", {"decision": "approved"}
    )
    result = restarted.resume(task_id)
    assert result.status is ResultStatus.SUCCESS
    assert executor_after_restart.calls == 1
    assert executor_after_restart.contexts[0].human_responses[0]["response"] == {
        "decision": "approved"
    }
    connection.close()


def test_information_answer_reaches_planner_after_restart(tmp_path):
    response_schema = {
        "type": "object",
        "required": ["environment"],
        "properties": {"environment": {"type": "string"}},
        "additionalProperties": False,
    }

    class Planner:
        def plan(self, context):
            if not context.human_responses:
                return EngineResult.waiting(
                    "Which environment?",
                    WaitReason.HUMAN_INPUT,
                    interaction_request={
                        "kind": "information_request",
                        "prompt": "Which environment should receive the change?",
                        "response_schema": response_schema,
                        "request_data": {"field": "environment"},
                        "resume_action": "replan",
                        "required": True,
                    },
                )
            assert context.human_responses[-1]["response"]["environment"] == "staging"
            return EngineResult.success(
                "planned",
                plan=ExecutionPlan("ship", [PlanStep("step-1", "Do it", "do")]),
            )

    db = tmp_path / "hitl-info.sqlite"
    executor = _CountingExecutor()
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=executor
    )
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    interaction = orchestrator.list_human_interactions(task_id)[0]
    interaction_id = interaction["interaction_id"]
    connection.close()

    connection, restarted, _ = _application(
        db, tmp_path, planner=Planner(), executor=executor
    )
    with pytest.raises(ValueError, match="missing required fields"):
        restarted.answer_human_interaction(task_id, interaction_id, "human", {})
    assert restarted.answer_human_interaction(
        task_id,
        interaction_id,
        "human",
        {"environment": "staging"},
    )
    result = restarted.resume(task_id)
    assert result.status is ResultStatus.SUCCESS
    assert executor.calls == 1
    assert executor.contexts[0].human_responses[-1]["response"] == {
        "environment": "staging"
    }
    connection.close()


def test_interaction_api_rejects_stale_ids_and_hides_answer_payload(tmp_path):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Need a choice",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Choose",
                    "response_schema": {"type": "string", "enum": ["a", "b"]},
                    "resume_action": "replan",
                    "required": True,
                },
            )

    db = tmp_path / "hitl-api.sqlite"
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=_CountingExecutor()
    )
    orchestrator.run(task_id)
    interaction = orchestrator.list_human_interactions(task_id)[0]
    assert "response_data" not in interaction
    with pytest.raises(ValueError, match="not found"):
        orchestrator.answer_human_interaction(task_id, "not-the-id", "human", "a")
    with pytest.raises(ValueError, match="allowed values"):
        orchestrator.answer_human_interaction(
            task_id, interaction["interaction_id"], "human", "c"
        )
    connection.close()


def test_invalid_interaction_contract_fails_the_planning_attempt(tmp_path):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Invalid request",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "unknown",
                    "prompt": "Choose",
                    "response_schema": {"type": "string"},
                    "resume_action": "replan",
                },
            )

    db = tmp_path / "hitl-invalid.sqlite"
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=_CountingExecutor()
    )
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.FAILED
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert (
        connection.execute(
            "SELECT status FROM task_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    connection.close()


def test_cancel_terminalizes_pending_interaction(tmp_path):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Need answer",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Choose",
                    "response_schema": {"type": "string", "enum": ["yes", "no"]},
                    "resume_action": "replan",
                    "required": True,
                },
            )

    db = tmp_path / "hitl-cancel.sqlite"
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=_CountingExecutor()
    )
    orchestrator.run(task_id)
    interaction_id = orchestrator.list_human_interactions(task_id)[0]["interaction_id"]
    orchestrator.cancel(task_id)
    assert (
        connection.execute(
            "SELECT status FROM task_human_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == "cancelled"
    )
    with pytest.raises(ValueError, match="no active waiting"):
        orchestrator.answer_human_interaction(task_id, interaction_id, "human", "yes")
    connection.close()


def test_answer_racing_resume_never_executes_without_a_durable_answer(tmp_path):
    class Planner:
        def plan(self, context):
            if not context.human_responses:
                return EngineResult.waiting(
                    "Need answer",
                    WaitReason.HUMAN_INPUT,
                    interaction_request={
                        "kind": "human_decision",
                        "prompt": "Choose",
                        "response_schema": {"type": "string", "enum": ["go"]},
                        "resume_action": "replan",
                        "required": True,
                    },
                )
            return EngineResult.success(
                "planned", plan=ExecutionPlan("ship", [PlanStep("s", "do", "do")])
            )

    db = tmp_path / "hitl-race.sqlite"
    initial_executor = _CountingExecutor()
    connection, initial, task_id = _application(
        db, tmp_path, planner=Planner(), executor=initial_executor
    )
    initial.run(task_id)
    interaction_id = initial.list_human_interactions(task_id)[0]["interaction_id"]
    connection.close()

    executor = _CountingExecutor()
    barrier = Barrier(2)

    def answer():
        connection, answerer, _ = _application(
            db, tmp_path, planner=Planner(), executor=executor, task_id=task_id
        )
        barrier.wait()
        result = answerer.answer_human_interaction(
            task_id, interaction_id, "reviewer", "go"
        )
        connection.close()
        return result

    def resume():
        connection, resumer, _ = _application(
            db, tmp_path, planner=Planner(), executor=executor, task_id=task_id
        )
        barrier.wait()
        result = resumer.resume(task_id)
        connection.close()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer_future = pool.submit(answer)
        resume_future = pool.submit(resume)
        assert answer_future.result()
        first_resume = resume_future.result()
    if first_resume.status is not ResultStatus.SUCCESS:
        connection, final_orchestrator, _ = _application(
            db, tmp_path, planner=Planner(), executor=executor, task_id=task_id
        )
        final = final_orchestrator.resume(task_id)
        connection.close()
    else:
        final = first_resume
    assert final.status is ResultStatus.SUCCESS
    assert executor.calls == 1


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "unsupported"},
        {"type": "string", "enum": "not-a-list"},
        {"type": "object", "properties": []},
        {"type": "object", "required": "name"},
        {"type": "object", "properties": {"name": "not-a-schema"}},
    ],
)
def test_invalid_response_schema_rejects_wait_atomically(tmp_path, schema):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Need answer",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Choose",
                    "response_schema": schema,
                    "resume_action": "replan",
                    "required": True,
                },
            )

    db = tmp_path / "invalid-schema.sqlite"
    connection, orchestrator, task_id = _application(
        db, tmp_path, planner=Planner(), executor=_CountingExecutor()
    )
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.FAILED
    assert orchestrator.list_human_interactions(task_id) == []
    assert (
        connection.execute(
            "SELECT count(*) FROM task_human_interactions WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT status FROM task_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    connection.close()


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("human_decision", WaitReason.PLAN_REVIEW),
        ("plan_review", WaitReason.HUMAN_INPUT),
    ],
)
def test_interaction_kind_must_match_wait_reason(tmp_path, kind, reason):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Need answer",
                reason,
                interaction_request={
                    "kind": kind,
                    "prompt": "Choose",
                    "response_schema": {"type": "string"},
                    "resume_action": "replan",
                    "required": True,
                },
            )

    connection, orchestrator, task_id = _application(
        tmp_path / "wrong-reason.sqlite",
        tmp_path,
        planner=Planner(),
        executor=_CountingExecutor(),
    )
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.FAILED
    assert orchestrator.list_human_interactions(task_id) == []
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    connection.close()


def test_changed_answer_is_rejected_without_second_audit_event(tmp_path):
    class Planner:
        def plan(self, _context):
            return EngineResult.waiting(
                "Need answer",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Choose",
                    "response_schema": {"type": "string", "enum": ["yes", "no"]},
                    "resume_action": "replan",
                    "required": True,
                },
            )

    connection, orchestrator, task_id = _application(
        tmp_path / "changed-answer.sqlite",
        tmp_path,
        planner=Planner(),
        executor=_CountingExecutor(),
    )
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    interaction_id = orchestrator.list_human_interactions(task_id)[0]["interaction_id"]
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "yes"
    )
    with pytest.raises(ValueError, match="different answer"):
        orchestrator.answer_human_interaction(task_id, interaction_id, "owner", "no")
    assert (
        connection.execute(
            "SELECT response_data FROM task_human_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == '"yes"'
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM task_events WHERE task_id = ? AND event_type = 'task.interaction.answered'",
            (task_id,),
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_old_interaction_token_cannot_answer_new_wait(tmp_path):
    class Planner:
        def plan(self, context):
            return EngineResult.waiting(
                "Need answer",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Choose",
                    "response_schema": {"type": "string"},
                    "request_data": {"round": len(context.human_responses) + 1},
                    "resume_action": "replan",
                    "required": True,
                },
            )

    connection, orchestrator, task_id = _application(
        tmp_path / "new-wait.sqlite",
        tmp_path,
        planner=Planner(),
        executor=_CountingExecutor(),
    )
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    old_id = orchestrator.list_human_interactions(task_id)[0]["interaction_id"]
    assert orchestrator.answer_human_interaction(task_id, old_id, "owner", "first")
    assert orchestrator.resume(task_id).status is ResultStatus.WAITING
    new_id = orchestrator.list_human_interactions(task_id)[0]["interaction_id"]
    assert new_id != old_id
    with pytest.raises(ValueError, match="stale human interaction"):
        orchestrator.answer_human_interaction(task_id, old_id, "owner", "first")
    assert (
        connection.execute(
            "SELECT status FROM task_human_interactions WHERE interaction_id = ?",
            (new_id,),
        ).fetchone()[0]
        == "pending"
    )
    connection.close()


def test_changes_requested_requires_new_plan_review_before_execution(tmp_path):
    class Planner:
        def __init__(self):
            self.calls = 0

        def plan(self, _context):
            self.calls += 1
            return EngineResult.success(
                "planned",
                plan=ExecutionPlan(
                    "ship feature",
                    [PlanStep(f"step-{self.calls}", "Do it", "do")],
                    version=self.calls,
                ),
            )

    planner = Planner()
    executor = _CountingExecutor()
    connection, orchestrator, task_id = _application(
        tmp_path / "changes-requested.sqlite",
        tmp_path,
        planner=planner,
        executor=executor,
        mode="interactive",
    )
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    first = orchestrator.list_human_interactions(task_id)[0]
    assert orchestrator.answer_human_interaction(
        task_id,
        first["interaction_id"],
        "reviewer",
        {"decision": "changes_requested", "feedback": "Revise step"},
    )
    assert orchestrator.resume(task_id).status is ResultStatus.WAITING
    second = orchestrator.list_human_interactions(task_id)[0]
    assert second["interaction_id"] != first["interaction_id"]
    assert second["plan_fingerprint"] != first["plan_fingerprint"]
    assert planner.calls == 2 and executor.calls == 0
    assert orchestrator.answer_human_interaction(
        task_id, second["interaction_id"], "reviewer", {"decision": "approved"}
    )
    assert orchestrator.resume(task_id).status is ResultStatus.SUCCESS
    assert executor.calls == 1
    connection.close()


def test_answered_validation_interaction_resumes_saved_execution(tmp_path):
    plan = ExecutionPlan("ship", [PlanStep("step-1", "Do it", "do")])
    execution = ExecutionResult(
        ExecutionStatus.SUCCESS,
        steps=[StepExecution("step-1", ExecutionStatus.SUCCESS)],
        next_action=NextAction.VALIDATE,
    )

    class Planner:
        def plan(self, _context):
            result = EngineResult.waiting(
                "Validate after confirmation",
                WaitReason.HUMAN_INPUT,
                interaction_request={
                    "kind": "human_decision",
                    "prompt": "Validate now?",
                    "response_schema": {"type": "boolean"},
                    "resume_action": "validate",
                    "required": True,
                },
            )
            result.data = {"plan": plan, "execution": execution}
            return result

    executor = _CountingExecutor()
    db = tmp_path / "validate-interaction.sqlite"
    connection, orchestrator, task_id = _application(
        db,
        tmp_path,
        planner=Planner(),
        executor=executor,
    )
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    interaction_id = orchestrator.list_human_interactions(task_id)[0]["interaction_id"]
    connection.close()

    connection, restarted, _ = _application(
        db,
        tmp_path,
        planner=Planner(),
        executor=executor,
    )
    assert restarted.answer_human_interaction(task_id, interaction_id, "owner", True)
    result = restarted.resume(task_id)
    assert result.status is ResultStatus.SUCCESS
    assert executor.calls == 0
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    connection.close()
