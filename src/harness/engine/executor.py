"""Execute plan steps through authorized tools."""

from __future__ import annotations

from harness.engine.context import ExecutionContext
from harness.engine.plan import ExecutionPlan
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    ResultStatus,
    StepExecution,
)
from harness.security.tool_policy import ToolSecurityPolicy
from harness.tools.base import ToolError, ToolRegistry


class Executor:
    """Execute only the tools explicitly referenced by an execution plan."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        security_policy: ToolSecurityPolicy | None = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.security_policy = security_policy or ToolSecurityPolicy()

    @property
    def registry(self) -> ToolRegistry:
        """Return the registry used for this executor."""
        return self.tool_registry

    def execute(self, context: ExecutionContext, plan: ExecutionPlan) -> EngineResult:
        """Execute every plan step and return structured results."""
        if not context.workspace:
            return EngineResult.waiting("Target workspace is missing.")

        executions: list[StepExecution] = []
        for step in plan.steps:
            if step.tool is None:
                executions.append(
                    StepExecution(step.id, ExecutionStatus.SUCCESS, result="no-op")
                )
                continue
            try:
                tool = self.tool_registry.get(step.tool)
                self.security_policy.authorize(step, tool, context)
                result = self.tool_registry.execute(step.tool, **step.arguments)
            except ToolError as error:
                execution = ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    steps=executions,
                    errors=[str(error)],
                    next_action="replan",
                )
                return EngineResult(
                    status=ResultStatus.FAILED,
                    message="Plan tool authorization or execution failed.",
                    errors=execution.errors,
                    data={"execution": execution, "next_action": "replan"},
                )
            executions.append(
                StepExecution(
                    step.id, ExecutionStatus.SUCCESS, step.tool, result=result
                )
            )

        execution = ExecutionResult(ExecutionStatus.SUCCESS, steps=executions)
        return EngineResult.success("Plan executed", execution=execution)
