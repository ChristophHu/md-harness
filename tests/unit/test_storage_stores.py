import sqlite3

import pytest

from harness.storage.artifact_store import ArtifactStore
from harness.storage.database import (
    backup_database,
    healthcheck,
    initialize_database,
    restore_database,
    schema_version,
    transaction,
)
from harness.storage.event_store import EventStore
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore


def db(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    return path


def test_database_transaction_health_version_and_backup(tmp_path):
    path = db(tmp_path)
    assert healthcheck(path) is True
    assert schema_version(path) == 1
    with transaction(path) as connection:
        connection.execute("INSERT INTO projects (name, path) VALUES ('p', '/p')")
    with transaction(path) as connection:
        connection.execute("INSERT INTO projects (name, path) VALUES ('bad', '/bad')")
        raise_error = True
        try:
            connection.execute("INSERT INTO missing VALUES (1)")
        except sqlite3.Error:
            connection.rollback()
    backup = backup_database(path, tmp_path / "backup.sqlite")
    assert backup.exists()
    restored = restore_database(backup, tmp_path / "restored.sqlite")
    assert healthcheck(restored)


def test_project_and_task_stores_cover_task_lifecycle(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        projects = ProjectStore(connection)
        tasks = TaskStore(connection)
        project_id = projects.create("Project", "/workspace")
        assert projects.get(project_id)["name"] == "Project"
        assert len(projects.list()) == 1
        task_id = tasks.create("Task", project_id=project_id, priority="high")
        assert tasks.get(task_id)["title"] == "Task"
        with pytest.raises(ValueError):
            tasks.update(task_id)
        tasks.update(task_id, description="Description", priority="normal")
        with pytest.raises(ValueError):
            tasks.update(task_id, status="invalid")
        tasks.transition(task_id, "ready")
        tasks.transition(task_id, "planned")
        tasks.transition(task_id, "in_progress")
        tasks.transition(task_id, "review")
        tasks.transition(task_id, "completed")
        assert tasks.get(task_id)["completed_at"] is not None
        with pytest.raises(ValueError, match="invalid transition"):
            tasks.transition(task_id, "ready")
        with pytest.raises(ValueError, match="not found"):
            tasks.transition(999, "ready")


def test_task_dependencies_criteria_and_attempts(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        store = TaskStore(connection)
        first = store.create("First")
        second = store.create("Second")
        store.add_dependency(second, first)
        assert store.dependencies(second)[0]["depends_on_task_id"] == first
        criterion_id = store.add_criterion(second, "Tests pass")
        assert store.criteria(second)[0]["completed"] == 0
        store.complete_criterion(criterion_id)
        assert store.criteria(second)[0]["completed"] == 1
        assert store.record_attempt(second, "failed", "agent", "error") == 1


def test_event_and_artifact_stores(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        task_id = TaskStore(connection).create("Task")
        event_id = EventStore(connection).record(task_id, "started", {"source": "test"})
        assert EventStore(connection).list_for_task(task_id)[0]["id"] == event_id
        artifact_id = ArtifactStore(connection).register(task_id, "result.txt", "text", "abc")
        assert ArtifactStore(connection).list_for_task(task_id)[0]["id"] == artifact_id

