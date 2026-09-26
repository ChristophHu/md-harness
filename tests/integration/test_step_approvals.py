"""Integration coverage for plan- and token-bound approval decisions."""

import pytest

from harness.engine.plan import ExecutionPlan, PlanStep
from harness.security.approval import fingerprint, request_for_step
from harness.storage.approval_store import ApprovalStore
from harness.storage.artifact_store import ArtifactStore
from harness.storage.checkpoint_store import CheckpointStore
from harness.storage.database import connect, initialize_database
from harness.storage.event_store import EventStore
from harness.storage.task_store import TaskStore
from harness.storage.transaction import (
    EngineUnitOfWork,
    PhaseDecision,
    TransactionError,
    TransactionManager,
)


def setup_task(tmp_path):
    connection = connect(initialize_database(tmp_path / "approval.sqlite"))
    tasks = TaskStore(connection)
    task_id = tasks.create("approval task")
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "owner")
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "executing")
    attempt_id = tasks.record_attempt(task_id, "running")
    tasks.complete_attempt(attempt_id, "waiting")
    tasks.transition(task_id, "waiting")
    checkpoint = CheckpointStore(connection)
    checkpoint.save(
        task_id,
        "waiting",
        "wait",
        attempt_id=attempt_id,
        reason="approval",
        waiting_reason_code="approval",
        plan_version=2,
        plan_fingerprint="plan-hash",
    )
    connection.commit()
    return connection, task_id, checkpoint


def request():
    return {
        "plan_version": 2,
        "plan_fingerprint": "plan-hash",
        "step_id": "step-1",
        "tool": "filesystem",
        "arguments_fingerprint": "args-hash",
        "permission_scope": "write",
    }


def test_scoped_approval_is_bound_and_idempotent(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    token = checkpoint.get_active(task_id)["wait_token"]
    approvals.create(task_id, token, request())
    assert approvals.matches(task_id, token, request())
    assert approvals.decide(task_id, token, "reviewer", "write", "approved")
    assert approvals.ready(task_id, token, request())
    assert not approvals.decide(task_id, token, "reviewer", "write", "approved")
    connection.close()


@pytest.mark.parametrize(
    ("scope", "token", "message"),
    [
        ("network", "same", "stale or mismatched"),
        ("write", "wrong", "stale or mismatched"),
    ],
)
def test_scoped_approval_rejects_scope_or_token_mismatch(
    tmp_path, scope, token, message
):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    current = checkpoint.get_active(task_id)["wait_token"]
    approvals.create(task_id, current, request())
    with pytest.raises(ValueError, match=message):
        approvals.decide(task_id, token, "reviewer", scope, "approved")
    connection.close()


def test_rejection_and_revocation_are_explicit_state_transitions(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    token = checkpoint.get_active(task_id)["wait_token"]
    approvals.create(task_id, token, request())
    assert approvals.decide(task_id, token, "reviewer", "write", "rejected")
    with pytest.raises(ValueError, match="no longer available"):
        approvals.decide(task_id, token, "reviewer", "write", "revoked")
    connection.close()


def test_approval_decision_uow_writes_audit_event_atomically(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    tasks = TaskStore(connection)
    events = EventStore(connection)
    artifacts = ArtifactStore(connection)
    manager = TransactionManager(connection, tasks, events, artifacts, checkpoint)
    work = EngineUnitOfWork(manager, tasks, events, artifacts, checkpoint)
    token = checkpoint.get_active(task_id)["wait_token"]
    work.approval_store.create(task_id, token, request())
    connection.commit()
    assert work.decide_approval(task_id, token, "reviewer", "write", "approved")
    assert events.list_for_task(task_id)[-1]["event_type"] == "task.approval.approved"
    connection.close()


def test_approval_decision_rolls_back_when_audit_write_fails(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    tasks = TaskStore(connection)
    artifacts = ArtifactStore(connection)

    class BrokenEvents(EventStore):
        def record(self, *_args):
            raise RuntimeError("audit failure")

    events = BrokenEvents(connection)
    manager = TransactionManager(connection, tasks, events, artifacts, checkpoint)
    work = EngineUnitOfWork(manager, tasks, events, artifacts, checkpoint)
    token = checkpoint.get_active(task_id)["wait_token"]
    work.approval_store.create(task_id, token, request())
    connection.commit()
    with pytest.raises(RuntimeError, match="audit failure"):
        work.decide_approval(task_id, token, "reviewer", "write", "approved")
    assert work.approval_store.get(token)["status"] == "pending"
    connection.close()


def test_approval_identity_helpers_are_canonical():
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})
    plan = ExecutionPlan(
        goal="ship",
        version=3,
        steps=[PlanStep("s1", "write", "write", "filesystem", {"path": "x"})],
    )
    identity = request_for_step(plan, plan.steps[0], "write")
    assert identity["plan_version"] == 3
    assert identity["step_id"] == "s1"
    assert identity["tool"] == "filesystem"
    assert identity["permission_scope"] == "write"


def test_approval_store_rejects_invalid_and_stale_decisions(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    token = checkpoint.get_active(task_id)["wait_token"]
    with pytest.raises(ValueError, match="required"):
        approvals.decide(task_id, token, "", "write", "approved")
    with pytest.raises(ValueError, match="invalid approval"):
        approvals.decide(task_id, token, "reviewer", "write", "unknown")
    with pytest.raises(ValueError, match="stale"):
        approvals.decide(task_id, token, "reviewer", "write", "approved")
    assert not approvals.ready(task_id, token, request())
    connection.close()


def test_approval_store_rejects_wrong_task_and_approval_eligibility(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    token = checkpoint.get_active(task_id)["wait_token"]
    approvals.create(task_id, token, request())
    with pytest.raises(ValueError, match="stale"):
        approvals.decide(task_id + 1, token, "reviewer", "write", "approved")
    connection.execute(
        "UPDATE tasks SET approval_status = 'pending' WHERE id = ?", (task_id,)
    )
    connection.commit()
    with pytest.raises(ValueError, match="eligible"):
        approvals.decide(task_id, token, "reviewer", "write", "approved")
    connection.close()


def _work(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    tasks = TaskStore(connection)
    events = EventStore(connection)
    artifacts = ArtifactStore(connection)
    manager = TransactionManager(connection, tasks, events, artifacts, checkpoint)
    return (
        connection,
        task_id,
        checkpoint,
        EngineUnitOfWork(manager, tasks, events, artifacts, checkpoint),
    )


def test_decision_persists_approval_and_replan_atomically(tmp_path):
    connection, task_id, checkpoint, work = _work(tmp_path)
    old_token = checkpoint.get_active(task_id)["wait_token"]
    decision = PhaseDecision(
        task_id=task_id,
        attempt_id=None,
        expected_status="waiting",
        events=(("task.plan.created", {"version": 3}),),
        checkpoint={
            "phase": "execution",
            "next_action": "wait",
            "plan_version": 3,
            "reason": "approval",
            "waiting_reason_code": "approval",
            "plan_fingerprint": "new-plan",
        },
        approval_request=request() | {"plan_version": 3},
        replan=("approval", 2, 3),
    )
    work.commit_decision(decision)
    assert checkpoint.get_active(task_id)["wait_token"] != old_token
    assert (
        work.approval_store.get(checkpoint.get_active(task_id)["wait_token"])
        is not None
    )
    connection.close()


def test_decision_rejects_missing_store_state_and_inactive_attempt(tmp_path):
    connection, task_id, checkpoint, work = _work(tmp_path)
    work.checkpoint_store = None
    with pytest.raises(TransactionError, match="checkpoint store"):
        work.commit_decision(
            PhaseDecision(
                task_id, None, "waiting", (), checkpoint={"status": "waiting"}
            )
        )
    work.checkpoint_store = checkpoint
    with pytest.raises(TransactionError, match="task state"):
        work.commit_decision(PhaseDecision(task_id, None, "executing", ()))
    with pytest.raises(TransactionError, match="attempt"):
        work.commit_decision(PhaseDecision(task_id, 99999, "waiting", ()))
    connection.close()


def test_approval_store_detects_update_race(tmp_path):
    connection, task_id, checkpoint = setup_task(tmp_path)
    approvals = ApprovalStore(connection)
    token = checkpoint.get_active(task_id)["wait_token"]
    approvals.create(task_id, token, request())
    connection.execute(
        "CREATE TRIGGER ignore_approval_update BEFORE UPDATE ON task_step_approvals "
        "BEGIN SELECT RAISE(IGNORE); END"
    )
    with pytest.raises(ValueError, match="lost a race"):
        approvals.decide(task_id, token, "reviewer", "write", "approved")
    connection.close()
