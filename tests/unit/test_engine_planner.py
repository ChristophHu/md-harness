"""Tests for deterministic planner behavior."""

from harness.engine.context import (
    AcceptanceCriterion,
    DependencyContext,
    ExecutionContext,
)
from harness.engine.context import (
    TestCriterion as Criterion,
)
from harness.engine.plan import ExecutionPlan
from harness.engine.planner import Planner
from harness.engine.result import ResultStatus


def context(**overrides):
    values = {
        "task_id": 1,
        "task_title": "Add healthcheck",
        "task_description": "Implement and test the SQLite healthcheck.",
        "approval_status": "approved",
        "acceptance_criteria": [AcceptanceCriterion(10, "Healthcheck works")],
        "test_criteria": [Criterion(20, "Run tests", command="pytest")],
        "available_tools": ["filesystem", "sqlite"],
    }
    values.update(overrides)
    return ExecutionContext(**values)


def test_planner_creates_structured_plan():
    result = Planner().plan(context())

    assert result.status is ResultStatus.SUCCESS
    plan = result.data["plan"]
    assert isinstance(plan, ExecutionPlan)
    assert plan.goal == "Add healthcheck"
    assert [step.id for step in plan.steps] == ["inspect", "implement", "validate"]
    assert plan.steps[1].acceptance_criteria == [10]
    assert plan.steps[2].test_criteria == [20]
    assert plan.steps[0].tool == "filesystem"
    assert plan.steps[2].tool == "sqlite"


def test_planner_waits_for_approval_or_external_context():
    result = Planner().plan(context(approval_status="pending"))

    assert result.status is ResultStatus.WAITING
    assert result.successful is False


def test_planner_waits_when_task_is_blocked():
    result = Planner().plan(context(task_status="waiting", approval_status="approved"))

    assert result.status is ResultStatus.WAITING


def test_planner_rejects_incomplete_context():
    missing_description = Planner().plan(context(task_description=""))
    missing_acceptance = Planner().plan(context(acceptance_criteria=[]))

    assert missing_description.status is ResultStatus.FAILED
    assert missing_description.errors == ["title_or_description_missing"]
    assert missing_acceptance.status is ResultStatus.FAILED
    assert missing_acceptance.errors == ["acceptance_criteria_missing"]


def test_planner_records_dependencies_and_previous_attempts():
    plan = (
        Planner()
        .plan(
            context(
                dependencies=[DependencyContext(2, "done", resolved=True)],
                previous_attempts=[{"status": "failed"}],
            )
        )
        .data["plan"]
    )

    assert "Task dependencies are resolved before execution." in plan.assumptions
    assert "Previous execution attempts exist and should be reviewed." in plan.risks


def test_planner_rejects_missing_implementation_tool():
    result = Planner().plan(context(available_tools=["sqlite"]))

    assert result.status is ResultStatus.FAILED
    assert result.errors == ["implementation_tool_missing"]


def test_planner_rejects_missing_validation_tool():
    result = Planner().plan(context(available_tools=["git"]))

    assert result.status is ResultStatus.FAILED
    assert result.errors == ["validation_tool_missing"]


def test_planner_waits_for_unresolved_blocking_dependency():
    result = Planner().plan(
        context(dependencies=[DependencyContext(2, "executing", resolved=False)])
    )

    assert result.status is ResultStatus.WAITING


def test_planner_versions_replans_and_uses_fallback_reason():
    result = Planner().plan(
        context(last_plan={"version": 2}, last_validation={"message": ""})
    )

    plan = result.data["plan"]
    assert plan.version == 3
    assert plan.replanned_from == 2
    assert plan.reason == "Previous validation was not successful."
