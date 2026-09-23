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
        next_step_id: str | None = None,
        completed_step_ids: list[str] | None = None,
        plan_fingerprint: str | None = None,
        execution_result: Any | None = None,
        validation_result: Any | None = None,
    ) -> int:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        extended = {"next_step_id", "completed_step_ids", "plan_fingerprint"}
        if not extended <= columns:
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
        extra = {
            "execution_result",
            "validation_result",
            "resume_count",
            "invalidated_at",
        }
        if not extra <= columns:
            cursor = self.connection.execute(
                """INSERT INTO task_checkpoints
                (task_id, phase, next_action, plan_version, attempt_id, reason, context_data,
                 next_step_id, completed_step_ids, plan_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET phase=excluded.phase,
                next_action=excluded.next_action, plan_version=excluded.plan_version,
                attempt_id=excluded.attempt_id, reason=excluded.reason,
                context_data=excluded.context_data, next_step_id=excluded.next_step_id,
                completed_step_ids=excluded.completed_step_ids,
                plan_fingerprint=excluded.plan_fingerprint, created_at=CURRENT_TIMESTAMP,
                resumed_at=NULL""",
                (
                    task_id,
                    phase,
                    next_action,
                    plan_version,
                    attempt_id,
                    reason,
                    json.dumps(context_data) if context_data is not None else None,
                    next_step_id,
                    json.dumps(completed_step_ids or []),
                    plan_fingerprint,
                ),
            )
            return cursor.lastrowid
        cursor = self.connection.execute(
            """INSERT INTO task_checkpoints
            (task_id, phase, next_action, plan_version, attempt_id, reason, context_data,
             next_step_id, completed_step_ids, plan_fingerprint, execution_result,
             validation_result, resume_count, invalidated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET phase=excluded.phase,
            next_action=excluded.next_action, plan_version=excluded.plan_version,
            attempt_id=excluded.attempt_id, reason=excluded.reason,
            context_data=excluded.context_data, next_step_id=excluded.next_step_id,
            completed_step_ids=excluded.completed_step_ids,
            plan_fingerprint=excluded.plan_fingerprint, created_at=CURRENT_TIMESTAMP,
            resumed_at=NULL, execution_result=excluded.execution_result,
            validation_result=excluded.validation_result, resume_count=0,
            invalidated_at=NULL""",
            (
                task_id,
                phase,
                next_action,
                plan_version,
                attempt_id,
                reason,
                json.dumps(context_data) if context_data is not None else None,
                next_step_id,
                json.dumps(completed_step_ids or []),
                plan_fingerprint,
                json.dumps(execution_result) if execution_result is not None else None,
                json.dumps(validation_result)
                if validation_result is not None
                else None,
                0,
                None,
            ),
        )
        return cursor.lastrowid

    def get(self, task_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()

    def mark_resumed(self, task_id: int) -> bool:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        if "resume_count" in columns:
            cursor = self.connection.execute(
                "UPDATE task_checkpoints SET resumed_at = CURRENT_TIMESTAMP, resume_count = resume_count + 1 WHERE task_id = ? AND resumed_at IS NULL",
                (task_id,),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE task_checkpoints SET resumed_at = CURRENT_TIMESTAMP WHERE task_id = ? AND resumed_at IS NULL",
                (task_id,),
            )
        return cursor.rowcount == 1

    def invalidate(self, task_id: int, reason: str | None = None) -> bool:
        """Invalidate a checkpoint so it cannot be resumed after cancellation."""
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        if "invalidated_at" in columns:
            cursor = self.connection.execute(
                """UPDATE task_checkpoints
                   SET invalidated_at = CURRENT_TIMESTAMP,
                       reason = COALESCE(?, reason)
                   WHERE task_id = ? AND invalidated_at IS NULL""",
                (reason, task_id),
            )
        else:
            cursor = self.connection.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
            )
        return cursor.rowcount == 1
