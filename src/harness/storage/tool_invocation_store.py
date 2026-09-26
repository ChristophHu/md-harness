"""Durable, plan-bound journal for potentially effectful tool calls."""

from __future__ import annotations

import json
from sqlite3 import Connection, Row
from typing import Any
from uuid import uuid4


class ToolInvocationStore:
    """One row per logical tool step; transaction boundaries belong to the UOW."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def get(self, invocation_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()

    def pending_for_task(self, task_id: int) -> str | None:
        row = self.connection.execute(
            """SELECT invocation_id FROM task_tool_invocations
               WHERE task_id = ? AND status = 'started' LIMIT 1""",
            (task_id,),
        ).fetchone()
        return row["invocation_id"] if row is not None else None

    def has_for_plan(self, task_id: int, plan_fingerprint: str) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM task_tool_invocations
               WHERE task_id = ? AND plan_fingerprint = ? LIMIT 1""",
                (task_id, plan_fingerprint),
            ).fetchone()
            is not None
        )

    def has_for_task(self, task_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM task_tool_invocations WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            is not None
        )

    def reserve(self, task_id: int, binding: dict[str, Any]) -> tuple[str, Any]:
        """Return new, completed or uncertain without replaying an unknown effect."""
        row = self.connection.execute(
            """SELECT * FROM task_tool_invocations
               WHERE task_id = ? AND plan_fingerprint = ? AND step_id = ?""",
            (task_id, binding["plan_fingerprint"], binding["step_id"]),
        ).fetchone()
        if row is not None:
            if any(
                row[field] != binding[field]
                for field in (
                    "plan_version",
                    "tool_name",
                    "arguments_fingerprint",
                    "permission_scope",
                )
            ):
                raise ValueError("tool invocation binding changed")
            if row["status"] == "completed":
                return "completed", json.loads(row["step_result"])
            if row["status"] == "started":
                return "uncertain", row["invocation_id"]
        unresolved = self.pending_for_task(task_id)
        if unresolved is not None:
            return "uncertain", unresolved
        if row is not None:
            self.connection.execute(
                """UPDATE task_tool_invocations SET status = 'started',
                   run_count = run_count + 1, step_result = NULL,
                   started_at = CURRENT_TIMESTAMP, completed_at = NULL,
                   resolved_at = NULL, resolved_by = NULL, evidence_ref = NULL
                   WHERE invocation_id = ? AND status = 'no_effect'""",
                (row["invocation_id"],),
            )
            return "new", row["invocation_id"]
        invocation_id = str(uuid4())
        self.connection.execute(
            """INSERT INTO task_tool_invocations
               (invocation_id, task_id, plan_fingerprint, plan_version, step_id,
                tool_name, arguments_fingerprint, permission_scope, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started')""",
            (
                invocation_id,
                task_id,
                binding["plan_fingerprint"],
                binding["plan_version"],
                binding["step_id"],
                binding["tool_name"],
                binding["arguments_fingerprint"],
                binding["permission_scope"],
            ),
        )
        return "new", invocation_id

    def complete(
        self, task_id: int, invocation_id: str, result: dict[str, Any]
    ) -> bool:
        cursor = self.connection.execute(
            """UPDATE task_tool_invocations SET status = 'completed',
               step_result = ?, completed_at = CURRENT_TIMESTAMP
               WHERE task_id = ? AND invocation_id = ? AND status = 'started'""",
            (json.dumps(result, sort_keys=True), task_id, invocation_id),
        )
        return cursor.rowcount == 1

    def resolve(
        self,
        task_id: int,
        invocation_id: str,
        actor: str,
        outcome: str,
        evidence_ref: str,
        result: dict[str, Any] | None,
    ) -> bool:
        cursor = self.connection.execute(
            """UPDATE task_tool_invocations SET status = ?, step_result = ?,
               completed_at = CASE WHEN ? = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END,
               resolved_at = CURRENT_TIMESTAMP, resolved_by = ?, evidence_ref = ?
               WHERE task_id = ? AND invocation_id = ? AND status = 'started'""",
            (
                outcome,
                json.dumps(result, sort_keys=True) if result is not None else None,
                outcome,
                actor,
                evidence_ref,
                task_id,
                invocation_id,
            ),
        )
        return cursor.rowcount == 1
