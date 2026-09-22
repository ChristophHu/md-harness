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


@dataclass(slots=True)
class EngineResult:
    """Portable result passed between engine components and the orchestrator."""

    status: ResultStatus
    message: str = ""
    errors: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    tests: dict[str, bool] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

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
    def waiting(cls, message: str) -> EngineResult:
        """Create a result indicating that human input or approval is needed."""
        return cls(status=ResultStatus.WAITING, message=message)


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
    next_action: NextAction | None = None
    attempt_id: int | None = None
    dry_run: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
