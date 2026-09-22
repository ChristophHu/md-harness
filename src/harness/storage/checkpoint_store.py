"""Persistence for resumable task checkpoints."""

from __future__ import annotations

import json
from sqlite3 import Connection, Row
from typing import Any


class CheckpointStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def save(
        self,
        task_id: int,
        phase: str,
        next_action: str,
        *,
        plan_version: int | None = None,
        attempt_id: int | None = None,
        reason: str | None = None,
        context_data: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO task_checkpoints
            (task_id, phase, next_action, plan_version, attempt_id, reason, context_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET phase=excluded.phase,
            next_action=excluded.next_action, plan_version=excluded.plan_version,
            attempt_id=excluded.attempt_id, reason=excluded.reason,
            context_data=excluded.context_data, created_at=CURRENT_TIMESTAMP,
            resumed_at=NULL""",
            (
                task_id,
                phase,
                next_action,
                plan_version,
                attempt_id,
                reason,
                json.dumps(context_data) if context_data is not None else None,
            ),
        )
        return cursor.lastrowid

    def get(self, task_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()

    def mark_resumed(self, task_id: int) -> None:
        self.connection.execute(
            "UPDATE task_checkpoints SET resumed_at = CURRENT_TIMESTAMP WHERE task_id = ?",
            (task_id,),
        )
