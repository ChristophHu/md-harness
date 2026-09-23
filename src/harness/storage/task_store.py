"""Task persistence and state transition operations."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any, ClassVar


class TaskStore:
    TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "created": {"ready", "failed", "cancelled"},
        "ready": {"planning", "failed", "cancelled"},
        "planning": {"executing", "waiting", "failed", "cancelled"},
        "executing": {"validating", "planning", "waiting", "failed", "cancelled"},
        "validating": {
            "done",
            "executing",
            "planning",
            "waiting",
            "failed",
            "cancelled",
        },
        "waiting": {"planning", "executing", "validating", "failed", "cancelled"},
        "done": set(),
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

    def __init__(
        self, connection: Connection, *, clock: Callable[[], datetime] | None = None
    ):
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(UTC))

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

    def claim(
        self, task_id: int, *, run_id: str | None = None, lease_seconds: int = 300
    ) -> bool:
        """Atomically claim an approved ready task for orchestration."""
        token = run_id or str(uuid.uuid4())
        expires = (self.clock() + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = self.connection.execute(
            """UPDATE tasks SET status = CASE WHEN status = 'ready' THEN 'planning' ELSE status END,
                planning_started_at = COALESCE(planning_started_at, CURRENT_TIMESTAMP),
                claim_token = ?, claimed_at = CURRENT_TIMESTAMP, claim_expires_at = ?,
                execution_epoch = execution_epoch + 1
                WHERE id = ? AND status IN ('ready', 'planning', 'executing', 'validating')
                AND approval_status = 'approved'
                AND (claim_expires_at IS NULL OR claim_expires_at < ?)""",
            (token, expires, task_id, self._now_sql()),
        )
        return cursor.rowcount == 1

    def renew_claim(self, task_id: int, run_id: str, lease_seconds: int = 300) -> bool:
        expires = (self.clock() + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = self.connection.execute(
            """UPDATE tasks SET claim_expires_at = ?
               WHERE id = ? AND claim_token = ?
               AND claim_expires_at >= ?""",
            (expires, task_id, run_id, self._now_sql()),
        )
        return cursor.rowcount == 1

    def release_claim(self, task_id: int, run_id: str) -> bool:
        cursor = self.connection.execute(
            """UPDATE tasks SET claim_token = NULL, claimed_at = NULL,
               claim_expires_at = NULL
               WHERE id = ? AND claim_token = ?""",
            (task_id, run_id),
        )
        return cursor.rowcount == 1

    def cancel_active_attempts(self, task_id: int, reason: str) -> int:
        """Finish every still-running attempt as cancelled."""
        cursor = self.connection.execute(
            """UPDATE task_attempts
               SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP,
                   error_message = ?
               WHERE task_id = ? AND completed_at IS NULL
               AND status = 'running'""",
            (reason, task_id),
        )
        return cursor.rowcount

    def cancel(self, task_id: int, reason: str) -> int:
        """Cancel a task after validating its state transition."""
        task = self._require_task(task_id)
        if "cancelled" not in self.TRANSITIONS.get(task["status"], set()):
            raise ValueError(f"invalid transition: {task['status']} -> cancelled")
        self.cancel_active_attempts(task_id, reason)
        cursor = self.connection.execute(
            """UPDATE tasks SET status = 'cancelled', claim_token = NULL,
               execution_epoch = execution_epoch + 1,
               claimed_at = NULL, claim_expires_at = NULL
               WHERE id = ?""",
            (task_id,),
        )
        return cursor.rowcount

    def assert_claim(self, task_id: int, run_id: str) -> None:
        task = self.get(task_id)
        if task is None or task["claim_token"] != run_id:
            raise RuntimeError("task claim is not owned by this run")
        if (
            task["claim_expires_at"] is not None
            and task["claim_expires_at"] < self._now_sql()
        ):
            raise RuntimeError("task claim has expired")

    def _now_sql(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")

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
            WHERE t.approval_status = 'approved' AND t.status IN ('ready', 'planning')
            AND (? IS NULL OR t.assigned_agent = ?)
            AND NOT EXISTS (
                SELECT 1 FROM task_dependencies d JOIN tasks dependency
                ON dependency.id = d.depends_on_task_id
                WHERE d.task_id = t.id AND d.dependency_type = 'blocks'
                AND dependency.status != 'done'
            ) ORDER BY t.priority, t.id"""
        return self.connection.execute(query, (agent, agent)).fetchall()

    def add_test_criterion(
        self,
        task_id: int,
        criterion: str,
        *,
        test_type: str = "automated",
        command: str | list[str] | None = None,
        timeout_seconds: float = 120.0,
        working_directory: str | None = None,
    ) -> int:
        return self.connection.execute(
            "INSERT INTO task_test_criteria (task_id, criterion, test_type, command, timeout_seconds, working_directory) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                criterion,
                test_type,
                json.dumps(command) if isinstance(command, list) else command,
                timeout_seconds,
                working_directory,
            ),
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
        if (
            status in {"planning", "executing"}
            and task["approval_status"] != "approved"
        ):
            raise ValueError("task must be approved before planning or execution")
        self.connection.execute(
            """UPDATE tasks SET status = ?,
                started_at = CASE WHEN ? = 'executing' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END,
                planning_started_at = CASE WHEN ? = 'planning' THEN COALESCE(planning_started_at, CURRENT_TIMESTAMP) ELSE planning_started_at END,
                validation_started_at = CASE WHEN ? = 'validating' THEN COALESCE(validation_started_at, CURRENT_TIMESTAMP) ELSE validation_started_at END,
                failed_at = CASE WHEN ? = 'failed' THEN CURRENT_TIMESTAMP ELSE failed_at END,
                completed_at = CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE completed_at END
                WHERE id = ?""",
            (status, status, status, status, status, status, task_id),
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
        *,
        run_id: str | None = None,
        claim_token: str | None = None,
        lease_seconds: int = 300,
    ) -> int:
        if claim_token is not None:
            self.assert_claim(task_id, claim_token)
        active = self.connection.execute(
            "SELECT 1 FROM task_attempts WHERE task_id = ? AND completed_at IS NULL AND status = 'running'",
            (task_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("task already has an active attempt")
        expires = (self.clock() + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return self.connection.execute(
            "INSERT INTO task_attempts (task_id, agent, status, error_message, run_id, claim_token, claim_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                agent,
                status,
                error_message,
                run_id,
                claim_token,
                expires if run_id else None,
            ),
        ).lastrowid

    def attempts(self, task_id: int) -> list[Row]:
        """Return execution attempts for a task in creation order."""
        return self.connection.execute(
            "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def complete_attempt(
        self,
        attempt_id: int,
        status: str,
        error_message: str | None = None,
        *,
        claim_token: str | None = None,
    ) -> None:
        """Complete an execution attempt with its final status."""
        if claim_token is not None:
            attempt = self.connection.execute(
                "SELECT claim_token, claim_expires_at FROM task_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["claim_token"] != claim_token:
                raise RuntimeError("attempt claim is not owned by this run")
            if (
                attempt["claim_expires_at"] is not None
                and attempt["claim_expires_at"] < self._now_sql()
            ):
                raise RuntimeError("attempt claim has expired")
        self.connection.execute(
            "UPDATE task_attempts SET status = ?, completed_at = CURRENT_TIMESTAMP, error_message = ? WHERE id = ? AND (? IS NULL OR claim_token = ?)",
            (status, error_message, attempt_id, claim_token, claim_token),
        )

    def record_replan(
        self,
        task_id: int,
        reason_code: str,
        plan_version: int,
        *,
        parent_plan_version: int | None = None,
        attempt_id: int | None = None,
        reason_fingerprint: str | None = None,
        plan_fingerprint: str | None = None,
    ) -> int:
        fingerprint = reason_fingerprint or reason_code
        return self.connection.execute(
            """INSERT INTO task_replans
               (task_id, attempt_id, parent_plan_version, plan_version,
                reason_code, reason_fingerprint, plan_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                attempt_id,
                parent_plan_version,
                plan_version,
                reason_code,
                fingerprint,
                plan_fingerprint,
            ),
        ).lastrowid

    def replans(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_replans WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
