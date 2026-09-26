"""Standard results returned by engine components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ResultStatus(StrEnum):
    """Lifecycle outcome shared by planner, executor and validator."""

    SUCCESS = "success"
    FAILED = "failed"
    WAITING = "waiting"


class ExecutionStatus(StrEnum):
    """Outcome of an individual execution or execution run."""

    SUCCESS = "success"
    FAILED = "failed"
    WAITING = "waiting"
    SKIPPED = "skipped"


class NextAction(StrEnum):
    """Workflow action suggested after execution."""

    VALIDATE = "validate"
    RETRY_EXECUTION = "retry_execution"
    REPLAN = "replan"
    WAIT = "wait"
    STOP = "stop"


class WaitReason(StrEnum):
    """Stable, explicit reason for a new waiting phase."""

    APPROVAL = "approval"
    EXTERNAL_INFORMATION = "external_information"
    TEMPORARY_ERROR = "temporary_error"
    WORKSPACE_MISSING = "workspace_missing"
    MANUAL_REPLAN = "manual_replan"
    HUMAN_INPUT = "human_input"
    PLAN_REVIEW = "plan_review"
    TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"


class ExecutionErrorType(StrEnum):
    """Stable categories used to select the next orchestration action."""

    TOOL_NOT_FOUND = "tool_not_found"
    UNAUTHORIZED_TOOL = "unauthorized_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    TOOL_FAILURE = "tool_failure"
    WORKSPACE_ERROR = "workspace_error"
    EXTERNAL_BLOCKER = "external_blocker"
    INVALID_NEXT_ACTION = "invalid_next_action"


@dataclass(frozen=True, slots=True)
class ExecutionError:
    """Structured execution failure passed between engine stages."""

    error_type: ExecutionErrorType
    message: str
    tool: str | None = None
    step_id: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """Stable, persistence-friendly description of an unexpected failure."""

    component: str
    error_class: str
    message: str
    retryable: bool
    recovery_action: NextAction
    details: dict[str, Any] = field(default_factory=dict)


class InvalidNextActionError(ValueError):
    """Raised when a stage returns an unsupported workflow action."""

    def __init__(self, value: object) -> None:
        self.error = ExecutionError(
            ExecutionErrorType.INVALID_NEXT_ACTION,
            f"Unknown next_action: {value}",
            details={"value": value},
        )
        super().__init__(self.error.message)


def action_for_error(error: ExecutionError) -> NextAction:
    """Map a structured error to the next safe workflow action."""
    if error.error_type is ExecutionErrorType.EXTERNAL_BLOCKER:
        return NextAction.WAIT
    if error.error_type is ExecutionErrorType.WORKSPACE_ERROR:
        if error.details.get("permission_required"):
            return (
                NextAction.WAIT
                if error.details.get("approval_expected")
                else NextAction.REPLAN
            )
        return NextAction.RETRY_EXECUTION if error.retryable else NextAction.REPLAN
    if error.error_type is ExecutionErrorType.TOOL_FAILURE and error.retryable:
        return NextAction.RETRY_EXECUTION
    return NextAction.REPLAN


@dataclass(slots=True)
class EngineResult:
    """Portable result passed between engine components and the orchestrator."""

    status: ResultStatus
    message: str = ""
    errors: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    tests: dict[str, bool] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    wait_reason: WaitReason | None = None
    interaction_request: dict[str, Any] | None = None

    @property
    def successful(self) -> bool:
        """Return whether the operation completed successfully."""
        return self.status is ResultStatus.SUCCESS

    @classmethod
    def success(cls, message: str = "", **data: Any) -> EngineResult:
        """Create a successful result."""
        return cls(status=ResultStatus.SUCCESS, message=message, data=data)

    @classmethod
    def failure(cls, message: str, *errors: str) -> EngineResult:
        """Create a failed result with optional error details."""
        return cls(
            status=ResultStatus.FAILED,
            message=message,
            errors=list(errors),
        )

    @classmethod
    def waiting(
        cls,
        message: str,
        reason: WaitReason | None = None,
        *,
        interaction_request: dict[str, Any] | None = None,
    ) -> EngineResult:
        """Create a result indicating that human input or approval is needed."""
        return cls(
            status=ResultStatus.WAITING,
            message=message,
            wait_reason=reason,
            interaction_request=interaction_request,
        )


@dataclass(slots=True)
class ToolExecutionResult:
    """Normalized, persistence-friendly output from one tool call."""

    output: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    error: str | None = None


@dataclass(slots=True)
class StepExecution:
    """Result of executing one plan step."""

    step_id: str
    status: ExecutionStatus
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    artifacts: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    acceptance_criteria: list[int] = field(default_factory=list)
    test_criteria: list[int] = field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    """Structured execution details returned by the executor."""

    status: ExecutionStatus
    steps: list[StepExecution] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_details: list[ExecutionError] = field(default_factory=list)
    next_action: NextAction | None = None
    wait_reason: WaitReason | None = None
    attempt_id: int | None = None
    dry_run: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
