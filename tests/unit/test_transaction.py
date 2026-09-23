import sqlite3

import pytest

from harness.storage.transaction import (
    EngineUnitOfWork,
    TransactionError,
    TransactionManager,
)


class Store:
    def __init__(self, connection):
        self.connection = connection


def test_transaction_manager_commits_and_validates_shared_connection():
    connection = sqlite3.connect(":memory:")
    store = Store(connection)
    manager = TransactionManager(connection, store)
    with manager.atomic():
        connection.execute("CREATE TABLE values_table (value INTEGER)")
        connection.execute("INSERT INTO values_table VALUES (1)")
    assert connection.execute("SELECT value FROM values_table").fetchone()[0] == 1


def test_transaction_manager_rolls_back_on_error():
    connection = sqlite3.connect(":memory:")
    manager = TransactionManager(connection)
    connection.execute("CREATE TABLE values_table (value INTEGER)")
    with pytest.raises(RuntimeError):
        with manager.atomic():
            connection.execute("INSERT INTO values_table VALUES (1)")
            raise RuntimeError("boom")
    assert connection.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 0


def test_transaction_manager_rejects_different_connection():
    connection = sqlite3.connect(":memory:")
    with pytest.raises(TransactionError, match="share"):
        TransactionManager(connection, Store(sqlite3.connect(":memory:")))


def test_transaction_manager_ignores_optional_none_store():
    connection = sqlite3.connect(":memory:")
    TransactionManager(connection, None)


def test_record_failure_raises_when_failure_transaction_cannot_commit():
    connection = sqlite3.connect(":memory:")
    manager = TransactionManager(connection)

    class BrokenStore(Store):
        def transition(self, *_args):
            raise RuntimeError("cannot write")

    with pytest.raises(TransactionError, match="could not persist"):
        manager.record_failure(BrokenStore(connection), Store(connection), 1, "broken")


def test_record_failure_uses_failure_transaction():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("INSERT INTO tasks VALUES (1, 'executing')")

    class Tasks(Store):
        def transition(self, task_id, status):
            self.connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

    class Events(Store):
        def record(self, task_id, event_type, _payload):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    manager = TransactionManager(connection)
    manager.record_failure(Tasks(connection), Events(connection), 1, "broken")
    assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "failed"
    assert (
        connection.execute("SELECT event_type FROM events").fetchone()[0]
        == "task.persistence.failed"
    )


def test_engine_unit_of_work_groups_cycle_start_and_decision():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("CREATE TABLE attempts (task_id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("INSERT INTO tasks VALUES (1, 'ready')")

    class Tasks(Store):
        def transition(self, task_id, status):
            self.connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

        def record_attempt(self, task_id, status):
            self.connection.execute(
                "INSERT INTO attempts VALUES (?, ?)", (task_id, status)
            )
            return 1

    class Events(Store):
        def transition(self, *_args):
            return None

        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    tasks, events = Tasks(connection), Events(connection)
    work = EngineUnitOfWork(
        TransactionManager(connection), tasks, events, Store(connection)
    )
    work.cycle_start(1, "executing", "task.executing")
    work.decision(1, "waiting", "task.waiting")

    assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "waiting"
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_external_resolution_rejects_invalid_input_and_missing_store():
    connection = sqlite3.connect(":memory:")
    store = type("Store", (), {"connection": connection})()
    work = EngineUnitOfWork(TransactionManager(connection), store, store, store)
    with pytest.raises(ValueError, match="non-empty"):
        work.resolve_external_wait(1, "", "actor", "ref")
    with pytest.raises(TransactionError, match="checkpoint store"):
        work.resolve_external_wait(1, "token", "actor", "ref")


@pytest.mark.parametrize("case", ["missing", "unresolved_update"])
def test_external_resolution_store_race_failures(case):
    connection = sqlite3.connect(":memory:")
    store = type("Store", (), {"connection": connection})()

    class Tasks:
        def __init__(self):
            self.connection = connection

        def get(self, _task_id):
            return None if case == "missing" else {"status": "waiting"}

    class Checkpoints:
        def __init__(self):
            self.connection = connection

        def resolve_external_wait(self, *_args):
            return False

        def get_active(self, _task_id):
            return {
                "wait_token": "token",
                "waiting_reason_code": "external_information",
                "external_resolved_at": None,
            }

    work = EngineUnitOfWork(
        TransactionManager(connection), Tasks(), store, store, Checkpoints()
    )
    with pytest.raises(
        ValueError, match="active waiting checkpoint|could not be resolved"
    ):
        work.resolve_external_wait(1, "token", "actor", "ref")


def test_engine_unit_of_work_rolls_back_a_failed_phase():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")

    class Events(Store):
        def transition(self, *_args):
            return None

        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )
            raise RuntimeError("event failure")

    events = Events(connection)
    work = EngineUnitOfWork(TransactionManager(connection), events, events, events)
    with pytest.raises(RuntimeError):
        work.decision(1, "waiting", "task.waiting")
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_engine_unit_of_work_persists_execution_result_and_artifacts():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("CREATE TABLE artifacts (task_id INTEGER, path TEXT)")
    connection.execute("CREATE TABLE attempts (id INTEGER, status TEXT)")

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    class Artifacts(Store):
        def register(self, task_id, path, _kind):
            self.connection.execute(
                "INSERT INTO artifacts VALUES (?, ?)", (task_id, path)
            )

    class Tasks(Store):
        def complete_attempt(self, attempt_id, status):
            self.connection.execute(
                "INSERT INTO attempts VALUES (?, ?)", (attempt_id, status)
            )

    class Result:
        def __init__(self):
            self.artifacts = ["report.txt"]

    work = EngineUnitOfWork(
        TransactionManager(connection),
        Tasks(connection),
        Events(connection),
        Artifacts(connection),
    )
    work.execution_result(1, 4, Result())
    assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    assert (
        connection.execute("SELECT status FROM attempts").fetchone()[0] == "completed"
    )


def test_engine_unit_of_work_finishes_decision_atomically():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tasks (id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE attempts (id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("INSERT INTO tasks VALUES (1, 'executing')")

    class Tasks(Store):
        def complete_attempt(self, attempt_id, status):
            self.connection.execute(
                "UPDATE attempts SET status = ? WHERE id = ?", (status, attempt_id)
            )

        def transition(self, task_id, status):
            self.connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    connection.execute("INSERT INTO attempts VALUES (4, 'running')")
    work = EngineUnitOfWork(
        TransactionManager(connection),
        Tasks(connection),
        Events(connection),
        Store(connection),
    )
    work.finish_decision(1, 4, "completed", "done", "task.done")
    assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "done"


def test_engine_unit_of_work_resume_without_checkpoint_store_returns_false():
    connection = sqlite3.connect(":memory:")
    store = Store(connection)
    work = EngineUnitOfWork(TransactionManager(connection), store, store, store)
    assert work.resume(1, "wait") is False


def test_engine_unit_of_work_persists_execution_wait_atomically():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tasks (id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE attempts (id INTEGER, status TEXT, error TEXT)")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("CREATE TABLE checkpoints (task_id INTEGER, reason TEXT)")
    connection.execute("INSERT INTO tasks VALUES (1, 'executing')")
    connection.execute("INSERT INTO attempts VALUES (4, 'running', NULL)")

    class Tasks(Store):
        def complete_attempt(self, attempt_id, status, error=None):
            self.connection.execute(
                "UPDATE attempts SET status = ?, error = ? WHERE id = ?",
                (status, error, attempt_id),
            )

        def transition(self, task_id, status):
            self.connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    class Checkpoints(Store):
        def save(self, task_id, **payload):
            self.connection.execute(
                "INSERT INTO checkpoints VALUES (?, ?)", (task_id, payload["reason"])
            )

    work = EngineUnitOfWork(
        TransactionManager(connection),
        Tasks(connection),
        Events(connection),
        Store(connection),
        Checkpoints(connection),
    )
    work.execution_wait(
        1,
        4,
        {"reason": "approval", "phase": "waiting", "next_action": "wait"},
        None,
        "wait",
    )
    assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "waiting"
    assert connection.execute("SELECT status FROM attempts").fetchone()[0] == "waiting"
    assert (
        connection.execute("SELECT reason FROM checkpoints").fetchone()[0] == "approval"
    )


def test_engine_unit_of_work_claims_and_releases_resume_lease():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    class Checkpoints(Store):
        available = True

        def claim_resume_lease(self, _task_id, *, token, lease_seconds=300):
            assert lease_seconds in {17, 300}
            return token if self.available else None

        def release_resume_lease(self, _task_id, token):
            return token == "lease"

    events = Events(connection)
    checkpoints = Checkpoints(connection)
    work = EngineUnitOfWork(
        TransactionManager(connection), events, events, events, checkpoints
    )
    assert work.claim_resume_lease(1, "lease", 17) is True
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert work.release_resume_lease(1, "lease") is True
    checkpoints.available = False
    assert work.claim_resume_lease(1, "other") is False
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_engine_unit_of_work_resume_lease_supports_legacy_store():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    class Checkpoints(Store):
        def claim_resume(self, _task_id):
            return True

    events = Events(connection)
    work = EngineUnitOfWork(
        TransactionManager(connection), events, events, events, Checkpoints(connection)
    )
    assert work.claim_resume_lease(1, "legacy") is True


def test_engine_unit_of_work_resume_lease_without_checkpoint_store():
    connection = sqlite3.connect(":memory:")
    store = Store(connection)
    work = EngineUnitOfWork(TransactionManager(connection), store, store, store)
    assert work.claim_resume_lease(1, "lease") is False
    assert work.release_resume_lease(1, "lease") is False


def test_engine_unit_of_work_legacy_resume_and_start_cycle():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tasks (id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE attempts (task_id INTEGER, status TEXT)")
    connection.execute("CREATE TABLE events (task_id INTEGER, event_type TEXT)")
    connection.execute("INSERT INTO tasks VALUES (1, 'ready')")

    class Tasks(Store):
        def transition(self, task_id, status):
            self.connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

        def record_attempt(self, task_id, status):
            self.connection.execute(
                "INSERT INTO attempts VALUES (?, ?)", (task_id, status)
            )
            return 42

    class Events(Store):
        def record(self, task_id, event_type, _payload=None):
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?)", (task_id, event_type)
            )

    tasks, events = Tasks(connection), Events(connection)
    work = EngineUnitOfWork(
        TransactionManager(connection), tasks, events, Store(connection)
    )
    assert work.start_cycle(1, "planning", "task.planning") == 42

    class Checkpoints(Store):
        def mark_resumed(self, _task_id):
            return True

    work = EngineUnitOfWork(
        TransactionManager(connection),
        tasks,
        events,
        Store(connection),
        Checkpoints(connection),
    )
    assert work.resume(1, "replan") is True
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
