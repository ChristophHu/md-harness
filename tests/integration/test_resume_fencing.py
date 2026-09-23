"""Real SQLite ownership and checkpoint consistency regressions."""

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from harness.engine.context_builder import ContextBuilder
from harness.engine.orchestrator import Orchestrator
from harness.engine.plan import ExecutionPlan
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    ResultStatus,
)
from harness.storage.artifact_store import ArtifactStore
from harness.storage.checkpoint_store import CheckpointStore
from harness.storage.database import connect, initialize_database
from harness.storage.event_store import EventStore
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore
from harness.storage.transaction import (
    EngineUnitOfWork,
    TransactionError,
    TransactionManager,
)


def stores(connection):
    tasks = TaskStore(connection)
    events = EventStore(connection)
    artifacts = ArtifactStore(connection)
    checkpoints = CheckpointStore(connection)
    manager = TransactionManager(connection, tasks, events, artifacts, checkpoints)
    return (
        tasks,
        events,
        checkpoints,
        manager,
        EngineUnitOfWork(manager, tasks, events, artifacts, checkpoints),
    )


def waiting_task(connection):
    tasks, _, checkpoints, _, _ = stores(connection)
    task_id = tasks.create("fenced task")
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "test")
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "executing")
    attempt_id = tasks.record_attempt(task_id, "running")
    tasks.complete_attempt(attempt_id, "waiting")
    tasks.transition(task_id, "waiting")
    checkpoints.save(
        task_id,
        "waiting",
        "wait",
        attempt_id=attempt_id,
        reason="need data",
        waiting_reason_code="external_information",
    )
    connection.commit()
    return task_id, attempt_id


def claim(work, checkpoints, task_id, token="owner"):
    checkpoint = checkpoints.get_active(task_id)
    return work.claim_resume_lease(
        task_id,
        token,
        60,
        expected_revision=checkpoint["revision"],
        expected_action=checkpoint["next_action"],
        expected_attempt_id=checkpoint["attempt_id"],
    )


def test_expired_resume_claim_fences_old_worker_and_preserves_new_writes(tmp_path):
    path = initialize_database(tmp_path / "fence.sqlite")
    first, second = connect(path), connect(path)
    task_id, _ = waiting_task(first)
    _, events_a, checkpoints_a, manager_a, work_a = stores(first)
    _, events_b, checkpoints_b, manager_b, work_b = stores(second)
    assert claim(work_a, checkpoints_a, task_id)
    first.execute(
        "UPDATE tasks SET claim_expires_at = '2000-01-01' WHERE id = ?", (task_id,)
    )
    first.execute(
        "UPDATE task_checkpoints SET resume_claim_expires_at = '2000-01-01' WHERE task_id = ?",
        (task_id,),
    )
    first.commit()
    assert claim(work_b, checkpoints_b, task_id, "new-owner")
    with pytest.raises(TransactionError, match="claim was lost"):
        with work_a.phase():
            events_a.record(task_id, "stale.write")
    with work_b.phase():
        events_b.record(task_id, "new.write")
    assert (
        first.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'stale.write'"
        ).fetchone()[0]
        == 0
    )
    assert (
        second.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'new.write'"
        ).fetchone()[0]
        == 1
    )
    assert manager_a.owner.epoch < manager_b.owner.epoch
    assert work_a.release_resume_lease(task_id, "owner") is False
    assert work_b.release_resume_lease(task_id, "new-owner") is True
    first.close()
    second.close()


def test_cancel_revokes_active_resumer_before_later_persistence(tmp_path):
    path = initialize_database(tmp_path / "cancel.sqlite")
    first, second = connect(path), connect(path)
    task_id, _ = waiting_task(first)
    _, events, checkpoints, manager, work = stores(first)
    _, _, _, _, canceller = stores(second)
    assert claim(work, checkpoints, task_id)
    canceller.cancel(task_id, "stop")
    with pytest.raises(TransactionError, match="claim was lost"):
        with work.phase():
            events.record(task_id, "late.write")
    assert (
        first.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'late.write'"
        ).fetchone()[0]
        == 0
    )
    assert manager.owner is not None
    assert work.release_resume_lease(task_id, "owner") is False
    first.close()
    second.close()


def test_cancel_on_same_manager_preserves_guard_until_worker_stops(tmp_path):
    path = initialize_database(tmp_path / "same-manager.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, checkpoints, manager, work = stores(connection)
    assert claim(work, checkpoints, task_id)
    original_owner = manager.owner
    work.cancel(task_id, "stop now")
    assert manager.owner == original_owner
    with pytest.raises(TransactionError, match="claim was lost"):
        with work.phase():
            pass
    work.release_resume_lease(task_id, "owner")
    connection.close()


def test_cancel_during_execution_prevents_followup_writes(tmp_path):
    path = initialize_database(tmp_path / "cancel-during-tool.sqlite")
    first, second = connect(path), connect(path)
    tasks, events, checkpoints, manager, _ = stores(first)
    _, _, _, _, canceller = stores(second)
    task_id = tasks.create("cancel while executing")
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "test")
    first.commit()

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan=ExecutionPlan("goal"))

    class Executor:
        def execute(self, _context, _plan):
            canceller.cancel(task_id, "cancelled inside tool")
            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, *_args):
            pytest.fail("validator must not run after cancellation")

    engine = Orchestrator(
        ContextBuilder(
            tasks,
            events,
            ArtifactStore(first),
            ProjectStore(first),
            checkpoint_store=checkpoints,
        ),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=tasks,
        event_store=events,
        artifact_store=ArtifactStore(first),
        checkpoint_store=checkpoints,
        transaction_manager=manager,
    )
    result = engine.run(task_id)
    assert result.status is ResultStatus.FAILED
    assert "cancelled" in result.message
    assert tasks.get(task_id)["status"] == "cancelled"
    assert checkpoints.get_active(task_id) is None
    assert tasks.attempts(task_id)[0]["status"] == "cancelled"
    assert (
        first.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'task.execution.completed'"
        ).fetchone()[0]
        == 0
    )
    first.close()
    second.close()


def test_checkpoint_lease_loss_rolls_back_task_renewal(tmp_path):
    path = initialize_database(tmp_path / "checkpoint-lease.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, checkpoints, manager, work = stores(connection)
    assert claim(work, checkpoints, task_id)
    connection.execute(
        "UPDATE task_checkpoints SET resume_claim_token = 'replacement' WHERE task_id = ?",
        (task_id,),
    )
    connection.commit()
    with pytest.raises(TransactionError, match="checkpoint claim was lost"):
        with work.phase():
            pass
    assert manager.owner is not None
    work.release_resume_lease(task_id, "owner")
    connection.close()


def test_heartbeat_keeps_long_running_resume_exclusive(tmp_path):
    path = initialize_database(tmp_path / "heartbeat.sqlite")
    first, second = connect(path), connect(path)
    task_id, _ = waiting_task(first)
    _, _, _, manager, work_a = stores(first)
    _, _, checkpoints_b, _, work_b = stores(second)
    assert work_a.claim_resume_lease(task_id, "owner", 1)
    with manager.keep_lease_alive():
        time.sleep(1.4)
        assert not claim(work_b, checkpoints_b, task_id, "other")
        with work_a.phase():
            pass
    work_a.release_resume_lease(task_id, "owner")
    first.close()
    second.close()


@pytest.mark.parametrize("loss", ["task", "checkpoint", "database"])
def test_heartbeat_reports_lost_ownership(tmp_path, monkeypatch, loss):
    path = initialize_database(tmp_path / f"heartbeat-{loss}.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, _, manager, work = stores(connection)
    assert work.claim_resume_lease(task_id, "owner", 1)
    if loss == "database":

        def broken_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("database unavailable")

        monkeypatch.setattr(
            "harness.storage.transaction.sqlite3.connect", broken_connect
        )
    with pytest.raises(TransactionError, match="lease renewal failed"):
        with manager.keep_lease_alive():
            if loss == "task":
                work.cancel(task_id, "stop")
            elif loss == "checkpoint":
                connection.execute(
                    "UPDATE task_checkpoints SET resume_claim_token = 'other' WHERE task_id = ?",
                    (task_id,),
                )
                connection.commit()
            time.sleep(0.7)
    work.release_resume_lease(task_id, "owner")
    connection.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", 99),
        ("expected_action", "replan"),
        ("expected_attempt_id", 999),
    ],
)
def test_resume_rejects_stale_checkpoint_snapshot(tmp_path, field, value):
    path = initialize_database(tmp_path / "snapshot.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, checkpoints, manager, work = stores(connection)
    checkpoint = checkpoints.get_active(task_id)
    expected = {
        "expected_revision": checkpoint["revision"],
        "expected_action": "wait",
        "expected_attempt_id": checkpoint["attempt_id"],
    }
    expected[field] = value
    assert work.claim_resume_lease(task_id, "stale", **expected) is False
    assert manager.owner is None
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'task.resumed'"
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_resume_rejects_status_and_attempt_mismatch(tmp_path):
    path = initialize_database(tmp_path / "mismatch.sqlite")
    connection = connect(path)
    task_id, attempt_id = waiting_task(connection)
    tasks, _, checkpoints, _, work = stores(connection)
    tasks.transition(task_id, "planning")
    connection.commit()
    assert not claim(work, checkpoints, task_id)
    tasks.transition(task_id, "waiting")
    connection.execute(
        "UPDATE task_attempts SET status = 'running' WHERE id = ?", (attempt_id,)
    )
    connection.commit()
    assert not claim(work, checkpoints, task_id)
    other_task = tasks.create("other")
    connection.execute(
        "UPDATE task_attempts SET task_id = ?, status = 'waiting' WHERE id = ?",
        (other_task, attempt_id),
    )
    connection.commit()
    assert not claim(work, checkpoints, task_id)
    connection.close()


def test_resume_rejects_missing_active_checkpoint(tmp_path):
    path = initialize_database(tmp_path / "retired.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, checkpoints, _, work = stores(connection)
    checkpoints.invalidate(task_id)
    connection.commit()
    assert not work.claim_resume_lease(task_id, "owner")
    connection.close()


def test_resume_requires_checkpoint_reader_for_fenced_task(tmp_path):
    path = initialize_database(tmp_path / "no-reader.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    tasks, events, _, manager, _ = stores(connection)

    class LegacyCheckpoints:
        def __init__(self, connection):
            self.connection = connection

    work = EngineUnitOfWork(
        manager, tasks, events, ArtifactStore(connection), LegacyCheckpoints(connection)
    )
    assert work.claim_resume_lease(task_id, "owner") is False
    connection.close()


def test_resume_rolls_back_claim_when_task_fencing_cas_fails(tmp_path):
    path = initialize_database(tmp_path / "cas.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    _, _, checkpoints, _, work = stores(connection)
    connection.execute(
        """CREATE TRIGGER ignore_owner_claim BEFORE UPDATE OF claim_token ON tasks
           WHEN NEW.claim_token = 'owner' BEGIN SELECT RAISE(IGNORE); END"""
    )
    connection.commit()
    with pytest.raises(TransactionError, match="changed during resume claim"):
        claim(work, checkpoints, task_id)
    assert checkpoints.get_active(task_id)["resume_claim_token"] is None
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'task.resumed'"
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_active_task_lease_blocks_resume_until_expiry_then_interrupts_attempt(tmp_path):
    path = initialize_database(tmp_path / "takeover.sqlite")
    connection = connect(path)
    task_id, _ = waiting_task(connection)
    tasks, _, checkpoints, _, work = stores(connection)
    tasks.transition(task_id, "executing")
    interrupted = tasks.record_attempt(task_id, "running")
    future = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        "UPDATE tasks SET claim_token = 'old', claim_expires_at = ? WHERE id = ?",
        (future, task_id),
    )
    connection.commit()
    assert not claim(work, checkpoints, task_id)
    assert tasks.attempts(task_id)[-1]["status"] == "running"
    connection.execute(
        "UPDATE tasks SET claim_expires_at = '2000-01-01' WHERE id = ?", (task_id,)
    )
    connection.commit()
    assert claim(work, checkpoints, task_id)
    assert (
        connection.execute(
            "SELECT status FROM task_attempts WHERE id = ?", (interrupted,)
        ).fetchone()[0]
        == "failed"
    )
    work.release_resume_lease(task_id, "owner")
    connection.close()
