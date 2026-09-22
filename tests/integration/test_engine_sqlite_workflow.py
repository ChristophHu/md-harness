"""End-to-end orchestration tests using real SQLite stores."""

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
from harness.storage.database import connect, initialize_database
from harness.storage.event_store import EventStore
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore
from harness.storage.transaction import TransactionManager


def build_orchestrator(tmp_path, validator):
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
        planner=Planner(),
        executor=Executor(),
        validator=validator,
        task_store=tasks,
        event_store=EventStore(connection),
        artifact_store=ArtifactStore(connection),
        transaction_manager=TransactionManager(
            connection, tasks, EventStore(connection), ArtifactStore(connection)
        ),
    )
    return orchestrator, connection, task_id


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
