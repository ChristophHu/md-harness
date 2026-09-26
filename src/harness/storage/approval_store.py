"""Persist grants bound to one checkpoint and one planned tool call."""

from __future__ import annotations

from sqlite3 import Connection, Row
from typing import Any


class ApprovalStore:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def get(self, wait_token: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_step_approvals WHERE wait_token = ?", (wait_token,)
        ).fetchone()

    def create(self, task_id: int, wait_token: str, request: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO task_step_approvals
               (wait_token, task_id, plan_version, plan_fingerprint, step_id,
                tool, arguments_fingerprint, permission_scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wait_token,
                task_id,
                request["plan_version"],
                request["plan_fingerprint"],
                request["step_id"],
                request["tool"],
                request["arguments_fingerprint"],
                request["permission_scope"],
            ),
        )

    def matches(self, task_id: int, wait_token: str, request: dict[str, Any]) -> bool:
        row = self.get(wait_token)
        return (
            row is not None
            and row["task_id"] == task_id
            and all(row[key] == value for key, value in request.items())
        )

    def ready(self, task_id: int, wait_token: str, request: dict[str, Any]) -> bool:
        if not self.matches(task_id, wait_token, request):
            return False
        row = self.get(wait_token)
        state = self.connection.execute(
            """SELECT t.approval_status, t.status, c.wait_token,
                      c.waiting_reason_code, c.invalidated_at
               FROM tasks t JOIN task_checkpoints c ON c.task_id = t.id
               WHERE t.id = ?""",
            (task_id,),
        ).fetchone()
        return bool(
            row is not None
            and row["status"] == "approved"
            and state is not None
            and state["approval_status"] == "approved"
            and state["status"] not in {"done", "failed", "cancelled"}
            and state["wait_token"] == wait_token
            and state["waiting_reason_code"] == "approval"
            and state["invalidated_at"] is None
        )

    def decide(
        self, task_id: int, wait_token: str, actor: str, scope: str, status: str
    ) -> bool:
        if not actor.strip() or not wait_token.strip() or not scope.strip():
            raise ValueError("actor, wait_token and scope are required")
        if status not in {"approved", "rejected", "revoked"}:
            raise ValueError("invalid approval decision")
        row = self.get(wait_token)
        checkpoint = self.connection.execute(
            """SELECT c.wait_token, c.waiting_reason_code, c.invalidated_at,
                      t.status, t.approval_status
               FROM task_checkpoints c JOIN tasks t ON t.id = c.task_id
               WHERE c.task_id = ?""",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["task_id"] != task_id
            or row["permission_scope"] != scope
            or checkpoint is None
            or checkpoint["wait_token"] != wait_token
            or checkpoint["waiting_reason_code"] != "approval"
            or checkpoint["invalidated_at"] is not None
            or checkpoint["status"] in {"done", "failed", "cancelled"}
        ):
            raise ValueError("stale or mismatched approval request")
        if status == "approved" and (
            checkpoint["status"] != "waiting"
            or checkpoint["approval_status"] != "approved"
        ):
            raise ValueError("task is not eligible for approval")
        if row["status"] == status:
            return False
        expected = "approved" if status == "revoked" else "pending"
        if row["status"] != expected:
            raise ValueError("approval decision is no longer available")
        cursor = self.connection.execute(
            """UPDATE task_step_approvals SET status = ?, decided_by = ?,
                      decided_at = CURRENT_TIMESTAMP
               WHERE wait_token = ? AND status = ?""",
            (status, actor, wait_token, expected),
        )
        if cursor.rowcount != 1:
            raise ValueError("approval decision lost a race")
        return True
