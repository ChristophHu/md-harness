import sqlite3

from harness.storage.checkpoint_store import CheckpointStore


def test_checkpoint_store_saves_loads_and_resumes():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE task_checkpoints (id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT, next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT, context_data TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, resumed_at TEXT)"
    )
    store = CheckpointStore(connection)
    store.save(
        1,
        "waiting",
        "wait",
        plan_version=2,
        attempt_id=3,
        reason="approval",
        context_data={"x": 1},
    )
    assert store.get(1)["reason"] == "approval"
    assert '"x": 1' in store.get(1)["context_data"]
    store.mark_resumed(1)
    assert store.get(1)["resumed_at"] is not None
    store.save(1, "waiting", "wait", reason="new")
    assert store.get(1)["resumed_at"] is None


def test_checkpoint_store_returns_none_for_unknown_task():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE task_checkpoints (id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT, next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT, context_data TEXT, created_at TEXT, resumed_at TEXT)"
    )
    assert CheckpointStore(connection).get(99) is None


def test_checkpoint_store_supports_legacy_schema_on_save():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE task_checkpoints (id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT, next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT, context_data TEXT, created_at TEXT, resumed_at TEXT)"
    )
    store = CheckpointStore(connection)
    assert store.save(1, "waiting", "wait", reason="legacy") == 1
    assert store.get(1)["reason"] == "legacy"


def test_checkpoint_store_supports_intermediate_schema_on_save():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT,
        context_data TEXT, next_step_id TEXT, completed_step_ids TEXT,
        plan_fingerprint TEXT, created_at TEXT, resumed_at TEXT)"""
    )
    store = CheckpointStore(connection)
    assert store.save(1, "waiting", "wait", reason="intermediate") == 1
    assert store.get(1)["reason"] == "intermediate"
