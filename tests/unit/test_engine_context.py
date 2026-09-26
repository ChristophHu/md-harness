"""Tests for the shared engine execution context."""

from harness.engine.context import (
    AcceptanceCriterion,
    DependencyContext,
    ExecutionContext,
)
from harness.engine.context import (
    TestCriterion as Criterion,
)


def test_execution_context_defaults_and_mutations() -> None:
    context = ExecutionContext(task_id=7, task_title="Add healthcheck")

    assert context.task_id == 7
    assert context.task_title == "Add healthcheck"
    assert context.task_description == ""
    assert context.task_type == "task"
    assert context.priority == "normal"
    assert context.task_status == "created"
    assert context.approval_status == "pending"
    assert context.assigned_agent is None
    assert context.acceptance_criteria == []
    assert context.test_criteria == []
    assert context.dependencies == []
    assert context.knowledge_documents == []
    assert context.previous_events == []
    assert context.previous_attempts == []
    assert context.artifacts == []
    assert context.available_tools == []
    assert context.workspace is None
    assert context.dry_run is False
    assert context.metadata == {}

    context.add_knowledge_document("architecture.md")
    context.add_knowledge_document("architecture.md")
    context.add_event({"type": "planned"})
    context.add_attempt({"status": "failed"})
    context.add_artifact({"path": "result.txt"})

    assert context.knowledge_documents == ["architecture.md"]
    assert context.previous_events == [{"type": "planned"}]
    assert context.previous_attempts == [{"status": "failed"}]
    assert context.artifacts == [{"path": "result.txt"}]


def test_execution_context_accepts_full_runtime_context() -> None:
    context = ExecutionContext(
        task_id=8,
        task_title="Run tests",
        task_description="Execute the test suite",
        task_type="feature",
        priority="high",
        task_status="planning",
        approval_status="approved",
        assigned_agent="developer",
        acceptance_criteria=[AcceptanceCriterion(1, "All tests pass")],
        test_criteria=[Criterion(2, "Run pytest", command="pytest")],
        dependencies=[DependencyContext(3, "done", resolved=True)],
        knowledge_documents=["testing.md"],
        previous_events=[{"type": "created"}],
        previous_attempts=[{"status": "failed"}],
        artifacts=[{"path": "old-result.txt"}],
        available_tools=["filesystem", "sqlite"],
        workspace="/workspace",
        dry_run=True,
        metadata={"attempt": 1},
    )

    assert context.task_description == "Execute the test suite"
    assert context.task_type == "feature"
    assert context.priority == "high"
    assert context.task_status == "planning"
    assert context.approval_status == "approved"
    assert context.assigned_agent == "developer"
    assert context.acceptance_criteria == [AcceptanceCriterion(1, "All tests pass")]
    assert context.test_criteria == [Criterion(2, "Run pytest", command="pytest")]
    assert context.dependencies == [DependencyContext(3, "done", resolved=True)]
    assert context.available_tools == ["filesystem", "sqlite"]
    assert context.workspace == "/workspace"
    assert context.dry_run is True
    assert context.metadata == {"attempt": 1}
