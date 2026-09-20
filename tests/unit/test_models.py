from datetime import datetime

from harness.models import (
    Project,
    Task,
    TaskAcceptanceCriterion,
    TaskArtifact,
    TaskAttempt,
    TaskDependency,
    TaskEvent,
)


def test_project_model_defaults():
    project = Project(None, "demo", "/tmp/demo")
    assert project.id is None
    assert project.name == "demo"
    assert project.created_at is None


def test_task_model_defaults():
    task = Task(None, 1, None, "TASK-1", "Implement feature")
    assert task.task_type == "task"
    assert task.status == "created"
    assert task.priority == "normal"
    assert task.description is None


def test_task_model_accepts_all_fields():
    now = datetime.now()
    task = Task(1, 2, 3, "TASK-1", "Title", "Description", "bugfix", "ready", "high", now, now, now, now)
    assert task.id == 1
    assert task.project_id == 2
    assert task.parent_id == 3
    assert task.status == "ready"
    assert task.completed_at == now


def test_task_dependency_default_type():
    assert TaskDependency(1, 2).dependency_type == "blocks"


def test_acceptance_criterion_defaults_to_incomplete():
    criterion = TaskAcceptanceCriterion(None, 1, "Tests pass")
    assert criterion.completed is False


def test_attempt_event_and_artifact_models():
    attempt = TaskAttempt(None, 1, "developer", "running")
    event = TaskEvent(None, 1, "started")
    artifact = TaskArtifact(None, 1, "output.txt")
    assert attempt.status == "running"
    assert event.payload is None
    assert artifact.checksum is None
