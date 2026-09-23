"""Execute plan steps through authorized tools."""

from __future__ import annotations

from harness.engine.context import ExecutionContext
from harness.engine.plan import ExecutionPlan
from harness.engine.result import (
    EngineResult,
    ExecutionError,
    ExecutionErrorType,
    ExecutionResult,
    ExecutionStatus,
    NextAction,
    ResultStatus,
    StepExecution,
    ToolExecutionResult,
    action_for_error,
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
            error = ExecutionError(
                ExecutionErrorType.EXTERNAL_BLOCKER,
                "Target workspace is missing.",
                details={"reason": "workspace_required"},
            )
            execution = ExecutionResult(
                ExecutionStatus.WAITING,
                errors=[error.message],
                error_details=[error],
                next_action=NextAction.WAIT,
            )
            return EngineResult(
                status=ResultStatus.WAITING,
                message=error.message,
                errors=[error.message],
                data={"execution": execution, "next_action": "wait"},
            )

        executions: list[StepExecution] = []
        for step in plan.steps:
            if step.action == "skip":
                executions.append(
                    StepExecution(step.id, ExecutionStatus.SKIPPED, result="skipped")
                )
                continue
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
                structured = ExecutionError(
                    error.error_type,
                    str(error),
                    tool=step.tool,
                    step_id=step.id,
                    retryable=error.error_type is ExecutionErrorType.TOOL_FAILURE,
                )
                next_action = action_for_error(structured)
                execution = ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    steps=executions,
                    errors=[str(error)],
                    error_details=[structured],
                    next_action=next_action,
                )
                return EngineResult(
                    status=ResultStatus.FAILED,
                    message="Plan tool authorization or execution failed.",
                    errors=execution.errors,
                    data={"execution": execution, "next_action": next_action},
                )
            executions.append(
                StepExecution(
                    step.id,
                    ExecutionStatus.SUCCESS,
                    step.tool,
                    result=result,
                    acceptance_criteria=list(step.acceptance_criteria),
                    test_criteria=list(step.test_criteria),
                    artifacts=(
                        list(result.data.get("artifacts", []))
                        if isinstance(result, ToolExecutionResult)
                        else []
                    ),
                    changed_files=(
                        list(result.data.get("changed_files", []))
                        if isinstance(result, ToolExecutionResult)
                        else []
                    ),
                )
            )

        execution = ExecutionResult(
            ExecutionStatus.SUCCESS,
            steps=executions,
            artifacts=sorted({path for step in executions for path in step.artifacts}),
            changed_files=sorted(
                {path for step in executions for path in step.changed_files}
            ),
        )
        return EngineResult.success("Plan executed", execution=execution)
