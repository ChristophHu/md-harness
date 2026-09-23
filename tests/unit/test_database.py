import sqlite3

import pytest

from harness.storage.database import (
    CURRENT_SCHEMA_VERSION,
    connect,
    initialize_database,
)

EXPECTED_TABLES = {
    "projects",
    "tasks",
    "task_dependencies",
    "task_acceptance_criteria",
    "task_test_criteria",
    "task_approvals",
    "agents",
    "task_assignments",
    "task_attempts",
    "task_events",
    "task_artifacts",
}


def test_initialize_database_creates_parent_directory_and_file(tmp_path):
    path = tmp_path / "nested" / "harness.sqlite"
    assert initialize_database(path) == path
    assert path.exists()


def test_current_schema_version_is_five(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
            == 11
        )


def test_initialize_database_creates_all_tables(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert EXPECTED_TABLES <= tables


def test_initialize_database_is_idempotent(tmp_path):
    path = tmp_path / "harness.sqlite"
    initialize_database(path)
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_connect_enables_foreign_keys_and_row_factory(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert isinstance(
            connection.execute("SELECT 1 AS value").fetchone(), sqlite3.Row
        )


def test_task_dependency_foreign_keys_and_cascade(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute("INSERT INTO projects (name, path) VALUES ('p', '/tmp/p')")
        connection.execute("INSERT INTO tasks (project_id, title) VALUES (1, 'one')")
        connection.execute("INSERT INTO tasks (project_id, title) VALUES (1, 'two')")
        connection.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (2, 1)"
        )
        connection.execute("DELETE FROM tasks WHERE id = 1")
        assert (
            connection.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
            == 0
        )


def test_database_rejects_invalid_acceptance_criterion(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute("INSERT INTO tasks (title) VALUES ('task')")
        try:
            connection.execute(
                "INSERT INTO task_acceptance_criteria (task_id, criterion, completed) VALUES (1, 'x', 2)"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("invalid completed value was accepted")


def test_database_rejects_invalid_status_and_approval_values(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks (title, status) VALUES ('task', 'unknown')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks (title, approval_status) VALUES ('task', 'unknown')"
            )


def test_task_cascade_removes_related_records(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute("INSERT INTO tasks (title) VALUES ('task')")
        connection.execute(
            "INSERT INTO task_approvals (task_id, status) VALUES (1, 'approved')"
        )
        connection.execute(
            "INSERT INTO task_test_criteria (task_id, criterion) VALUES (1, 'test')"
        )
        connection.execute("DELETE FROM tasks WHERE id = 1")
        assert (
            connection.execute("SELECT COUNT(*) FROM task_approvals").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM task_test_criteria").fetchone()[0]
            == 0
        )
