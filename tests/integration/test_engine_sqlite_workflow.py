"""End-to-end orchestration tests using real SQLite stores."""

from dataclasses import dataclass

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
)
from harness.engine.validator import Validator
from harness.security.tool_policy import ToolSecurityPolicy
from harness.storage.artifact_store import ArtifactStore
from harness.storage.checkpoint_store import CheckpointStore
from harness.storage.database import connect, initialize_database
from harness.storage.event_store import EventStore
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore
from harness.storage.transaction import TransactionManager
from harness.tools.base import PermissionLevel, Tool, ToolDefinition, ToolRegistry


@dataclass
class NoOpTool(Tool):
    """Read-only test tool that accepts the planner's abstract steps."""

    name: str

    def __post_init__(self):
        object.__setattr__(
            self,
            "definition",
            ToolDefinition(self.name, "test tool", PermissionLevel.READ),
        )
        Tool.__init__(self)

    def execute(self, **_arguments):
        return None


def test_real_engine_components_complete_sqlite_workflow(tmp_path):
    path = initialize_database(tmp_path / "real-harness.sqlite")
    connection = connect(path)
    projects = ProjectStore(connection)
    tasks = TaskStore(connection)
    project_id = projects.create("Project", str(tmp_path))
    task_id = tasks.create(
        "Implement healthcheck",
        project_id=project_id,
        description="Implement the healthcheck and validate it.",
    )
    tasks.add_test_criterion(task_id, "healthcheck passes")
    connection.execute(
        "INSERT INTO task_acceptance_criteria (task_id, criterion) VALUES (?, ?)",
        (task_id, "healthcheck exists"),
    )
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "integration-test")
    registry = ToolRegistry()
    registry.register(NoOpTool("filesystem"))
    registry.register(NoOpTool("sqlite"))
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
