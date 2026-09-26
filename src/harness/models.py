"""Domain models corresponding to the harness SQLite schema."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Project:
    id: int | None
    name: str
    path: str
    parent_id: int | None = None
    description: str = ""
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Task:
    id: int | None
    project_id: int | None
    parent_id: int | None
    external_key: str | None
    title: str
    description: str | None = None
    task_type: str = "task"
    status: str = "created"
    priority: str = "normal"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    approval_status: str = "pending"
    assigned_agent: str | None = None


@dataclass(slots=True)
class TaskDependency:
    task_id: int
    depends_on_task_id: int
    dependency_type: str = "blocks"


@dataclass(slots=True)
class TaskAcceptanceCriterion:
    id: int | None
    task_id: int
    criterion: str
    completed: bool = False


@dataclass(slots=True)
class TaskTestCriterion:
    id: int | None
    task_id: int
    criterion: str
    test_type: str = "automated"
    command: str | None = None
    completed: bool = False


@dataclass(slots=True)
class TaskAttempt:
    id: int | None
    task_id: int
    agent: str | None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


@dataclass(slots=True)
class TaskEvent:
    id: int | None
    task_id: int
    event_type: str
    payload: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class TaskArtifact:
    id: int | None
    task_id: int
    path: str
    artifact_type: str | None = None
    checksum: str | None = None
    created_at: datetime | None = None
