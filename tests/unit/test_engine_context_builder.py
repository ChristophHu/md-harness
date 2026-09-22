"""Tests for building execution contexts from persistence and runtime services."""

from types import SimpleNamespace

import pytest

from harness.engine.context_builder import ContextBuilder
from harness.storage.artifact_store import ArtifactStore
from harness.storage.database import connect, initialize_database
from harness.storage.event_store import EventStore
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore


def test_context_builder_loads_all_context_sources(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        tasks = TaskStore(connection)
        projects = ProjectStore(connection)
        events = EventStore(connection)
        artifacts = ArtifactStore(connection)
        project_id = projects.create("Project", "/workspace/project")
        task_id = tasks.create(
            "Implement feature",
            project_id=project_id,
            description="Build the feature",
            task_type="feature",
            priority="high",
            assigned_agent="developer",
        )
        dependency_id = tasks.create("Dependency")
        acceptance_id = tasks.add_criterion(task_id, "Feature works")
        tasks.complete_criterion(acceptance_id)
        test_id = tasks.add_test_criterion(
            task_id, "Run tests", test_type="pytest", command="pytest"
        )
        tasks.add_dependency(task_id, dependency_id)
        tasks.record_attempt(task_id, "failed", "developer", "first failure")
        events.record(task_id, "task.created", {"source": "test"})
        artifacts.register(task_id, "result.txt", "report", "abc")

        registry = SimpleNamespace(
            list=lambda: [
                SimpleNamespace(name="filesystem"),
                SimpleNamespace(name="git"),
            ]
        )
        builder = ContextBuilder(
            tasks,
            events,
            artifacts,
            projects,
            knowledge_loader=lambda current_id: [f"task-{current_id}.md"],
            tool_registry=registry,
            dry_run=True,
        )

        context = builder.build(task_id)

    assert context.task_title == "Implement feature"
    assert context.task_description == "Build the feature"
    assert context.task_type == "feature"
    assert context.priority == "high"
    assert context.workspace == "/workspace/project"
    assert context.dry_run is True
    assert context.acceptance_criteria[0].id == acceptance_id
    assert context.acceptance_criteria[0].completed is True
    assert context.test_criteria[0].id == test_id
    assert context.test_criteria[0].command == "pytest"
    assert context.dependencies[0].task_id == dependency_id
    assert context.dependencies[0].status == "created"
    assert context.dependencies[0].resolved is False
    assert context.previous_events[0]["event_type"] == "task.created"
    assert context.previous_attempts[0]["error_message"] == "first failure"
    assert context.artifacts[0]["path"] == "result.txt"
    assert context.knowledge_documents == [f"task-{task_id}.md"]
    assert context.available_tools == ["filesystem", "git"]


def test_context_builder_handles_missing_optional_sources(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        tasks = TaskStore(connection)
        projects = ProjectStore(connection)
        task_id = tasks.create("Standalone task")
        context = ContextBuilder(
            tasks,
            EventStore(connection),
            ArtifactStore(connection),
            projects,
        ).build(task_id)

    assert context.workspace is None
    assert context.knowledge_documents == []
    assert context.available_tools == []


def test_context_builder_rejects_unknown_task(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        builder = ContextBuilder(
            TaskStore(connection),
            EventStore(connection),
            ArtifactStore(connection),
            ProjectStore(connection),
        )
        with pytest.raises(ValueError, match="task not found: 99"):
            builder.build(99)


def test_context_builder_loads_replanning_feedback(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        tasks = TaskStore(connection)
        task_id = tasks.create("Task")
        events = EventStore(connection)
        events.record(task_id, "task.plan.created", {"version": 1})
        events.record(task_id, "task.validation.completed", {"message": "failed"})
        events.record(task_id, "task.replanning", {"reason": "missing test"})
        events.record(task_id, "raw", "not-json")
        events.record(task_id, "ignored", ["not a mapping"])
        context = ContextBuilder(
            tasks, events, ArtifactStore(connection), ProjectStore(connection)
        ).build(task_id)

    assert context.last_plan == {"version": 1}
    assert context.last_validation == {"message": "failed"}
    assert context.replanning_reasons == [{"reason": "missing test"}]
