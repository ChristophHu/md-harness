import json
import sqlite3

import pytest

from harness.storage.transaction import (
    EngineUnitOfWork,
    ExecutionOwner,
    TransactionError,
    TransactionManager,
)


def _orphaned_work(tmp_path, *, status="planning"):
    from harness.storage.database import connect, initialize_database
    from harness.storage.factory import StoreFactory
    from harness.storage.project_store import ProjectStore

    connection = connect(initialize_database(tmp_path / "orphan.sqlite"))
    stores = StoreFactory.create(connection)
    project_id = ProjectStore(connection).create("project", str(tmp_path))
    task_id = stores.task_store.create("task", project_id=project_id)
    stores.task_store.transition(task_id, "ready")
    stores.task_store.approve(task_id, "test")
    stores.task_store.transition(task_id, status)
    attempt_id = stores.task_store.record_attempt(task_id, "running")
    connection.execute(
        "UPDATE tasks SET claim_token = 'expired', claim_expires_at = '2000-01-01' WHERE id = ?",
        (task_id,),
    )
    connection.commit()
    work = EngineUnitOfWork(
        stores.transaction_manager,
        stores.task_store,
        stores.event_store,
        stores.artifact_store,
        stores.checkpoint_store,
    )
    return connection, stores, work, task_id, attempt_id


def test_claim_orphaned_run_takes_over_only_after_fencing_old_attempt(tmp_path):
    connection, stores, work, task_id, attempt_id = _orphaned_work(tmp_path)
    assert work.claim_orphaned_run(task_id, "new-owner", 23) is True
    task = stores.task_store.get(task_id)
    assert task["claim_token"] == "new-owner"
    assert task["execution_epoch"] == 1
    assert stores.task_store.attempts(task_id)[0]["status"] == "failed"
    assert work.manager.owner.token == "new-owner"
    event = stores.event_store.list_for_task(task_id)[0]
    assert event["event_type"] == "task.recovery.claimed"
    assert json.loads(event["payload"]) == {
        "previous_status": "planning",
        "interrupted_attempts": 1,
    }
    assert attempt_id == stores.task_store.attempts(task_id)[0]["id"]
    connection.close()


def test_claim_orphaned_run_rejects_invalid_or_ineligible_tasks(tmp_path):
    from harness.storage.database import connect, initialize_database
    from harness.storage.factory import StoreFactory

    connection = connect(initialize_database(tmp_path / "ineligible.sqlite"))
    stores = StoreFactory.create(connection)
    work = EngineUnitOfWork(
        stores.transaction_manager,
        stores.task_store,
        stores.event_store,
        stores.artifact_store,
        stores.checkpoint_store,
    )
    assert work.claim_orphaned_run(1, "token") is False
    project_id = connection.execute(
        "INSERT INTO projects (name, path) VALUES ('p', '/tmp/p')"
    ).lastrowid
    task_id = stores.task_store.create("ready", project_id=project_id)
    assert work.claim_orphaned_run(task_id, "token") is False
    stores.task_store.transition(task_id, "ready")
    stores.task_store.approve(task_id, "test")
    stores.task_store.transition(task_id, "planning")
    connection.execute(
        "UPDATE tasks SET claim_token = 'live', claim_expires_at = '2999-01-01' WHERE id = ?",
        (task_id,),
    )
    connection.commit()
    assert work.claim_orphaned_run(task_id, "token") is False
    connection.execute(
        "UPDATE tasks SET claim_token = 'expired', claim_expires_at = '2000-01-01' WHERE id = ?",
        (task_id,),
    )
    stores.checkpoint_store.save(task_id, "executing", "validate")
    connection.commit()
    assert work.claim_orphaned_run(task_id, "token") is False
    assert (
        EngineUnitOfWork(
            stores.transaction_manager,
            stores.task_store,
            stores.event_store,
            stores.artifact_store,
        ).claim_orphaned_run(task_id, "token")
        is False
    )
    assert work.claim_orphaned_run(task_id, "token", 0) is False
    assert (
        EngineUnitOfWork(
            stores.transaction_manager,
            stores.task_store,
            stores.event_store,
            stores.artifact_store,
            None,
        ).claim_orphaned_run(task_id, "token", 0)
        is False
    )
    connection.close()


def test_claim_orphaned_run_refuses_tool_effect_history(tmp_path):
    connection, _stores, work, task_id, _attempt_id = _orphaned_work(tmp_path)
    connection.execute(
        """INSERT INTO task_tool_invocations
           (invocation_id, task_id, plan_fingerprint, plan_version, step_id,
            tool_name, arguments_fingerprint, permission_scope, status)
           VALUES ('invocation', ?, 'plan', 1, 'step', 'git', 'args', 'network', 'completed')""",
        (task_id,),
    )
    connection.commit()
    assert work.claim_orphaned_run(task_id, "new-owner") is False
    connection.close()


def test_claim_orphaned_run_rejects_legacy_or_raced_claims(tmp_path):
    connection, stores, work, task_id, _attempt_id = _orphaned_work(tmp_path)
    work.task_store.get = lambda _task_id: {
        "status": "planning",
        "claim_token": None,
        "claim_expires_at": None,
    }
    assert work.claim_orphaned_run(task_id, "no-epoch") is False

    work.task_store.get = stores.task_store.get
    work.checkpoint_store = Store(connection)
    assert work.claim_orphaned_run(task_id, "no-checkpoint-reader") is False
    work.checkpoint_store = stores.checkpoint_store
    work.manager.connection.execute(
        """CREATE TRIGGER ignore_claim BEFORE UPDATE OF claim_token ON tasks
           BEGIN SELECT RAISE(IGNORE); END"""
    )
    assert work.claim_orphaned_run(task_id, "raced") is False
    connection.close()


def test_claim_orphaned_run_fails_if_atomic_claim_loses_race(tmp_path):
    connection, _stores, work, task_id, _attempt_id = _orphaned_work(tmp_path)
    work.manager.connection.execute(
        """CREATE TRIGGER replace_claim BEFORE UPDATE OF claim_token ON tasks
           BEGIN
             UPDATE tasks SET claim_token = 'other', claim_expires_at = '2999-01-01'
             WHERE id = NEW.id;
             SELECT RAISE(IGNORE);
           END"""
    )
    assert work.claim_orphaned_run(task_id, "raced") is False
    connection.close()


class Store:
    def __init__(self, connection):
        self.connection = connection


def test_heartbeat_is_noop_without_owner_or_file_database():
    manager = TransactionManager(sqlite3.connect(":memory:"))
    with manager.keep_lease_alive():
        pass
    manager.owner = ExecutionOwner(1, "owner", 1)
    with manager.keep_lease_alive():
        pass


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


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        (None, TypeError, "mapping"),
        ({"kind": "unknown"}, ValueError, "kind"),
        ({"prompt": " "}, ValueError, "prompt"),
        ({"resume_action": "stop"}, ValueError, "resume_action"),
        ({"response_schema": {"type": "alien"}}, ValueError, "response_schema"),
        ({"request_data": "text"}, TypeError, "request_data"),
    ],
)
def test_interaction_request_contract_rejects_invalid_fields(change, error, message):
    request = {
        "kind": "human_decision",
        "prompt": "Choose",
        "resume_action": "replan",
        "response_schema": {"type": "string"},
    }
    if change is not None:
        request.update(change)
    else:
        request = None
    with pytest.raises(error, match=message):
        EngineUnitOfWork._validate_interaction_request(request)


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

        def get_active(self, _task_id):
            return {"wait_token": "approval-token"}

    class Approvals:
        def __init__(self):
            self.created = []

        def create(self, task_id, token, request):
            self.created.append((task_id, token, request))

    work = EngineUnitOfWork(
        TransactionManager(connection),
        Tasks(connection),
        Events(connection),
        Store(connection),
        Checkpoints(connection),
    )
    approvals = Approvals()
    work.approval_store = approvals
    work.execution_wait(
        1,
        4,
        {"reason": "approval", "phase": "waiting", "next_action": "wait"},
        None,
        "wait",
        {"step_id": "step-1"},
    )
    assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "waiting"
    assert connection.execute("SELECT status FROM attempts").fetchone()[0] == "waiting"
    assert (
        connection.execute("SELECT reason FROM checkpoints").fetchone()[0] == "approval"
    )
    assert approvals.created == [(1, "approval-token", {"step_id": "step-1"})]


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
