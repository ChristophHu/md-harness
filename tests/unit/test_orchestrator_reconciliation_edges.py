"""Safety checks for unavailable persistence and malformed interaction requests."""

from types import SimpleNamespace

import pytest

from harness.config import ConfigError, PersistenceMode
from harness.engine.context import ExecutionContext
from harness.engine.orchestrator import Orchestrator
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import EngineResult, NextAction, ResultStatus, WaitReason


def test_unavailable_interaction_and_reconciliation_api_is_explicit():
    orchestrator = Orchestrator(object())
    with pytest.raises(ConfigError, match="human interactions require"):
        orchestrator.list_human_interactions(1)
    with pytest.raises(ConfigError, match="human interactions require"):
        orchestrator.answer_human_interaction(1, "request", "human", "yes")
    with pytest.raises(ConfigError, match="tool reconciliation requires"):
        orchestrator.resolve_tool_invocation(
            1, "wait", "invocation", "human", "completed", "proof"
        )
    assert orchestrator.list_tool_reconciliations(1) == []
    assert orchestrator._reconcile_filesystem_write(1, {}, {}) is None
    assert (
        orchestrator._persist_planner_interaction(
            1, 1, EngineResult.waiting("human", WaitReason.HUMAN_INPUT), 1, 0, 0
        )
        is None
    )


def test_project_suggestion_needs_an_existing_project_task():
    orchestrator = Orchestrator(object())
    orchestrator.decision_vault = SimpleNamespace(suggest=lambda *_args: ["unused"])
    orchestrator.task_store = SimpleNamespace(get=lambda _task_id: None)
    assert orchestrator.suggest_human_interaction(1, "question") == []


def test_planner_interaction_rejects_malformed_or_unresumable_requests():
    orchestrator = Orchestrator(object())
    orchestrator.unit_of_work = SimpleNamespace(
        _validate_interaction_request=lambda _request: None
    )
    orchestrator.checkpoint_store = object()
    result = EngineResult.waiting("human", WaitReason.HUMAN_INPUT)
    result.interaction_request = "not-a-mapping"
    with pytest.raises(TypeError, match="mapping"):
        orchestrator._persist_planner_interaction(1, 1, result, 1, 0, 0)
    result.interaction_request = {
        "kind": "human_decision",
        "prompt": "Review",
        "resume_action": "validate",
    }
    with pytest.raises(ValueError, match="saved plan and execution"):
        orchestrator._persist_planner_interaction(1, 1, result, 1, 0, 0)


def test_stage_interaction_contract_rejects_unanswerable_requests():
    engine = Orchestrator(object())
    result = EngineResult.waiting("question", WaitReason.HUMAN_INPUT)
    result.interaction_request = "text"
    with pytest.raises(TypeError, match="mapping"):
        engine._validate_stage_interaction(result, NextAction.WAIT, "execution")
    request = {
        "kind": "human_decision",
        "prompt": "Choose",
        "response_schema": {"type": "string"},
        "resume_action": "retry_execution",
    }
    result.interaction_request = request
    with pytest.raises(ValueError, match="transactional persistence"):
        engine._validate_stage_interaction(result, NextAction.WAIT, "execution")
    engine.unit_of_work = SimpleNamespace(_validate_interaction_request=lambda _r: None)
    engine.checkpoint_store = object()
    result.status = ResultStatus.SUCCESS
    with pytest.raises(ValueError, match="WAITING result"):
        engine._validate_stage_interaction(result, NextAction.WAIT, "execution")
    result.status = ResultStatus.WAITING
    request["resume_action"] = "validate"
    with pytest.raises(ValueError, match="resume or replan"):
        engine._validate_stage_interaction(result, NextAction.WAIT, "execution")


def test_executor_interaction_fails_closed_without_persistence():
    context = ExecutionContext(task_id=1, task_title="Task", workspace="/workspace")
    plan = ExecutionPlan("Task", [PlanStep("step", "Do", "do")])
    result = EngineResult.waiting(
        "Need answer",
        WaitReason.HUMAN_INPUT,
        interaction_request={"kind": "human_decision", "prompt": "Choose"},
    )
    result.data["next_action"] = NextAction.WAIT
    engine = Orchestrator(
        SimpleNamespace(build=lambda _id: context),
        planner=SimpleNamespace(
            plan=lambda _context: EngineResult.success("plan", plan=plan)
        ),
        executor=SimpleNamespace(execute=lambda *_args: result),
        validator=SimpleNamespace(
            validate=lambda *_args: EngineResult.success("valid")
        ),
        persistence_mode=PersistenceMode.DISABLED,
    )
    outcome = engine.run(1)
    assert outcome.status is ResultStatus.FAILED
    assert "requires persistence" in outcome.message
