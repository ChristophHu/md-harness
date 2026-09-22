"""Execution context assembled for one task run."""

from __future__ import annotations

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
    command: str | None = None
    completed: bool = False


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
