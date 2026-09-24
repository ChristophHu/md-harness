"""Effectful tool calls cannot be blindly replayed after an uncertain exit."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.config import ExecutionConfig
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.orchestrator import Orchestrator
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import (
    EngineResult,
    ResultStatus,
    ToolExecutionResult,
    WaitReason,
)
from harness.engine.validator import ValidationResult, Validator
from harness.security.tool_policy import ToolSecurityPolicy
from harness.storage.database import connect, initialize_database
from harness.storage.factory import StoreFactory
from harness.storage.project_store import ProjectStore
from harness.tools.base import PermissionLevel, Tool, ToolDefinition, ToolRegistry
from harness.tools.filesystem import FilesystemTool


class EffectTool(Tool):
    definition = ToolDefinition("effect", "Append one effect", PermissionLevel.WRITE)

    def __init__(self, path, *, crash: bool = False):
        super().__init__()
        self.path = path
        self.crash = crash
        self.calls = 0

    def execute(self, **_arguments):
        self.calls += 1
        if self.crash:
            raise SystemExit("simulated process loss before effect")
        with self.path.open("a", encoding="utf-8") as file:
            file.write("effect\n")
        return ToolExecutionResult(data={"changed_files": [self.path.name]})


class ProcessExitingTool(EffectTool):
    def execute(self, **_arguments):
        with self.path.open("a", encoding="utf-8") as file:
            file.write("effect\n")
        os._exit(23)


class Planner:
    def plan(self, _context):
        return EngineResult.success(
            "planned",
            plan=ExecutionPlan(
                "one effect", [PlanStep("effect-1", "Apply effect", "write", "effect")]
            ),
        )


class SuccessValidator:
    def validate(self, *_args):
        return EngineResult.success("validated", validation=ValidationResult())


def _engine(database, workspace, tool, *, task_id=None):
    if not database.exists():
        initialize_database(database)
    connection = connect(database)
    stores = StoreFactory.create(connection, workspace)
    projects = ProjectStore(connection)
    if task_id is None:
        project_id = projects.create("Effects", str(workspace))
        task_id = stores.task_store.create("One effect", project_id=project_id)
        stores.task_store.transition(task_id, "ready")
        stores.task_store.approve(task_id, "owner")
    registry = ToolRegistry()
    registry.register(tool)
    builder = ContextBuilder.from_stores(stores, projects, tool_registry=registry)
    orchestrator = Orchestrator(
        builder,
        planner=Planner(),
        executor=Executor(registry, ToolSecurityPolicy(require_approval=False)),
        validator=SuccessValidator(),
        stores=stores,
        execution_config=ExecutionConfig(dry_run=False, persistence_mode="required"),
    )
    return connection, orchestrator, task_id


def test_crash_after_effect_requires_reconciliation_then_reuses_step(tmp_path):
    database = tmp_path / "journal.sqlite"
    effect_path = tmp_path / "effect.txt"
    connection, initial, task_id = _engine(database, tmp_path, EffectTool(effect_path))

    def crash_before_journal_completion(_task_id, _invocation_id, _step):
        raise SystemExit("simulated process loss after effect")

    initial.unit_of_work.complete_tool_invocation = crash_before_journal_completion
    with pytest.raises(SystemExit, match="after effect"):
        initial.run(task_id)
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    row = connection.execute(
        "SELECT invocation_id, status FROM task_tool_invocations WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    invocation_id = row["invocation_id"]
    assert row["status"] == "started"
    connection.close()

    recovered_tool = EffectTool(effect_path)
    connection, restarted, _ = _engine(
        database, tmp_path, recovered_tool, task_id=task_id
    )
    waiting = restarted.resume(task_id)
    assert waiting.status is ResultStatus.WAITING
    assert waiting.wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    assert recovered_tool.calls == 0
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    wait_token = restarted.checkpoint_store.get_active(task_id)["wait_token"]
    pending = restarted.list_tool_reconciliations(task_id)
    assert pending[0]["invocation_id"] == invocation_id
    assert pending[0]["wait_token"] == wait_token
    assert pending[0]["tool"] == "effect"
    assert restarted.run(task_id).wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    with pytest.raises(ValueError, match="required"):
        restarted.resolve_tool_invocation(
            task_id, wait_token, invocation_id, "owner", "completed", ""
        )
    with pytest.raises(ValueError, match="stale tool reconciliation token"):
        restarted.resolve_tool_invocation(
            task_id,
            "old-token",
            invocation_id,
            "owner",
            "completed",
            "inspection://effect.txt",
        )
    assert restarted.resolve_tool_invocation(
        task_id,
        wait_token,
        invocation_id,
        "owner",
        "completed",
        "inspection://effect.txt",
    )
    assert not restarted.resolve_tool_invocation(
        task_id,
        wait_token,
        invocation_id,
        "owner",
        "completed",
        "inspection://effect.txt",
    )
    assert restarted.list_tool_reconciliations(task_id) == []
    result = restarted.run(task_id)
    assert result.status is ResultStatus.SUCCESS
    assert recovered_tool.calls == 0
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    assert (
        connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        == "done"
    )
    connection.close()


def test_verified_no_effect_allows_one_retry(tmp_path):
    database = tmp_path / "no-effect.sqlite"
    effect_path = tmp_path / "effect.txt"
    connection, initial, task_id = _engine(
        database, tmp_path, EffectTool(effect_path, crash=True)
    )
    with pytest.raises(SystemExit, match="before effect"):
        initial.run(task_id)
    assert not effect_path.exists()
    invocation_id = connection.execute(
        "SELECT invocation_id FROM task_tool_invocations WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    connection.close()

    tool = EffectTool(effect_path)
    connection, restarted, _ = _engine(database, tmp_path, tool, task_id=task_id)
    assert restarted.resume(task_id).wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    wait_token = restarted.checkpoint_store.get_active(task_id)["wait_token"]
    assert restarted.resolve_tool_invocation(
        task_id, wait_token, invocation_id, "owner", "no_effect", "inspection://absent"
    )
    assert restarted.resume(task_id).status is ResultStatus.SUCCESS
    assert tool.calls == 1
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    row = connection.execute(
        "SELECT status, run_count FROM task_tool_invocations WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    assert (row["status"], row["run_count"]) == ("completed", 2)
    connection.close()


def test_cancel_invalidates_tool_reconciliation(tmp_path):
    database = tmp_path / "cancelled.sqlite"
    connection, orchestrator, task_id = _engine(
        database, tmp_path, EffectTool(tmp_path / "effect.txt", crash=True)
    )
    with pytest.raises(SystemExit):
        orchestrator.run(task_id)
    connection.close()

    connection, restarted, _ = _engine(
        database, tmp_path, EffectTool(tmp_path / "effect.txt"), task_id=task_id
    )
    assert restarted.resume(task_id).wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    request = restarted.list_tool_reconciliations(task_id)[0]
    assert restarted.cancel(task_id).status is ResultStatus.SUCCESS
    with pytest.raises(ValueError, match="not waiting"):
        restarted.resolve_tool_invocation(
            task_id,
            request["wait_token"],
            request["invocation_id"],
            "owner",
            "no_effect",
            "inspection://absent",
        )
    assert restarted.list_tool_reconciliations(task_id) == []
    connection.close()


def test_crash_after_journal_completion_before_engine_checkpoint_skips_tool(tmp_path):
    database = tmp_path / "completed-journal.sqlite"
    effect_path = tmp_path / "effect.txt"
    connection, initial, task_id = _engine(database, tmp_path, EffectTool(effect_path))

    def crash_before_checkpoint(*_args, **_kwargs):
        raise SystemExit("simulated crash before engine checkpoint")

    initial._finish_persistent_execution = crash_before_checkpoint
    with pytest.raises(SystemExit, match="before engine checkpoint"):
        initial.run(task_id)
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    assert (
        connection.execute(
            "SELECT status FROM task_tool_invocations WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "completed"
    )
    connection.close()

    tool = EffectTool(effect_path)
    connection, restarted, _ = _engine(database, tmp_path, tool, task_id=task_id)
    assert restarted.resume(task_id).status is ResultStatus.SUCCESS
    assert tool.calls == 0
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    connection.close()


def test_journal_completion_error_becomes_reconciliation_wait(tmp_path):
    database = tmp_path / "journal-error.sqlite"
    effect_path = tmp_path / "effect.txt"
    connection, orchestrator, task_id = _engine(
        database, tmp_path, EffectTool(effect_path)
    )

    def failed_journal_write(_task_id, _invocation_id, _step):
        raise OSError("transient journal write failure")

    orchestrator.unit_of_work.complete_tool_invocation = failed_journal_write
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.WAITING
    assert result.wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    assert (
        connection.execute(
            "SELECT status FROM task_tool_invocations WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "started"
    )
    connection.close()


def test_readback_step_stays_live_while_write_step_is_journaled(tmp_path):
    database = initialize_database(tmp_path / "readback.sqlite")
    connection = connect(database)
    stores = StoreFactory.create(connection, tmp_path)
    projects = ProjectStore(connection)
    project_id = projects.create("Readback", str(tmp_path))
    task_id = stores.task_store.create("Write then read", project_id=project_id)
    stores.task_store.transition(task_id, "ready")
    stores.task_store.approve(task_id, "owner")
    registry = ToolRegistry()
    registry.register(FilesystemTool(tmp_path))

    class ReadbackPlanner:
        def plan(self, _context):
            return EngineResult.success(
                "planned",
                plan=ExecutionPlan(
                    "write and verify",
                    [
                        PlanStep(
                            "write",
                            "Write file",
                            "write",
                            "filesystem",
                            {
                                "operation": "write",
                                "path": "proof.txt",
                                "content": "verified",
                            },
                        ),
                        PlanStep(
                            "read",
                            "Read file",
                            "read",
                            "filesystem",
                            {"operation": "read", "path": "proof.txt"},
                        ),
                    ],
                ),
            )

    builder = ContextBuilder.from_stores(stores, projects, tool_registry=registry)
    orchestrator = Orchestrator(
        builder,
        planner=ReadbackPlanner(),
        executor=Executor(registry, ToolSecurityPolicy(require_approval=False)),
        validator=Validator(),
        stores=stores,
        execution_config=ExecutionConfig(dry_run=False, persistence_mode="required"),
    )
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.SUCCESS
    assert (tmp_path / "proof.txt").read_text(encoding="utf-8") == "verified"
    assert (
        connection.execute(
            "SELECT step_id FROM task_tool_invocations WHERE task_id = ?", (task_id,)
        ).fetchall()[0][0]
        == "write"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM task_tool_invocations WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_real_process_exit_after_tool_effect_never_replays_on_restart(tmp_path):
    database = tmp_path / "process-crash.sqlite"
    effect_path = tmp_path / "effect.txt"
    connection, _, task_id = _engine(database, tmp_path, EffectTool(effect_path))
    connection.commit()
    connection.close()

    child = subprocess.run(
        [sys.executable, __file__, str(database), str(tmp_path), str(task_id)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 23, child.stderr
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    with connect(database) as connection:
        assert (
            connection.execute(
                "SELECT status FROM task_tool_invocations WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            == "started"
        )
        connection.execute(
            "UPDATE tasks SET claim_expires_at = '2000-01-01 00:00:00' WHERE id = ?",
            (task_id,),
        )

    recovered_tool = EffectTool(effect_path)
    connection, restarted, _ = _engine(
        database, tmp_path, recovered_tool, task_id=task_id
    )
    assert restarted.resume(task_id).wait_reason is WaitReason.TOOL_OUTCOME_UNKNOWN
    assert recovered_tool.calls == 0
    assert effect_path.read_text(encoding="utf-8") == "effect\n"
    connection.close()


def test_test_runner_is_journaled_despite_read_permission():
    assert Executor._has_external_effect(
        PlanStep("tests", "Run checks", "run", "test_runner"), PermissionLevel.READ
    )


if __name__ == "__main__":
    db_path, workspace_path, task = sys.argv[1:]
    connection, orchestrator, _ = _engine(
        Path(db_path),
        Path(workspace_path),
        ProcessExitingTool(Path(workspace_path) / "effect.txt"),
        task_id=int(task),
    )
    orchestrator.run(int(task))
    connection.close()
