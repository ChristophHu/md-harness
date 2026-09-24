"""Execution context assembled for one task run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AcceptanceCriterion:
    """One business acceptance criterion belonging to a task."""

    id: int
    criterion: str
    completed: bool = False


@dataclass(slots=True)
class TestCriterion:
    """One technical validation criterion belonging to a task."""

    id: int
    criterion: str
    test_type: str = "automated"
    command: str | list[str] | None = None
    timeout_seconds: float = 120.0
    working_directory: str | None = None
    completed: bool = False


@dataclass(slots=True)
class FileChange:
    path: str
    operation: str
    content: str | None = None
    expected_content: str | None = None
    search: str | None = None
    replacement: str | None = None
    acceptance_criteria: list[int] = field(default_factory=list)


@dataclass(slots=True)
class TestRequest:
    command: list[str]
    timeout_seconds: float = 120.0
    working_directory: str | None = None
    test_criteria: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ChangeRequest:
    files: list[FileChange] = field(default_factory=list)
    tests: list[TestRequest] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Any) -> ChangeRequest:
        if not isinstance(value, dict):
            raise TypeError("change must be a mapping")
        files = []
        for item in value.get("files", []):
            if not isinstance(item, dict):
                raise TypeError("file change must be a mapping")
            files.append(
                FileChange(
                    **{
                        key: item[key]
                        for key in (
                            "path",
                            "operation",
                            "content",
                            "expected_content",
                            "search",
                            "replacement",
                            "acceptance_criteria",
                        )
                        if key in item
                    }
                )
            )
        tests = []
        for item in value.get("tests", []):
            if not isinstance(item, dict) or not isinstance(item.get("command"), list):
                raise TypeError("test request command must be a list")
            tests.append(
                TestRequest(
                    **{
                        key: item[key]
                        for key in (
                            "command",
                            "timeout_seconds",
                            "working_directory",
                            "test_criteria",
                        )
                        if key in item
                    }
                )
            )
        return cls(files=files, tests=tests)


@dataclass(slots=True)
class DependencyContext:
    """Resolved dependency information for one task dependency."""

    task_id: int
    status: str
    dependency_type: str = "blocks"
    resolved: bool = False


@dataclass(slots=True)
class ExecutionContext:
    """Context shared by planning, execution and validation.

    The context deliberately contains references and normalized values only.
    Persistence remains the responsibility of the task, knowledge and storage
    layers.
    """

    task_id: int
    task_title: str
    task_description: str = ""
    task_type: str = "task"
    priority: str = "normal"
    task_status: str = "created"
    approval_status: str = "pending"
    assigned_agent: str | None = None
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    test_criteria: list[TestCriterion] = field(default_factory=list)
    dependencies: list[DependencyContext] = field(default_factory=list)
    knowledge_documents: list[str] = field(default_factory=list)
    previous_events: list[dict[str, Any]] = field(default_factory=list)
    previous_attempts: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_plan: dict[str, Any] | None = None
    last_validation: dict[str, Any] | None = None
    replanning_reasons: list[dict[str, Any]] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    workspace: str | None = None
    dry_run: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    resume_checkpoint: dict[str, Any] | None = None
    external_information_ref: str | None = None
    ownership_guard: Callable[[], None] | None = None
    tool_invocation_begin: Callable[[Any, Any, str], tuple[str, Any]] | None = None
    tool_invocation_complete: Callable[[str, Any], None] | None = None
    tool_invocation_pending: Callable[[], str | None] | None = None
    approval_checker: Callable[[Any, Any], bool] | None = None
    active_plan: Any | None = None
    hitl_mode: str = "minimal"
    human_responses: list[dict[str, Any]] = field(default_factory=list)

    def add_knowledge_document(self, document: str) -> None:
        """Add a knowledge document once while preserving insertion order."""
        if document not in self.knowledge_documents:
            self.knowledge_documents.append(document)

    def add_event(self, event: dict[str, Any]) -> None:
        """Add an event to the in-memory context."""
        self.previous_events.append(event)

    def add_attempt(self, attempt: dict[str, Any]) -> None:
        """Add a previous execution attempt to the context."""
        self.previous_attempts.append(attempt)

    def add_artifact(self, artifact: dict[str, Any]) -> None:
        """Add a generated artifact reference to the context."""
        self.artifacts.append(artifact)
