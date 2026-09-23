"""End-to-end orchestration tests using real SQLite stores."""

import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.orchestrator import Orchestrator
from harness.engine.plan import ExecutionPlan
from harness.engine.planner import Planner
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    ResultStatus,
    WaitReason,
)
from harness.engine.validator import Validator
from harness.security.tool_policy import ToolSecurityPolicy
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
    ValidationWrite,
)
from harness.tools.base import ToolRegistry
from harness.tools.filesystem import FilesystemTool
from harness.tools.git import GitRepository
from harness.tools.test_runner import TestRunner


def test_real_engine_components_complete_sqlite_workflow(tmp_path):
    path = initialize_database(tmp_path / "real-harness.sqlite")
    connection = connect(path)
    projects = ProjectStore(connection)
    tasks = TaskStore(connection)
    project_id = projects.create("Project", str(tmp_path))
    task_id = tasks.create(
        "Create file: healthcheck.py",
        project_id=project_id,
        description="Implement the healthcheck and validate it.",
    )
    tasks.add_test_criterion(
        task_id, "healthcheck passes", command=["uv", "run", "pytest", "--version"]
    )
    connection.execute(
        "INSERT INTO task_acceptance_criteria (task_id, criterion) VALUES (?, ?)",
        (task_id, "healthcheck exists"),
    )
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "integration-test")
    registry = ToolRegistry()
    registry.register(FilesystemTool(tmp_path))
    GitRepository.init(tmp_path, "real-workflow")
    registry.register(GitRepository(tmp_path, "real-workflow"))
    registry.register(TestRunner(tmp_path))
    event_store = EventStore(connection)
    artifact_store = ArtifactStore(connection)
    context_builder = ContextBuilder(
        tasks, event_store, artifact_store, projects, tool_registry=registry
    )
    orchestrator = Orchestrator(
        context_builder,
        planner=Planner(),
        executor=Executor(registry, ToolSecurityPolicy(require_approval=True)),
        validator=Validator(),
        task_store=tasks,
        event_store=event_store,
        artifact_store=artifact_store,
        transaction_manager=TransactionManager(
            connection, tasks, event_store, artifact_store
        ),
    )

    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.SUCCESS
    assert (
        tmp_path / "healthcheck.py"
    ).read_text() == "Implement the healthcheck and validate it."
    assert result.data["validation"].next_action.value == "stop"
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    assert [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?", (task_id,)
        )
    ] == [
        "task.planning",
        "task.plan.created",
        "task.executing",
        "task.execution.completed",
        "task.execution.next_action",
        "task.validating",
        "task.validation.completed",
        "task.done",
    ]


def build_orchestrator(tmp_path, validator, planner=None, executor=None):
    path = initialize_database(tmp_path / "harness.sqlite")
    connection = connect(path)
    projects = ProjectStore(connection)
    tasks = TaskStore(connection)
    project_id = projects.create("Project", str(tmp_path))
    task_id = tasks.create("Task", project_id=project_id)
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "test")
    context_builder = ContextBuilder(
        tasks,
        EventStore(connection),
        ArtifactStore(connection),
        projects,
        checkpoint_store=CheckpointStore(connection),
    )

    class Planner:
        def plan(self, _context):
            return EngineResult.success("planned", plan=ExecutionPlan("Task"))

    class Executor:
        def execute(self, _context, _plan):
            from harness.engine.result import StepExecution

            execution = ExecutionResult(
                ExecutionStatus.SUCCESS, artifacts=["report.txt"]
            )
            execution.steps.append(
                StepExecution(
                    "report",
                    ExecutionStatus.SUCCESS,
                    artifacts=["step-report.txt"],
                    changed_files=["src/generated.py"],
                )
            )
            return EngineResult.success("executed", execution=execution)

    orchestrator = Orchestrator(
        context_builder,
        planner=planner or Planner(),
        executor=executor or Executor(),
        validator=validator,
        task_store=tasks,
        event_store=EventStore(connection),
        artifact_store=ArtifactStore(connection),
        checkpoint_store=context_builder.checkpoint_store,
        transaction_manager=TransactionManager(
            connection, tasks, EventStore(connection), ArtifactStore(connection)
        ),
    )
    return orchestrator, connection, task_id


@pytest.mark.parametrize(
    ("outcome", "failure_at"),
    [
        ("wait", "validation_event"),
        ("wait", "checkpoint"),
        ("wait", "attempt"),
        ("wait", "transition"),
        ("wait", "decision_event"),
        ("replan", "replan_record"),
        ("done", "invalidate"),
        ("failed", "failed_event"),
    ],
)
def test_validation_decision_rolls_back_every_partial_write(
    tmp_path, monkeypatch, outcome, failure_at
):
    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, type("Validator", (), {"validate": lambda *_args: None})()
    )
    tasks = orchestrator.task_store
    events = orchestrator.event_store
    checkpoints = orchestrator.checkpoint_store
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "executing")
    tasks.transition(task_id, "validating")
    attempt_id = tasks.record_attempt(task_id, "running")
    checkpoints.save(task_id, "validating", "validate", reason="before decision")
    connection.commit()
    checkpoint_before = dict(checkpoints.get(task_id))
    events_before = _event_types(connection, task_id)
    write = ValidationWrite(
        task_id,
        attempt_id,
        {"status": "failed"},
        outcome,
        "blocked",
        checkpoint={
            "phase": "waiting" if outcome == "wait" else "validating",
            "next_action": "wait" if outcome == "wait" else "replan",
            "reason": "blocked",
            "waiting_reason_code": "external_information"
            if outcome == "wait"
            else None,
        }
        if outcome in {"wait", "replan"}
        else None,
        event_type="task.replanning" if outcome == "replan" else None,
        event_payload={"reason": "blocked"},
        replan=(1, 2) if outcome == "replan" else None,
    )
    target, method, event_name = {
        "validation_event": (events, "record", "task.validation.completed"),
        "checkpoint": (checkpoints, "save", None),
        "attempt": (tasks, "complete_attempt", None),
        "transition": (tasks, "transition", None),
        "decision_event": (events, "record", "task.waiting"),
        "replan_record": (tasks, "record_replan", None),
        "invalidate": (checkpoints, "invalidate", None),
        "failed_event": (events, "record", "task.failed"),
    }[failure_at]
    original = getattr(target, method)

    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        if event_name is None or args[1] == event_name:
            raise RuntimeError(f"injected failure after {failure_at}")
        return result

    monkeypatch.setattr(target, method, fail_after_write)
    with pytest.raises(RuntimeError, match="injected failure"):
        orchestrator.unit_of_work.validation_decision(write)
    assert tasks.get(task_id)["status"] == "validating"
    assert tasks.attempts(task_id)[-1]["status"] == "running"
    assert dict(checkpoints.get(task_id)) == checkpoint_before
    assert _event_types(connection, task_id) == events_before
    assert tasks.replans(task_id) == []


def test_validation_decision_requires_active_task_attempt_and_checkpoint(tmp_path):
    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, type("Validator", (), {"validate": lambda *_args: None})()
    )
    tasks = orchestrator.task_store
    connection.commit()
    write = ValidationWrite(task_id, None, {}, "done", "validated")
    with pytest.raises(TransactionError, match="not validating"):
        orchestrator.unit_of_work.validation_decision(write)
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "executing")
    tasks.transition(task_id, "validating")
    connection.commit()
    with pytest.raises(TransactionError, match="not active"):
        orchestrator.unit_of_work.validation_decision(
            ValidationWrite(task_id, 999, {}, "done", "validated")
        )
    attempt_id = tasks.record_attempt(task_id, "running")
    tasks.complete_attempt(attempt_id, "failed")
    connection.commit()
    with pytest.raises(TransactionError, match="not active"):
        orchestrator.unit_of_work.validation_decision(
            ValidationWrite(task_id, attempt_id, {}, "done", "validated")
        )
    without_checkpoint = EngineUnitOfWork(
        TransactionManager(connection),
        tasks,
        orchestrator.event_store,
        orchestrator.artifact_store,
    )
    with pytest.raises(TransactionError, match="requires checkpoint store"):
        without_checkpoint.validation_decision(
            ValidationWrite(
                task_id, None, {}, "wait", "blocked", checkpoint={"phase": "waiting"}
            )
        )
    active_id = tasks.record_attempt(task_id, "running")
    orchestrator.checkpoint_store.save(task_id, "validating", "validate")
    orchestrator.unit_of_work.complete_done(task_id, active_id)
    assert tasks.get(task_id)["status"] == "done"
    assert tasks.attempts(task_id)[-1]["status"] == "completed"
    assert orchestrator.checkpoint_store.get_active(task_id) is None
    connection.close()


@pytest.mark.parametrize("action", ["retry_execution", "replan"])
def test_validation_decision_recovers_after_post_commit_process_crash(tmp_path, action):
    database = initialize_database(tmp_path / "validation-crash.sqlite")
    connection = connect(database)
    projects = ProjectStore(connection)
    tasks = TaskStore(connection)
    project_id = projects.create("Project", str(tmp_path))
    task_id = tasks.create("Task", project_id=project_id)
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "test")
    connection.commit()
    connection.close()
    worker = textwrap.dedent(
        """
        import os
        import sys
        from harness.engine.context_builder import ContextBuilder
        from harness.engine.orchestrator import Orchestrator
        from harness.engine.plan import ExecutionPlan
        from harness.engine.result import EngineResult, ExecutionResult, ExecutionStatus, ResultStatus
        from harness.storage.artifact_store import ArtifactStore
        from harness.storage.checkpoint_store import CheckpointStore
        from harness.storage.database import connect
        from harness.storage.event_store import EventStore
        from harness.storage.project_store import ProjectStore
        from harness.storage.task_store import TaskStore
        from harness.storage.transaction import TransactionManager

        database, task_id, mode, action = sys.argv[1:]
        connection = connect(database)
        tasks = TaskStore(connection)
        events = EventStore(connection)
        artifacts = ArtifactStore(connection)
        checkpoints = CheckpointStore(connection)
        builder = ContextBuilder(
            tasks, events, artifacts, ProjectStore(connection),
            checkpoint_store=checkpoints,
        )

        class Planner:
            def plan(self, _context):
                return EngineResult.success("planned", plan=ExecutionPlan("Task"))

        class Executor:
            def execute(self, *_args):
                return EngineResult.success(
                    "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
                )

        class Validator:
            def validate(self, context, *_args):
                prior = sum(
                    event["event_type"] == "task.validation.completed"
                    for event in context.previous_events
                )
                if prior == 0:
                    return EngineResult(
                        ResultStatus.FAILED, "try another cycle",
                        data={"next_action": action},
                    )
                return EngineResult.success("validated", next_action="stop")

        class CrashAfterCommit(Orchestrator):
            def _finish_validation(self, *args, **kwargs):
                decision = super()._finish_validation(*args, **kwargs)
                if decision.outcome in {"retry", "replan"}:
                    os._exit(74)
                return decision

        engine_type = CrashAfterCommit if mode == "crash" else Orchestrator
        engine = engine_type(
            builder, planner=Planner(), executor=Executor(), validator=Validator(),
            task_store=tasks, event_store=events, artifact_store=artifacts,
            checkpoint_store=checkpoints,
            transaction_manager=TransactionManager(connection, tasks, events, artifacts),
        )
        result = engine.run(int(task_id)) if mode == "crash" else engine.resume(int(task_id))
        if result.status is not ResultStatus.SUCCESS:
            raise SystemExit(f"workflow failed: {result.status}: {result.message}")
        """
    )
    crash = subprocess.run(
        [sys.executable, "-c", worker, str(database), str(task_id), "crash", action],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert crash.returncode == 74, crash.stderr
    connection = connect(database)
    assert TaskStore(connection).get(task_id)["status"] == "validating"
    checkpoint = CheckpointStore(connection).get_active(task_id)
    assert checkpoint["next_action"] == action
    assert _event_types(connection, task_id).count("task.validation.completed") == 1
    assert [row["status"] for row in TaskStore(connection).attempts(task_id)] == [
        "failed"
    ]
    connection.close()

    recovered = subprocess.run(
        [sys.executable, "-c", worker, str(database), str(task_id), "recover", action],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    connection = connect(database)
    assert TaskStore(connection).get(task_id)["status"] == "done"
    assert CheckpointStore(connection).get_active(task_id) is None
    assert _event_types(connection, task_id).count("task.validation.completed") == 2
    assert _event_types(connection, task_id).count("task.done") == 1
    assert [row["status"] for row in TaskStore(connection).attempts(task_id)] == [
        "failed",
        "completed",
    ]
    connection.close()


def test_external_wait_resolution_survives_restart_and_reaches_executor(tmp_path):
    from harness.engine.plan import PlanStep

    seen = []

    class Planner:
        def plan(self, context):
            seen.append(("planner", context.external_information_ref))
            return EngineResult.success(
                "planned",
                plan=ExecutionPlan("Task", steps=[PlanStep("read", "read", "noop")]),
            )

    class Executor:
        def execute(self, context, _plan):
            reference = context.external_information_ref
            seen.append(("executor", reference))
            if reference is not None:
                assert context.resume_checkpoint["information_ref"] == reference
                assert (tmp_path / reference).read_text() == "answer"
            return EngineResult.success(
                "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
            )

    class Validator:
        def validate(self, context, *_args):
            if context.external_information_ref is None:
                return EngineResult(
                    ResultStatus.WAITING,
                    "external answer needed",
                    data={"next_action": "wait"},
                    wait_reason=WaitReason.EXTERNAL_INFORMATION,
                )
            return EngineResult.success("validated", next_action="stop")

    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, Validator(), Planner(), Executor()
    )
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.WAITING
    checkpoint = orchestrator.checkpoint_store.get_active(task_id)
    token = checkpoint["wait_token"]
    assert token
    assert checkpoint["external_resolved_at"] is None
    attempts_before = len(orchestrator.task_store.attempts(task_id))
    unresolved = orchestrator.resume(task_id)
    assert unresolved.status is ResultStatus.WAITING
    assert len(orchestrator.task_store.attempts(task_id)) == attempts_before
    assert seen == [("planner", None), ("executor", None)]

    (tmp_path / "answer.txt").write_text("answer")
    assert orchestrator.resolve_external_wait(task_id, token, "tester", "answer.txt")
    assert not orchestrator.resolve_external_wait(
        task_id, token, "tester", "answer.txt"
    )
    assert _event_types(connection, task_id).count("task.external_wait.resolved") == 1
    connection.close()

    reopened = connect(tmp_path / "harness.sqlite")
    tasks = TaskStore(reopened)
    events = EventStore(reopened)
    artifacts = ArtifactStore(reopened)
    checkpoints = CheckpointStore(reopened)
    restarted = Orchestrator(
        ContextBuilder(
            tasks,
            events,
            artifacts,
            ProjectStore(reopened),
            checkpoint_store=checkpoints,
        ),
        planner=Planner(),
        executor=Executor(),
        validator=Validator(),
        task_store=tasks,
        event_store=events,
        artifact_store=artifacts,
        checkpoint_store=checkpoints,
        transaction_manager=TransactionManager(reopened, tasks, events, artifacts),
    )
    assert restarted.resume(task_id).status is ResultStatus.SUCCESS
    assert seen[-1] == ("executor", "answer.txt")
    assert tasks.get(task_id)["status"] == "done"


def test_new_wait_rotates_token_and_clears_prior_resolution(tmp_path):
    orchestrator, connection, task_id = build_orchestrator(
        tmp_path,
        type(
            "Validator", (), {"validate": lambda *_args: EngineResult.success("ok")}
        )(),
    )
    store = orchestrator.checkpoint_store
    store.save(task_id, "waiting", "wait", waiting_reason_code="external_information")
    orchestrator.task_store.transition(task_id, "planning")
    orchestrator.task_store.transition(task_id, "waiting")
    first = store.get_active(task_id)["wait_token"]
    assert orchestrator.resolve_external_wait(task_id, first, "tester", "answer.txt")
    store.save(task_id, "executing", "validate")
    assert store.get_active(task_id)["information_ref"] == "answer.txt"
    store.save(task_id, "waiting", "wait", waiting_reason_code="external_information")
    checkpoint = store.get_active(task_id)
    assert checkpoint["wait_token"] != first
    assert checkpoint["external_resolved_at"] is None
    assert checkpoint["information_ref"] is None
    assert checkpoint["resolved_by"] is None
    with pytest.raises(ValueError, match="stale wait token"):
        orchestrator.resolve_external_wait(task_id, first, "tester", "answer.txt")
    connection.close()


def test_external_wait_rejects_wrong_reason_and_stale_token(tmp_path):
    class Validator:
        calls = 0

        def validate(self, *_args):
            self.calls += 1
            return EngineResult(
                ResultStatus.WAITING,
                "approval needed" if self.calls == 1 else "external answer needed",
                data={"next_action": "wait"},
                wait_reason=(
                    WaitReason.APPROVAL
                    if self.calls == 1
                    else WaitReason.EXTERNAL_INFORMATION
                ),
            )

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    first = orchestrator.checkpoint_store.get_active(task_id)["wait_token"]
    with pytest.raises(ValueError, match="not waiting for external"):
        orchestrator.resolve_external_wait(task_id, first, "tester", "a.txt")
    with pytest.raises(ValueError, match="stale wait token"):
        orchestrator.resolve_external_wait(task_id, "wrong-token", "tester", "a.txt")
    orchestrator.task_store.approve(task_id, "tester")
    assert orchestrator.resume(task_id).status is ResultStatus.WAITING
    second = orchestrator.checkpoint_store.get_active(task_id)["wait_token"]
    assert second != first
    with pytest.raises(ValueError, match="stale wait token"):
        orchestrator.resolve_external_wait(task_id, first, "tester", "a.txt")
    assert orchestrator.resolve_external_wait(task_id, second, "tester", "a.txt")
    with pytest.raises(ValueError, match="another reference"):
        orchestrator.resolve_external_wait(task_id, second, "tester", "b.txt")
    assert _event_types(connection, task_id).count("task.external_wait.resolved") == 1


def test_external_wait_resolution_racing_resume(tmp_path):
    class Validator:
        def validate(self, context, *_args):
            if context.external_information_ref:
                return EngineResult.success("validated", next_action="stop")
            return EngineResult(
                ResultStatus.WAITING,
                "answer needed",
                data={"next_action": "wait"},
                wait_reason=WaitReason.EXTERNAL_INFORMATION,
            )

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    token = orchestrator.checkpoint_store.get_active(task_id)["wait_token"]
    connection.close()
    barrier = Barrier(2)

    def worker(resolve):
        conn = connect(tmp_path / "harness.sqlite")
        tasks = TaskStore(conn)
        events = EventStore(conn)
        artifacts = ArtifactStore(conn)
        checkpoints = CheckpointStore(conn)
        engine = Orchestrator(
            ContextBuilder(
                tasks,
                events,
                artifacts,
                ProjectStore(conn),
                checkpoint_store=checkpoints,
            ),
            planner=type(
                "Planner",
                (),
                {
                    "plan": lambda _self, _ctx: EngineResult.success(
                        "planned", plan=ExecutionPlan("Task")
                    )
                },
            )(),
            executor=type(
                "Executor",
                (),
                {
                    "execute": lambda _self, _ctx, _plan: EngineResult.success(
                        "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
                    )
                },
            )(),
            validator=Validator(),
            task_store=tasks,
            event_store=events,
            artifact_store=artifacts,
            checkpoint_store=checkpoints,
            transaction_manager=TransactionManager(conn, tasks, events, artifacts),
        )
        barrier.wait()
        try:
            return (
                engine.resolve_external_wait(task_id, token, "tester", "answer.txt")
                if resolve
                else engine.resume(task_id).status
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resolver = pool.submit(worker, True)
        resumer = pool.submit(worker, False)
        assert resolver.result() is True
        assert resumer.result() in {ResultStatus.WAITING, ResultStatus.SUCCESS}
    final_connection = connect(tmp_path / "harness.sqlite")
    if TaskStore(final_connection).get(task_id)["status"] == "waiting":
        tasks = TaskStore(final_connection)
        events = EventStore(final_connection)
        artifacts = ArtifactStore(final_connection)
        checkpoints = CheckpointStore(final_connection)
        restarted = Orchestrator(
            ContextBuilder(
                tasks,
                events,
                artifacts,
                ProjectStore(final_connection),
                checkpoint_store=checkpoints,
            ),
            planner=type(
                "Planner",
                (),
                {
                    "plan": lambda _self, _ctx: EngineResult.success(
                        "planned", plan=ExecutionPlan("Task")
                    )
                },
            )(),
            executor=type(
                "Executor",
                (),
                {
                    "execute": lambda _self, _ctx, _plan: EngineResult.success(
                        "executed", execution=ExecutionResult(ExecutionStatus.SUCCESS)
                    )
                },
            )(),
            validator=Validator(),
            task_store=tasks,
            event_store=events,
            artifact_store=artifacts,
            checkpoint_store=checkpoints,
            transaction_manager=TransactionManager(
                final_connection, tasks, events, artifacts
            ),
        )
        assert restarted.resume(task_id).status is ResultStatus.SUCCESS
    assert TaskStore(final_connection).get(task_id)["status"] == "done"
    assert (
        _event_types(final_connection, task_id).count("task.external_wait.resolved")
        == 1
    )


@pytest.mark.parametrize("reason", [None, "unknown"])
def test_new_wait_requires_valid_reason(tmp_path, reason):
    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.WAITING,
                "wait",
                data={"next_action": "wait"},
                wait_reason=reason,
            )

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.FAILED
    assert "WAIT requires a valid WaitReason" in result.message
    assert TaskStore(connection).get(task_id)["status"] == "failed"
    assert CheckpointStore(connection).get_active(task_id) is None


def _event_types(connection, task_id):
    return [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]


def test_sqlite_workflow_persists_success_attempt_and_events(tmp_path):
    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "stop"})

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.run(task_id)

    task = connection.execute(
        "SELECT status, created_at, updated_at, started_at, planning_started_at, validation_started_at, completed_at, failed_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    attempt = connection.execute(
        "SELECT status, completed_at FROM task_attempts WHERE task_id = ?", (task_id,)
    ).fetchone()
    events = [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?", (task_id,)
        )
    ]
    assert result.status is ResultStatus.SUCCESS
    assert task[0] == "done"
    assert all(task[index] is not None for index in range(1, 7))
    assert task[7] is None
    assert attempt[0] == "completed"
    assert attempt[1] is not None
    assert "task.plan.created" in events
    assert "task.execution.completed" in events
    assert "task.validation.completed" in events
    artifacts = [
        row[0]
        for row in connection.execute(
            "SELECT path FROM task_artifacts WHERE task_id = ?", (task_id,)
        )
    ]
    assert artifacts == ["report.txt", "step-report.txt"]
    assert (
        "src/generated.py"
        in connection.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'task.execution.completed'",
            (task_id,),
        ).fetchone()[0]
    )
    checkpoint = CheckpointStore(connection)
    assert checkpoint.get(task_id)["invalidated_at"] is not None
    assert checkpoint.get_active(task_id) is None
    event_count = len(_event_types(connection, task_id))
    attempt_count = connection.execute(
        "SELECT COUNT(*) FROM task_attempts WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    repeated = orchestrator.resume(task_id)
    assert repeated.status is ResultStatus.SUCCESS
    assert repeated.data["already_completed"] is True
    assert orchestrator.unit_of_work.claim_resume_lease(task_id, "late") is False
    assert len(_event_types(connection, task_id)) == event_count
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == attempt_count
    )


def test_sqlite_workflow_persists_validation_failure_and_failed_status(tmp_path):
    class Validator:
        def validate(self, *_args):
            return EngineResult(
                ResultStatus.FAILED,
                message="invalid result",
                data={"next_action": "stop"},
            )

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.FAILED
    assert (
        connection.execute(
            "SELECT status, failed_at, planning_started_at, validation_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()[0]
        == "failed"
    )
    failed_task = connection.execute(
        "SELECT failed_at, planning_started_at, validation_started_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert all(value is not None for value in failed_task)
    events = [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?", (task_id,)
        )
    ]
    assert "task.validation.failed" in events
    assert "task.failed" in events


def test_sqlite_workflow_supports_cancelled(tmp_path):
    class Validator:
        def validate(self, *_args):
            return EngineResult.success("unused")

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.cancel(task_id, "User cancelled")

    assert result.data["cancelled"] is True
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert (
        connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()[0]
        == "task.cancelled"
    )


def test_sqlite_workflow_persists_invalid_next_action(tmp_path):
    class Validator:
        def validate(self, *_args):
            return EngineResult(ResultStatus.SUCCESS, data={"next_action": "invalid"})

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.FAILED
    task = connection.execute(
        "SELECT status, failed_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert task[0] == "failed"
    assert task[1] is not None
    events = [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?", (task_id,)
        )
    ]
    assert "task.validation.invalid_next_action" in events
    assert "task.failed" in events


def test_sqlite_workflow_persists_unexpected_planner_exception(tmp_path):
    class Planner:
        def plan(self, _context):
            raise RuntimeError("planner crashed")

    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, object(), planner=Planner()
    )
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.FAILED
    assert "Unexpected error during planning" in result.message
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert (
        connection.execute(
            "SELECT status, error_message FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        == "failed"
    )
    assert "task.planning.exception" in _event_types(connection, task_id)


def test_sqlite_workflow_persists_unexpected_executor_exception(tmp_path):
    class Executor:
        def execute(self, *_args):
            raise RuntimeError("executor crashed")

    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, object(), executor=Executor()
    )
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.FAILED
    assert "Unexpected error during execution" in result.message
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert "task.execution.exception" in _event_types(connection, task_id)


def test_sqlite_workflow_persists_unexpected_validator_exception(tmp_path):
    class Validator:
        def validate(self, *_args):
            raise RuntimeError("validator crashed")

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.FAILED
    assert "Unexpected error during validation" in result.message
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert "task.validation.exception" in _event_types(connection, task_id)


def test_sqlite_task_claim_is_atomic(tmp_path):
    path = initialize_database(tmp_path / "claim.sqlite")
    first = connect(path)
    second = connect(path)
    try:
        projects = ProjectStore(first)
        tasks = TaskStore(first)
        project_id = projects.create("Project", str(tmp_path))
        task_id = tasks.create("Claim me", project_id=project_id)
        tasks.transition(task_id, "ready")
        tasks.approve(task_id, "test")
        first.commit()

        first_tasks = TaskStore(first)
        second_tasks = TaskStore(second)
        assert first_tasks.claim(task_id) is True
        first.commit()
        assert second_tasks.claim(task_id) is False
        assert second_tasks.get(task_id)["status"] == "planning"
    finally:
        first.close()
        second.close()


def test_sqlite_workflow_persists_replan_and_plan_feedback(tmp_path):
    class Validator:
        calls = 0

        def validate(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return EngineResult(
                    ResultStatus.FAILED,
                    message="plan needs revision",
                    data={"next_action": "replan"},
                )
            return EngineResult.success("validated", next_action="stop")

    validator = Validator()
    orchestrator, connection, task_id = build_orchestrator(tmp_path, validator)
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.SUCCESS
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    attempts = connection.execute(
        "SELECT status FROM task_attempts WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    assert [row[0] for row in attempts] == ["failed", "completed"]
    assert _event_types(connection, task_id).count("task.replanning") == 1
    payload = connection.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'task.replanning'",
        (task_id,),
    ).fetchone()[0]
    assert "plan needs revision" in payload


def test_sqlite_workflow_persists_retry_attempts(tmp_path):
    class Executor:
        calls = 0

        def execute(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return EngineResult(
                    ResultStatus.FAILED,
                    message="temporary failure",
                    data={"next_action": "retry_execution"},
                )
            from harness.engine.result import ExecutionResult, ExecutionStatus

            return EngineResult.success(
                "executed",
                execution=ExecutionResult(ExecutionStatus.SUCCESS),
            )

    executor = Executor()

    class Validator:
        def validate(self, *_args):
            return EngineResult.success("validated", next_action="stop")

    orchestrator, connection, task_id = build_orchestrator(
        tmp_path, Validator(), executor=executor
    )
    result = orchestrator.run(task_id)

    assert result.status is ResultStatus.SUCCESS
    assert executor.calls == 2
    attempts = connection.execute(
        "SELECT status, error_message FROM task_attempts WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    assert attempts[0][0] == "failed"
    assert attempts[0][1] == "temporary failure"
    assert attempts[1][0] == "completed"
    assert "task.retrying" in _event_types(connection, task_id)


def test_sqlite_workflow_persists_wait_checkpoint_and_resume(tmp_path):
    class Validator:
        calls = 0

        def validate(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return EngineResult(
                    ResultStatus.WAITING,
                    message="approval required",
                    data={"next_action": "wait"},
                    wait_reason="approval",
                )
            return EngineResult.success("validated", next_action="stop")

    validator = Validator()
    orchestrator, connection, task_id = build_orchestrator(tmp_path, validator)
    waiting = orchestrator.run(task_id)
    checkpoint = connection.execute(
        "SELECT phase, next_action, reason, resumed_at FROM task_checkpoints WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert waiting.status is ResultStatus.WAITING
    assert checkpoint[0:3] == ("waiting", "wait", "approval required")
    assert checkpoint[3] is None
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "waiting"
    )

    resumed = orchestrator.resume(task_id)

    assert resumed.status is ResultStatus.SUCCESS
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    assert (
        connection.execute(
            "SELECT resumed_at FROM task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        is not None
    )
    assert _event_types(connection, task_id).count("task.waiting") == 1
    assert CheckpointStore(connection).get_active(task_id) is None
    checkpoint = CheckpointStore(connection).get(task_id)
    assert checkpoint["invalidated_at"] is not None
    assert checkpoint["reason"] == "approval required"
    events_before = _event_types(connection, task_id)
    attempts_before = connection.execute(
        "SELECT COUNT(*) FROM task_attempts WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    assert orchestrator.resume(task_id).data["already_completed"] is True
    assert _event_types(connection, task_id) == events_before
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == attempts_before
    )


def test_terminal_checkpoint_failure_rolls_back_task_attempt_and_event(tmp_path):
    class Validator:
        def validate(self, *_args):
            return EngineResult.success("validated", next_action="stop")

    class FailingCheckpointStore(CheckpointStore):
        def invalidate(self, task_id, reason=None):
            super().invalidate(task_id, reason)
            raise RuntimeError("checkpoint write failed")

    orchestrator, connection, task_id = build_orchestrator(tmp_path, Validator())
    orchestrator.unit_of_work.checkpoint_store = FailingCheckpointStore(connection)
    with pytest.raises(RuntimeError, match="checkpoint write failed"):
        orchestrator.run(task_id)

    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "validating"
    )
    assert (
        connection.execute(
            "SELECT status FROM task_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "running"
    )
    assert CheckpointStore(connection).get_active(task_id) is not None
    assert "task.done" not in _event_types(connection, task_id)


def test_validation_resume_retires_checkpoint_and_is_idempotent(tmp_path):
    class Validator:
        calls = 0

        def validate(self, *_args):
            self.calls += 1
            return EngineResult.success("validated", next_action="stop")

    validator = Validator()
    orchestrator, connection, task_id = build_orchestrator(tmp_path, validator)
    tasks = orchestrator.task_store
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "executing")
    tasks.transition(task_id, "validating")
    tasks.transition(task_id, "waiting")
    checkpoints = CheckpointStore(connection)
    checkpoints.save(
        task_id,
        "waiting",
        "validate",
        reason="resume validation",
        plan_version=1,
        context_data={
            "plan": Orchestrator._serialize(ExecutionPlan("Task")),
            "execution": Orchestrator._serialize(
                ExecutionResult(ExecutionStatus.SUCCESS)
            ),
        },
    )
    connection.commit()

    assert orchestrator.resume(task_id).status is ResultStatus.SUCCESS
    assert validator.calls == 1
    assert checkpoints.get(task_id)["invalidated_at"] is not None
    assert checkpoints.get_active(task_id) is None
    events_before = _event_types(connection, task_id)
    assert orchestrator.resume(task_id).data["already_completed"] is True
    assert validator.calls == 1
    assert _event_types(connection, task_id) == events_before


def test_sqlite_resume_recovers_after_process_crash_and_restart(tmp_path):
    database = initialize_database(tmp_path / "crash-recovery.sqlite")
    connection = connect(database)
    projects = ProjectStore(connection)
    tasks = TaskStore(connection)
    project_id = projects.create("Crash recovery", str(tmp_path))
    task_id = tasks.create("Resume me", project_id=project_id)
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "integration-test")
    tasks.transition(task_id, "planning")
    tasks.transition(task_id, "waiting")
    plan = ExecutionPlan("Resume me")
    checkpoint = CheckpointStore(connection)
    checkpoint.save(
        task_id,
        "waiting",
        "wait",
        reason="approval required",
        waiting_reason_code="approval",
        context_data={
            "plan": Orchestrator._serialize(plan),
            "execution": Orchestrator._serialize(
                ExecutionResult(ExecutionStatus.SUCCESS)
            ),
        },
    )
    connection.commit()
    connection.close()

    worker = textwrap.dedent(
        """
        import os
        import sys
        from harness.engine.context_builder import ContextBuilder
        from harness.engine.orchestrator import Orchestrator
        from harness.engine.result import EngineResult, ExecutionResult, ExecutionStatus, ResultStatus
        from harness.storage.artifact_store import ArtifactStore
        from harness.storage.checkpoint_store import CheckpointStore
        from harness.storage.database import connect
        from harness.storage.event_store import EventStore
        from harness.storage.project_store import ProjectStore
        from harness.storage.task_store import TaskStore
        from harness.storage.transaction import TransactionManager

        database, task_id, mode, marker = sys.argv[1:]
        connection = connect(database)
        tasks = TaskStore(connection)
        events = EventStore(connection)
        artifacts = ArtifactStore(connection)
        checkpoints = CheckpointStore(connection)
        projects = ProjectStore(connection)
        builder = ContextBuilder(
            tasks, events, artifacts, projects, checkpoint_store=checkpoints
        )

        class Executor:
            def execute(self, *_args):
                with open(marker, "a", encoding="utf-8") as stream:
                    stream.write("executed\\n")
                execution = ExecutionResult(
                    ExecutionStatus.SUCCESS, next_action="validate"
                )
                return EngineResult.success("executed", execution=execution)

        class Validator:
            def validate(self, *_args):
                return EngineResult(ResultStatus.SUCCESS, data={"next_action": "stop"})

        class CrashAfterClaim(Orchestrator):
            def _resume_from_checkpoint(self, *_args):
                os._exit(73)

        manager = TransactionManager(connection, tasks, events, artifacts)
        orchestrator_type = CrashAfterClaim if mode == "crash" else Orchestrator
        orchestrator = orchestrator_type(
            builder,
            planner=object(),
            executor=Executor(),
            validator=Validator(),
            task_store=tasks,
            event_store=events,
            artifact_store=artifacts,
            checkpoint_store=checkpoints,
            transaction_manager=manager,
            resume_lease_seconds=1,
        )
        result = orchestrator.resume(int(task_id))
        if mode == "recover" and result.status is not ResultStatus.SUCCESS:
            raise SystemExit(f"resume failed: {result.status}: {result.message}")
        connection.close()
        """
    )
    marker = tmp_path / "execution-count.txt"
    crash = subprocess.run(
        [
            sys.executable,
            "-c",
            worker,
            str(database),
            str(task_id),
            "crash",
            str(marker),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert crash.returncode == 73, crash.stderr

    connection = connect(database)
    row = connection.execute(
        "SELECT resume_claim_token, resume_claim_expires_at FROM task_checkpoints WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert row["resume_claim_token"] is not None
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "waiting"
    )
    connection.execute(
        "UPDATE task_checkpoints SET resume_claim_expires_at = '2000-01-01 00:00:00' WHERE task_id = ?",
        (task_id,),
    )
    connection.commit()
    connection.close()

    recovery = subprocess.run(
        [
            sys.executable,
            "-c",
            worker,
            str(database),
            str(task_id),
            "recover",
            str(marker),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert recovery.returncode == 0, recovery.stderr

    connection = connect(database)
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    assert (
        connection.execute(
            "SELECT status FROM task_attempts WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()[0]
        == "completed"
    )
    assert (
        connection.execute(
            "SELECT resume_claim_token FROM task_checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        is None
    )
    assert _event_types(connection, task_id).count("task.resumed") == 2
    assert _event_types(connection, task_id).count("task.done") == 1
    connection.close()
    assert marker.read_text(encoding="utf-8").splitlines() == ["executed"]
