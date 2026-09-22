"""Security policy for plan-authorized tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.engine.context import ExecutionContext
from harness.engine.plan import PlanStep
from harness.tools.base import PermissionLevel, Tool, ToolError


@dataclass(frozen=True, slots=True)
class ToolSecurityPolicy:
    """Authorize tools against plan, configuration and workspace boundaries."""

    allow_network: bool = False
    allow_destructive: bool = False
    require_approval: bool = True
    enforce_workspace: bool = True
    dry_run_allows_writes: bool = False

    def authorize(
        self,
        plan_step: PlanStep,
        tool: Tool,
        context: ExecutionContext,
    ) -> None:
        """Raise ``ToolError`` when a plan step may not use the tool."""
        if plan_step.tool != tool.tool_name:
            raise ToolError(
                f"tool '{tool.tool_name}' is not authorized by plan step '{plan_step.id}'"
            )
        if tool.tool_name not in context.available_tools:
            raise ToolError(f"tool '{tool.tool_name}' is not available in the context")
        permission = tool.definition.permission
        if permission is PermissionLevel.NETWORK and not self.allow_network:
            raise ToolError(f"network permission denied for tool '{tool.tool_name}'")
        if permission is PermissionLevel.DESTRUCTIVE and not self.allow_destructive:
            raise ToolError(
                f"destructive permission denied for tool '{tool.tool_name}'"
            )
        if self.require_approval and context.approval_status != "approved":
            raise ToolError("tool execution requires task approval")
        if (
            context.dry_run
            and permission
            in {
                PermissionLevel.WRITE,
                PermissionLevel.DESTRUCTIVE,
                PermissionLevel.NETWORK,
            }
            and not self.dry_run_allows_writes
        ):
            raise ToolError(f"tool '{tool.tool_name}' is not allowed during dry-run")
        if self.enforce_workspace:
            self._check_workspace(tool, context)

    @staticmethod
    def _check_workspace(tool: Tool, context: ExecutionContext) -> None:
        if not context.workspace:
            raise ToolError("workspace is required for tool execution")
        workspace = Path(context.workspace).expanduser().resolve()
        for attribute in ("root", "path"):
            value = getattr(tool, attribute, None)
            if value is None:
                continue
            try:
                Path(value).expanduser().resolve().relative_to(workspace)
            except ValueError as error:
                raise ToolError(
                    f"tool '{tool.tool_name}' is outside the configured workspace"
                ) from error
