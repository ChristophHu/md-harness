"""Tests for tool security authorization."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.engine.context import ExecutionContext
from harness.engine.plan import PlanStep
from harness.security.tool_policy import ToolSecurityPolicy
from harness.tools.base import PermissionLevel, Tool, ToolDefinition, ToolError


@dataclass
class PolicyTool(Tool):
    name: str
    permission: PermissionLevel
    root: Path | None = None

    def __post_init__(self):
        Tool.__init__(self)
        self.definition = ToolDefinition(self.name, "test", self.permission)

    def execute(self, **_arguments):
        return None


def context(**overrides):
    values = {
        "task_id": 1,
        "task_title": "Task",
        "workspace": "/workspace",
        "available_tools": ["tool"],
        "approval_status": "approved",
    }
    values.update(overrides)
    return ExecutionContext(**values)


def authorize(tool, **overrides):
    ToolSecurityPolicy(**overrides).authorize(
        PlanStep("step", "run", "run", tool.name), tool, context()
    )


def test_policy_allows_approved_read_tool():
    authorize(PolicyTool("tool", PermissionLevel.READ))


def test_policy_checks_plan_and_availability():
    tool = PolicyTool("tool", PermissionLevel.READ)
    with pytest.raises(ToolError, match="not authorized"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "other"), tool, context()
        )
    with pytest.raises(ToolError, match="not available"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "tool"), tool, context(available_tools=[])
        )


def test_policy_checks_network_and_destructive_permissions():
    with pytest.raises(ToolError, match="network"):
        authorize(PolicyTool("tool", PermissionLevel.NETWORK))
    authorize(PolicyTool("tool", PermissionLevel.NETWORK), allow_network=True)
    with pytest.raises(ToolError, match="destructive"):
        authorize(PolicyTool("tool", PermissionLevel.DESTRUCTIVE))
    authorize(PolicyTool("tool", PermissionLevel.DESTRUCTIVE), allow_destructive=True)


def test_policy_checks_approval_and_dry_run():
    with pytest.raises(ToolError, match="approval"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "tool"),
            PolicyTool("tool", PermissionLevel.READ),
            context(approval_status="pending"),
        )
    authorize(PolicyTool("tool", PermissionLevel.WRITE), dry_run_allows_writes=True)
    with pytest.raises(ToolError, match="dry-run"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "tool"),
            PolicyTool("tool", PermissionLevel.WRITE),
            context(dry_run=True),
        )


def test_policy_checks_workspace_boundary():
    with pytest.raises(ToolError, match="workspace is required"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "tool"),
            PolicyTool("tool", PermissionLevel.READ),
            context(workspace=None),
        )
    with pytest.raises(ToolError, match="outside"):
        ToolSecurityPolicy().authorize(
            PlanStep("step", "run", "run", "tool"),
            PolicyTool("tool", PermissionLevel.READ, Path("/outside")),
            context(),
        )
    ToolSecurityPolicy().authorize(
        PlanStep("step", "run", "run", "tool"),
        PolicyTool("tool", PermissionLevel.READ, Path("/workspace/sub")),
        context(),
    )


def test_policy_can_disable_optional_checks():
    tool = PolicyTool("tool", PermissionLevel.WRITE, Path("/outside"))
    ToolSecurityPolicy(
        require_approval=False,
        enforce_workspace=False,
        dry_run_allows_writes=True,
    ).authorize(
        PlanStep("step", "run", "run", "tool"),
        tool,
        context(approval_status="pending", dry_run=True),
    )
