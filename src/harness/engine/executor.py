"""Execute plan steps through authorized tools."""

from __future__ import annotations

import json

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
    WaitReason,
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
        context.active_plan = plan
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
                wait_reason=WaitReason.WORKSPACE_MISSING,
            )
            return EngineResult(
                status=ResultStatus.WAITING,
                message=error.message,
                errors=[error.message],
                wait_reason=WaitReason.WORKSPACE_MISSING,
                data={"execution": execution, "next_action": "wait"},
            )

        executions: list[StepExecution] = []
        checkpoint = context.resume_checkpoint or {}
        completed_ids = checkpoint.get("completed_step_ids", [])
        if isinstance(completed_ids, str):
            try:
                completed_ids = json.loads(completed_ids)
            except json.JSONDecodeError:
                completed_ids = []
        completed = set(completed_ids or [])
        for step in plan.steps:
            if step.id in completed:
                executions.append(
                    StepExecution(
                        step.id,
                        ExecutionStatus.SKIPPED,
                        step.tool,
                        result="resumed",
                        acceptance_criteria=list(step.acceptance_criteria),
                        test_criteria=list(step.test_criteria),
                    )
                )
                continue
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
            if context.ownership_guard is not None:
                context.ownership_guard()
            try:
                tool = self.tool_registry.get(step.tool)
                self.security_policy.authorize(step, tool, context)
                result = self.tool_registry.execute(step.tool, **step.arguments)
                if context.ownership_guard is not None:
                    context.ownership_guard()
            except ToolError as error:
                structured = ExecutionError(
                    error.error_type,
                    str(error),
                    tool=step.tool,
                    step_id=step.id,
                    retryable=error.error_type is ExecutionErrorType.TOOL_FAILURE,
                    details=error.details,
                )
                next_action = action_for_error(structured)
                execution = ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    steps=executions,
                    errors=[str(error)],
                    error_details=[structured],
                    next_action=next_action,
                    wait_reason=(
                        WaitReason.APPROVAL
                        if next_action is NextAction.WAIT
                        and structured.details.get("approval_required")
                        else WaitReason.EXTERNAL_INFORMATION
                        if next_action is NextAction.WAIT
                        and structured.error_type is ExecutionErrorType.EXTERNAL_BLOCKER
                        else WaitReason.APPROVAL
                        if next_action is NextAction.WAIT
                        else None
                    ),
                )
                return EngineResult(
                    status=ResultStatus.FAILED,
                    message="Plan tool authorization or execution failed.",
                    errors=execution.errors,
                    wait_reason=execution.wait_reason,
                    data={"execution": execution, "next_action": next_action},
                )
            normalized = (
                ToolExecutionResult(data=result)
                if step.tool == "test_runner" and isinstance(result, dict)
                else result
            )
            if (
                step.tool == "test_runner"
                and isinstance(normalized, ToolExecutionResult)
                and (
                    normalized.data.get("timed_out")
                    or normalized.data.get("exit_code") != 0
                )
            ):
                executions.append(
                    StepExecution(
                        step.id,
                        ExecutionStatus.FAILED,
                        step.tool,
                        result=normalized,
                        test_criteria=list(step.test_criteria),
                        changed_files=list(normalized.data.get("changed_files", [])),
                    )
                )
                test_error = ExecutionError(
                    ExecutionErrorType.TOOL_FAILURE,
                    "test command failed",
                    tool=step.tool,
                    step_id=step.id,
                    retryable=True,
                )
                failed = ExecutionResult(
                    ExecutionStatus.FAILED,
                    executions,
                    errors=[test_error.message],
                    error_details=[test_error],
                    next_action=NextAction.RETRY_EXECUTION,
                )
                return EngineResult(
                    ResultStatus.FAILED,
                    "Test command failed.",
                    errors=failed.errors,
                    data={
                        "execution": failed,
                        "next_action": NextAction.RETRY_EXECUTION,
                    },
                )
            executions.append(
                StepExecution(
                    step.id,
                    ExecutionStatus.SUCCESS,
                    step.tool,
                    result=normalized,
                    acceptance_criteria=list(step.acceptance_criteria),
                    test_criteria=list(step.test_criteria),
                    artifacts=(
                        list(normalized.data.get("artifacts", []))
                        if isinstance(normalized, ToolExecutionResult)
                        else []
                    ),
                    changed_files=(
                        list(normalized.data.get("changed_files", []))
                        if isinstance(normalized, ToolExecutionResult)
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
