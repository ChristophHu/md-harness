import sqlite3

import pytest

from harness.storage.transaction import TransactionError, TransactionManager


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
