"""Build execution contexts from the harness data sources."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from harness.engine.context import (
    AcceptanceCriterion,
    DependencyContext,
    ExecutionContext,
    TestCriterion,
)


class ContextBuilder:
    """Assemble one complete :class:`ExecutionContext` for a task run."""

    def __init__(
        self,
        task_store: Any,
        event_store: Any,
        artifact_store: Any,
        project_store: Any,
        *,
        knowledge_loader: Callable[[int], Iterable[str]] | None = None,
        tool_registry: Any | None = None,
        dry_run: bool = False,
    ) -> None:
        self.task_store = task_store
        self.event_store = event_store
        self.artifact_store = artifact_store
        self.project_store = project_store
        self.knowledge_loader = knowledge_loader
        self.tool_registry = tool_registry
        self.dry_run = dry_run

    def build(self, task_id: int) -> ExecutionContext:
        """Load all sources and create a consistent task snapshot."""
        task = self.task_store.get(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")

        project = self._project(task["project_id"])
        event_dicts = [dict(row) for row in self.event_store.list_for_task(task_id)]
        last_plan, last_validation, replanning_reasons = self._feedback(event_dicts)
        context = ExecutionContext(
            task_id=task["id"],
            task_title=task["title"],
            task_description=task["description"] or "",
            task_type=task["task_type"],
            priority=task["priority"],
            task_status=task["status"],
            approval_status=task["approval_status"],
            assigned_agent=task["assigned_agent"],
            acceptance_criteria=[
                AcceptanceCriterion(row["id"], row["criterion"], bool(row["completed"]))
                for row in self.task_store.criteria(task_id)
            ],
            test_criteria=[
                TestCriterion(
                    row["id"],
                    row["criterion"],
                    row["test_type"],
                    row["command"],
                    bool(row["completed"]),
                )
                for row in self.task_store.test_criteria(task_id)
            ],
            dependencies=[
                self._dependency(row) for row in self.task_store.dependencies(task_id)
            ],
            previous_events=event_dicts,
            previous_attempts=[dict(row) for row in self.task_store.attempts(task_id)],
            artifacts=[dict(row) for row in self.artifact_store.list_for_task(task_id)],
            available_tools=self._tools(),
            workspace=project["path"] if project is not None else None,
            dry_run=self.dry_run,
            last_plan=last_plan,
            last_validation=last_validation,
            replanning_reasons=replanning_reasons,
        )
        if self.knowledge_loader is not None:
            context.knowledge_documents.extend(self.knowledge_loader(task_id))
        return context

    def _project(self, project_id: int | None) -> Any:
        return self.project_store.get(project_id) if project_id is not None else None

    def _dependency(self, row: Any) -> DependencyContext:
        dependency = self.task_store.get(row["depends_on_task_id"])
        status = dependency["status"] if dependency is not None else "missing"
        return DependencyContext(
            task_id=row["depends_on_task_id"],
            status=status,
            dependency_type=row["dependency_type"],
            resolved=status == "done",
        )

    def _tools(self) -> list[str]:
        if self.tool_registry is None:
            return []
        return [definition.name for definition in self.tool_registry.list()]

    @staticmethod
    def _feedback(
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        last_plan = None
        last_validation = None
        reasons = []
        for event in events:
            payload = event.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            if not isinstance(payload, dict):
                continue
            if event.get("event_type") == "task.plan.created":
                last_plan = payload
            elif event.get("event_type") == "task.validation.completed":
                last_validation = payload
            elif event.get("event_type") == "task.replanning":
                reasons.append(payload)
        return last_plan, last_validation, reasons
