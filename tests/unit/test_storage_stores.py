import sqlite3
from pathlib import Path

import pytest

from harness.storage.agent_store import AgentStore
from harness.storage.artifact_store import ArtifactStore
from harness.storage.database import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_PATH,
    apply_migrations,
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
    assert schema_version(path) == CURRENT_SCHEMA_VERSION == 5
    with transaction(path) as connection:
        connection.execute("INSERT INTO projects (name, path) VALUES ('p', '/p')")
    with transaction(path) as connection:
        connection.execute("INSERT INTO projects (name, path) VALUES ('bad', '/bad')")
        try:
            connection.execute("INSERT INTO missing VALUES (1)")
        except sqlite3.Error:
            connection.rollback()
    backup = backup_database(path, tmp_path / "backup.sqlite")
    assert backup.exists()
    restored = restore_database(backup, tmp_path / "restored.sqlite")
    assert healthcheck(restored)


def test_schema_file_and_migration_are_applied(tmp_path):
    path = db(tmp_path)
    assert SCHEMA_PATH.exists()
    assert schema_version(path) == CURRENT_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_name'"
            ).fetchone()[0]
            == "md-harness"
        )
        assert apply_migrations(connection) == CURRENT_SCHEMA_VERSION


def test_sqlite_seed_contains_three_tasks(tmp_path):
    path = db(tmp_path)
    seed = Path(__file__).parents[1] / "fixtures" / "sqlite_seed.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(seed.read_text(encoding="utf-8"))
        tasks = connection.execute(
            "SELECT external_key, status FROM tasks ORDER BY id"
        ).fetchall()
        agent = connection.execute("SELECT name FROM agents WHERE id = 200").fetchone()[
            0
        ]
        assignment_count = connection.execute(
            "SELECT COUNT(*) FROM task_assignments"
        ).fetchone()[0]
    assert tasks[:3] == [
        ("FIX-001", "done"),
        ("FIX-002", "ready"),
        ("FIX-003", "created"),
    ]
    assert tasks[3] == ("FIX-004", "ready")
    assert agent == "developer"
    assert assignment_count == 1


def test_transaction_rolls_back_on_sqlite_error(tmp_path):
    path = db(tmp_path)
    with pytest.raises(sqlite3.Error):
        with transaction(path) as connection:
            connection.execute(
                "INSERT INTO projects (name, path) VALUES ('temporary', '/tmp')"
            )
            connection.execute("INSERT INTO missing_table VALUES (1)")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_healthcheck_returns_false_for_invalid_database_path(tmp_path):
    invalid_path = tmp_path / "database-directory"
    invalid_path.mkdir()
    assert healthcheck(invalid_path) is False


def test_project_and_task_stores_cover_task_lifecycle(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        projects = ProjectStore(connection)
        tasks = TaskStore(connection)
        project_id = projects.create("Project", "/workspace")
        assert projects.get(project_id)["name"] == "Project"
        assert len(projects.list()) == 1
        child_id = projects.create(
            "Child",
            "/workspace/child",
            description="Child project",
            parent_id=project_id,
        )
        assert projects.children(project_id)[0]["id"] == child_id
        task_id = tasks.create("Task", project_id=project_id, priority="high")
        assert tasks.get(task_id)["title"] == "Task"
        with pytest.raises(ValueError, match="unsupported"):
            tasks.create("Invalid", status="ready")
        with pytest.raises(ValueError):
            tasks.update(task_id)
        tasks.update(task_id, description="Description", priority="normal")
        with pytest.raises(ValueError):
            tasks.update(task_id, status="invalid")
        tasks.transition(task_id, "ready")
        tasks.approve(task_id, "reviewer")
        tasks.transition(task_id, "planning")
        assert tasks.get(task_id)["planning_started_at"] is not None
        tasks.transition(task_id, "executing")
        tasks.transition(task_id, "validating")
        assert tasks.get(task_id)["validation_started_at"] is not None
        tasks.transition(task_id, "done")
        assert tasks.get(task_id)["completed_at"] is not None
        with pytest.raises(ValueError, match="invalid transition"):
            tasks.transition(task_id, "ready")
        with pytest.raises(ValueError, match="not found"):
            tasks.transition(999, "ready")


def test_failed_transition_records_failure_timestamp(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        tasks = TaskStore(connection)
        task_id = tasks.create("Task")
        tasks.transition(task_id, "ready")
        tasks.transition(task_id, "failed")
        assert tasks.get(task_id)["failed_at"] is not None


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
        store.complete_attempt(1, "completed")
        assert store.attempts(second)[0]["status"] == "completed"


def test_list_ready_returns_only_ready_tasks(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        store = TaskStore(connection)
        ready = store.create("Ready", priority="high")
        other = store.create("Other")
        store.transition(ready, "ready")
        assert [row["id"] for row in store.list_ready()] == [ready]
        assert other not in [row["id"] for row in store.list_ready()]


def test_approval_test_criteria_and_executable_tasks(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        store = TaskStore(connection)
        dependency = store.create("Dependency")
        task = store.create("Executable", assigned_agent="developer")
        store.transition(task, "ready")
        store.add_dependency(task, dependency)
        store.approve(task, "reviewer", "scope confirmed")
        assert store.get_executable_tasks("developer") == []
        store.transition(dependency, "ready")
        store.approve(dependency, "reviewer")
        store.transition(dependency, "planning")
        store.transition(dependency, "executing")
        store.transition(dependency, "validating")
        store.transition(dependency, "done")
        assert store.get_executable_tasks("developer")[0]["id"] == task
        criterion = store.add_test_criterion(
            task, "Coverage is sufficient", command="pytest --cov"
        )
        assert store.test_criteria(task)[0]["id"] == criterion
        store.revoke_approval(task, "reviewer", "needs changes")
        assert store.get(task)["approval_status"] == "revoked"


def test_approval_rejects_unknown_task(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        store = TaskStore(connection)
        with pytest.raises(ValueError, match="not found"):
            store.approve(999, "reviewer")


def test_dependencies_reject_self_and_cycles(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        store = TaskStore(connection)
        first = store.create("First")
        second = store.create("Second")
        store.add_dependency(second, first)
        assert store._depends_on(first, second, {first}) is False
        with pytest.raises(ValueError, match="itself"):
            store.add_dependency(first, first)
        with pytest.raises(ValueError, match="cycle"):
            store.add_dependency(first, second)


def test_unapproved_task_cannot_start(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        store = TaskStore(connection)
        task = store.create("Task")
        store.transition(task, "ready")
        with pytest.raises(ValueError, match="approved"):
            store.transition(task, "planning")


def test_agent_store_and_foreign_keys(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        tasks = TaskStore(connection)
        agents = AgentStore(connection)
        task = tasks.create("Task")
        agent = agents.create(
            "developer", description="Builds code", capabilities='["python"]'
        )
        assignment = agents.assign(task, agent)
        assert agents.get(agent)["name"] == "developer"
        assert agents.assignments(task)[0]["id"] == assignment
        with pytest.raises(sqlite3.IntegrityError):
            agents.create("developer")
        with pytest.raises(sqlite3.IntegrityError):
            agents.assign(task, 999)


def test_cascades_and_constraints(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        task = TaskStore(connection).create("Task", external_key="UNIQUE-1")
        with pytest.raises(sqlite3.IntegrityError):
            TaskStore(connection).create("Duplicate", external_key="UNIQUE-1")
        TaskStore(connection).add_test_criterion(task, "same")
        with pytest.raises(sqlite3.IntegrityError):
            TaskStore(connection).add_test_criterion(task, "same")
        connection.execute("DELETE FROM tasks WHERE id = ?", (task,))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM task_test_criteria WHERE task_id = ?", (task,)
            ).fetchone()[0]
            == 0
        )


def test_event_and_artifact_stores(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        task_id = TaskStore(connection).create("Task")
        event_id = EventStore(connection).record(task_id, "started", {"source": "test"})
        assert EventStore(connection).list_for_task(task_id)[0]["id"] == event_id
        artifact_id = ArtifactStore(connection).register(
            task_id, "result.txt", "text", "abc"
        )
        assert ArtifactStore(connection).list_for_task(task_id)[0]["id"] == artifact_id


def test_event_payload_variants_and_missing_store_records(tmp_path):
    path = db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        task_id = TaskStore(connection).create("Task")
        events = EventStore(connection)
        events.record(task_id, "plain", "text")
        events.record(task_id, "empty", None)
        rows = events.list_for_task(task_id)
        assert rows[0]["payload"] == "text"
        assert rows[1]["payload"] is None
        assert ProjectStore(connection).get(999) is None
        assert AgentStore(connection).get(999) is None
        assert ArtifactStore(connection).list_for_task(999) == []
