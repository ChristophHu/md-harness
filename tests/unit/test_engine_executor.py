"""Tests for plan-authorized execution."""

import pytest

from harness.engine.context import ExecutionContext
from harness.engine.executor import Executor
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import ExecutionStatus, ResultStatus, ToolExecutionResult
from harness.security.tool_policy import ToolSecurityPolicy
from harness.tools.base import (
    PermissionLevel,
    Tool,
    ToolDefinition,
    ToolError,
    ToolParameter,
    ToolRegistry,
)


class EchoTool(Tool):
    definition = ToolDefinition(
        "echo",
        "Echo arguments",
        PermissionLevel.READ,
        (ToolParameter("value", "integer"),),
    )

    def execute(self, **arguments):
        return arguments


class RunnerTool(Tool):
    definition = ToolDefinition(
        "test_runner",
        "runner",
        PermissionLevel.READ,
        (ToolParameter("command", "list"),),
    )

    def __init__(self, result):
        super().__init__()
        self.result = result

    def execute(self, **_arguments):
        return self.result


def context(**overrides):
    values = {
        "task_id": 1,
        "task_title": "Task",
        "workspace": "/workspace",
        "available_tools": ["echo"],
        "approval_status": "approved",
    }
    values.update(overrides)
    return ExecutionContext(**values)


def test_executor_executes_only_plan_tools():
    registry = ToolRegistry()
    registry.register(EchoTool())
    plan = ExecutionPlan(
        "Task", [PlanStep("step", "Echo", "execute", "echo", {"value": 1})]
    )

    result = Executor(registry).execute(context(), plan)

    assert result.status is ResultStatus.SUCCESS
    execution = result.data["execution"]
    assert execution.status is ExecutionStatus.SUCCESS
    assert execution.steps[0].result == {"value": 1}


def test_executor_checks_ownership_before_and_after_each_tool():
    calls = []

    class RevokingTool(EchoTool):
        def execute(self, **arguments):
            calls.append("tool")
            return arguments

    registry = ToolRegistry()
    registry.register(RevokingTool())
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("claim revoked during tool")

    execution_context = context(ownership_guard=guard)
    plan = ExecutionPlan(
        "Task",
        [
            PlanStep("first", "First", "execute", "echo", {"value": 1}),
            PlanStep("second", "Second", "execute", "echo", {"value": 2}),
        ],
    )
    with pytest.raises(RuntimeError, match="claim revoked"):
        Executor(registry).execute(execution_context, plan)
    assert checks == 2
    assert calls == ["tool"]


def test_executor_normalizes_failed_test_runner_result():
    registry = ToolRegistry()
    registry.register(RunnerTool({"exit_code": 1, "passed": False, "timed_out": False}))
    plan = ExecutionPlan(
        "Task",
        [PlanStep("tests", "Tests", "run_tests", "test_runner", {"command": ["x"]})],
    )
    result = Executor(registry).execute(context(available_tools=["test_runner"]), plan)
    assert result.status is ResultStatus.FAILED
    assert result.data["execution"].next_action.value == "retry_execution"
    assert isinstance(result.data["execution"].steps[0].result, ToolExecutionResult)


def test_executor_supports_noop_steps():
    plan = ExecutionPlan("Task", [PlanStep("inspect", "Inspect", "inspect")])

    result = Executor(ToolRegistry()).execute(context(available_tools=[]), plan)

    assert result.successful is True
    assert result.data["execution"].steps[0].result == "no-op"


def test_executor_marks_explicitly_skipped_steps():
    plan = ExecutionPlan("Task", [PlanStep("skip", "Skip", "skip")])

    result = Executor(ToolRegistry()).execute(context(available_tools=[]), plan)

    step = result.data["execution"].steps[0]
    assert step.status is ExecutionStatus.SKIPPED
    assert step.result == "skipped"


def test_executor_preserves_tool_output_exit_code_and_changes():
    class ReportingTool(EchoTool):
        def execute(self, **_arguments):
            return ToolExecutionResult(
                output="updated",
                exit_code=0,
                data={"changed_files": ["src/app.py"], "artifacts": ["report.txt"]},
            )

    registry = ToolRegistry()
    registry.register(ReportingTool())
    plan = ExecutionPlan(
        "Task", [PlanStep("step", "Report", "execute", "echo", {"value": 1})]
    )

    result = Executor(registry).execute(context(), plan)
    execution = result.data["execution"]
    tool_result = execution.steps[0].result

    assert tool_result.output == "updated"
    assert tool_result.exit_code == 0
    assert execution.changed_files == ["src/app.py"]
    assert execution.artifacts == ["report.txt"]


def test_executor_waits_without_workspace():
    result = Executor(ToolRegistry()).execute(
        context(workspace=None), ExecutionPlan("Task")
    )

    assert result.status is ResultStatus.WAITING


def test_executor_replans_when_tool_is_not_in_context():
    registry = ToolRegistry()
    registry.register(EchoTool())
    plan = ExecutionPlan("Task", [PlanStep("step", "Echo", "execute", "echo")])

    result = Executor(registry).execute(context(available_tools=[]), plan)

    assert result.status is ResultStatus.FAILED
    assert result.data["next_action"] == "replan"
    assert result.data["execution"].next_action == "replan"


def test_executor_skips_completed_resume_steps():
    from harness.engine.plan import ExecutionPlan, PlanStep
    from harness.engine.result import ExecutionStatus

    registry = ToolRegistry()
    registry.register(RunnerTool({"passed": True, "exit_code": 0, "timed_out": False}))
    context_value = context(available_tools=["test_runner"])
    context_value.resume_checkpoint = {"completed_step_ids": '["first"]'}
    result = Executor(registry).execute(
        context_value,
        ExecutionPlan(
            "resume",
            [
                PlanStep("first", "First", "run", "test_runner", {"command": []}),
                PlanStep("second", "Second", "run", "test_runner", {"command": []}),
            ],
        ),
    )
    assert result.successful
    assert result.data["execution"].steps[0].status is ExecutionStatus.SKIPPED
    assert result.data["execution"].steps[0].result == "resumed"
    context_value.resume_checkpoint = {"completed_step_ids": "not-json"}
    fresh = Executor(registry).execute(
        context_value,
        ExecutionPlan(
            "resume",
            [PlanStep("second", "Second", "run", "test_runner", {"command": []})],
        ),
    )
    assert fresh.successful


def test_executor_replans_when_tool_is_not_registered():
    plan = ExecutionPlan("Task", [PlanStep("step", "Missing", "execute", "missing")])

    result = Executor(ToolRegistry()).execute(
        context(available_tools=["missing"]), plan
    )

    assert result.status is ResultStatus.FAILED
    assert result.data["next_action"] == "replan"


def test_policy_rejects_tool_not_matching_plan_step():
    tool = EchoTool()
    step = PlanStep("step", "Other", "execute", "other")

    try:
        ToolSecurityPolicy().authorize(step, tool, context())
    except ToolError as error:
        assert "not authorized" in str(error)
    else:
        raise AssertionError("unauthorized tool was accepted")
