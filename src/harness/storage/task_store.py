"""Task persistence and state transition operations."""

from sqlite3 import Connection, Row
from typing import Any, ClassVar


class TaskStore:
    TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "idea": {"backlog", "ready", "cancelled"},
        "created": {"ready", "cancelled"},
        "backlog": {"planned", "ready", "cancelled"},
        "ready": {"planned", "in_progress", "cancelled"},
        "planned": {"in_progress", "cancelled"},
        "in_progress": {"review", "failed", "blocked"},
        "review": {"completed", "in_progress"},
        "failed": {"in_progress", "cancelled"},
        "blocked": {"in_progress", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }
    FIELD_NAMES: ClassVar[set[str]] = {
        "title",
        "description",
        "task_type",
        "priority",
        "external_key",
        "parent_id",
        "project_id",
        "assigned_agent",
    }

    def __init__(self, connection: Connection):
        self.connection = connection

    def create(
        self, title: str, *, project_id: int | None = None, **fields: Any
    ) -> int:
        if set(fields) - self.FIELD_NAMES:
            raise ValueError("unsupported task field")
        columns = ["title", "project_id"] + list(fields)
        values = [title, project_id] + list(fields.values())
        placeholders = ", ".join("?" for _ in values)
        return self.connection.execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})", values
        ).lastrowid

    def get(self, task_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    def update(self, task_id: int, **fields: Any) -> None:
        if not fields:
            raise ValueError("at least one field is required")
        if set(fields) - self.FIELD_NAMES:
            raise ValueError("unsupported task field")
        self.connection.execute(
            f"UPDATE tasks SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
            [*fields.values(), task_id],
        )

    def list_ready(self) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE status = 'ready' ORDER BY priority, id"
        ).fetchall()

    def approve(
        self, task_id: int, approved_by: str, reason: str | None = None
    ) -> None:
        self._require_task(task_id)
        self.connection.execute(
            "UPDATE tasks SET approval_status = 'approved' WHERE id = ?", (task_id,)
        )
        self.connection.execute(
            "INSERT INTO task_approvals (task_id, status, approved_by, reason) VALUES (?, 'approved', ?, ?)",
            (task_id, approved_by, reason),
        )

    def revoke_approval(
        self, task_id: int, approved_by: str, reason: str | None = None
    ) -> None:
        self._require_task(task_id)
        self.connection.execute(
            "UPDATE tasks SET approval_status = 'revoked' WHERE id = ?", (task_id,)
        )
        self.connection.execute(
            "INSERT INTO task_approvals (task_id, status, approved_by, reason) VALUES (?, 'revoked', ?, ?)",
            (task_id, approved_by, reason),
        )

    def get_executable_tasks(self, agent: str | None = None) -> list[Row]:
        query = """SELECT t.* FROM tasks t
            WHERE t.approval_status = 'approved' AND t.status IN ('ready', 'planned')
            AND (? IS NULL OR t.assigned_agent = ?)
            AND NOT EXISTS (
                SELECT 1 FROM task_dependencies d JOIN tasks dependency
                ON dependency.id = d.depends_on_task_id
                WHERE d.task_id = t.id AND d.dependency_type = 'blocks'
                AND dependency.status != 'completed'
            ) ORDER BY t.priority, t.id"""
        return self.connection.execute(query, (agent, agent)).fetchall()

    def add_test_criterion(
        self,
        task_id: int,
        criterion: str,
        *,
        test_type: str = "automated",
        command: str | None = None,
    ) -> int:
        return self.connection.execute(
            "INSERT INTO task_test_criteria (task_id, criterion, test_type, command) VALUES (?, ?, ?, ?)",
            (task_id, criterion, test_type, command),
        ).lastrowid

    def test_criteria(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_test_criteria WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def _require_task(self, task_id: int) -> Row:
        task = self.get(task_id)
        if task is None:
            raise ValueError("task not found")
        return task

    def transition(self, task_id: int, status: str) -> None:
        task = self.get(task_id)
        if task is None:
            raise ValueError("task not found")
        if status not in self.TRANSITIONS.get(task["status"], set()):
            raise ValueError(f"invalid transition: {task['status']} -> {status}")
        if status == "in_progress" and task["approval_status"] != "approved":
            raise ValueError("task must be approved before execution")
        self.connection.execute(
            "UPDATE tasks SET status = ?, started_at = CASE WHEN ? = 'in_progress' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END, completed_at = CASE WHEN ? = 'completed' THEN CURRENT_TIMESTAMP ELSE completed_at END WHERE id = ?",
            (status, status, status, task_id),
        )

    def add_dependency(
        self, task_id: int, depends_on_task_id: int, dependency_type: str = "blocks"
    ) -> None:
        if task_id == depends_on_task_id:
            raise ValueError("a task cannot depend on itself")
        if self._depends_on(depends_on_task_id, task_id):
            raise ValueError("dependency cycle detected")
        self.connection.execute(
            "INSERT INTO task_dependencies VALUES (?, ?, ?)",
            (task_id, depends_on_task_id, dependency_type),
        )

    def _depends_on(
        self, task_id: int, target_id: int, visited: set[int] | None = None
    ) -> bool:
        visited = visited or set()
        if task_id in visited:
            return False
        visited.add(task_id)
        for row in self.connection.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        ):
            dependency_id = row[0]
            if dependency_id == target_id or self._depends_on(
                dependency_id, target_id, visited
            ):
                return True
        return False

    def dependencies(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_dependencies WHERE task_id = ?", (task_id,)
        ).fetchall()

    def add_criterion(self, task_id: int, criterion: str) -> int:
        return self.connection.execute(
            "INSERT INTO task_acceptance_criteria (task_id, criterion) VALUES (?, ?)",
            (task_id, criterion),
        ).lastrowid

    def complete_criterion(self, criterion_id: int) -> None:
        self.connection.execute(
            "UPDATE task_acceptance_criteria SET completed = 1 WHERE id = ?",
            (criterion_id,),
        )

    def criteria(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_acceptance_criteria WHERE task_id = ?", (task_id,)
        ).fetchall()

    def record_attempt(
        self,
        task_id: int,
        status: str,
        agent: str | None = None,
        error_message: str | None = None,
    ) -> int:
        return self.connection.execute(
            "INSERT INTO task_attempts (task_id, agent, status, error_message) VALUES (?, ?, ?, ?)",
            (task_id, agent, status, error_message),
        ).lastrowid
