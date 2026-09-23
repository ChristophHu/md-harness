import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

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
    assert store.get_active(1)["reason"] == "approval"
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


def test_checkpoint_store_invalidates_extended_checkpoint():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT,
        context_data TEXT, created_at TEXT, resumed_at TEXT,
        invalidated_at TEXT, resume_count INTEGER DEFAULT 0)"""
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait", reason="approval")
    assert store.mark_resumed(1) is True
    assert store.claim_resume(1) is False
    assert store.invalidate(1, "cancelled") is True
    checkpoint = store.get(1)
    assert checkpoint["invalidated_at"] is not None
    assert checkpoint["reason"] == "cancelled"
    assert store.invalidate(1, "again") is False


def test_checkpoint_store_claims_legacy_checkpoint_once():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE task_checkpoints (id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT, next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT, context_data TEXT, created_at TEXT, resumed_at TEXT)"
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait")
    assert store.claim_resume(1) is True
    assert store.claim_resume(1) is False


def test_checkpoint_store_invalidates_legacy_checkpoint_by_deleting():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE task_checkpoints (id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT, next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT, context_data TEXT, created_at TEXT, resumed_at TEXT)"
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait")
    assert store.invalidate(1, "cancelled") is True
    assert store.get(1) is None


def test_checkpoint_store_leases_expire_and_can_be_reclaimed():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT,
        context_data TEXT, next_step_id TEXT, completed_step_ids TEXT,
        plan_fingerprint TEXT, created_at TEXT, resumed_at TEXT,
        execution_result TEXT, validation_result TEXT, resume_count INTEGER DEFAULT 0,
        invalidated_at TEXT, waiting_reason_code TEXT,
        resume_claim_token TEXT, resume_claim_expires_at TEXT)"""
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait", reason="approval", waiting_reason_code="approval")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        store.claim_resume_lease(1, token="first", lease_seconds=5, now=now) == "first"
    )
    assert store.claim_resume_lease(1, token="other", now=now) is None
    later = now + timedelta(seconds=6)
    assert store.claim_resume_lease(1, token="second", now=later) == "second"
    assert store.get(1)["resume_claim_token"] == "second"
    assert store.release_resume_lease(1, "wrong") is False
    assert store.release_resume_lease(1, "second") is True


def test_checkpoint_store_lease_rejects_bad_duration_and_invalidated_checkpoint():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT,
        context_data TEXT, next_step_id TEXT, completed_step_ids TEXT,
        plan_fingerprint TEXT, created_at TEXT, resumed_at TEXT,
        execution_result TEXT, validation_result TEXT, resume_count INTEGER DEFAULT 0,
        invalidated_at TEXT, waiting_reason_code TEXT,
        resume_claim_token TEXT, resume_claim_expires_at TEXT)"""
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait")
    with pytest.raises(ValueError, match="positive"):
        store.claim_resume_lease(1, lease_seconds=0)
    store.invalidate(1, "cancelled")
    assert store.claim_resume_lease(1) is None


def test_checkpoint_store_claim_resume_rejects_invalidated_extended_row():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, plan_version INTEGER, attempt_id INTEGER, reason TEXT,
        context_data TEXT, next_step_id TEXT, completed_step_ids TEXT,
        plan_fingerprint TEXT, created_at TEXT, resumed_at TEXT,
        execution_result TEXT, validation_result TEXT, resume_count INTEGER DEFAULT 0,
        invalidated_at TEXT)"""
    )
    store = CheckpointStore(connection)
    store.save(1, "waiting", "wait")
    store.invalidate(1)
    assert store.claim_resume(1) is False


def test_checkpoint_store_lease_without_lease_columns_returns_none():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, phase TEXT,
        next_action TEXT, resumed_at TEXT)"""
    )
    assert CheckpointStore(connection).claim_resume_lease(1) is None


def test_checkpoint_store_ensure_resumed_sets_timestamp():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE task_checkpoints (
        id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE, resumed_at TEXT)"""
    )
    connection.execute("INSERT INTO task_checkpoints (task_id) VALUES (1)")
    CheckpointStore(connection).ensure_resumed(1)
    assert connection.execute(
        "SELECT resumed_at FROM task_checkpoints WHERE task_id = 1"
    ).fetchone()[0]
