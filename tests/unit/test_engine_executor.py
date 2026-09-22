"""Tests for plan-authorized execution."""

from harness.engine.context import ExecutionContext
from harness.engine.executor import Executor
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import ExecutionStatus, ResultStatus
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


def test_executor_supports_noop_steps():
    plan = ExecutionPlan("Task", [PlanStep("inspect", "Inspect", "inspect")])

    result = Executor(ToolRegistry()).execute(context(available_tools=[]), plan)

    assert result.successful is True
    assert result.data["execution"].steps[0].result == "no-op"


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
