"""Persistence for resumable task checkpoints."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
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
        waiting_reason_code: str | None = None,
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
            "waiting_reason_code",
            "resume_claim_token",
            "resume_claim_expires_at",
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
        wait_token = str(uuid.uuid4()) if next_action == "wait" else None
        if {
            "wait_token",
            "external_resolved_at",
            "resolved_by",
            "information_ref",
        } <= columns:
            revision_assignment = (
                ", revision=task_checkpoints.revision + 1"
                if "revision" in columns
                else ""
            )
            cursor = self.connection.execute(
                """INSERT INTO task_checkpoints
                (task_id, phase, next_action, plan_version, attempt_id, reason, context_data,
                 next_step_id, completed_step_ids, plan_fingerprint, execution_result,
                 validation_result, resume_count, invalidated_at, waiting_reason_code,
                 wait_token, external_resolved_at, resolved_by, information_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(task_id) DO UPDATE SET phase=excluded.phase,
                next_action=excluded.next_action, plan_version=excluded.plan_version,
                attempt_id=excluded.attempt_id, reason=excluded.reason,
                context_data=excluded.context_data, next_step_id=excluded.next_step_id,
                completed_step_ids=excluded.completed_step_ids,
                plan_fingerprint=excluded.plan_fingerprint, created_at=CURRENT_TIMESTAMP,
                resumed_at=NULL, execution_result=excluded.execution_result,
                validation_result=excluded.validation_result, resume_count=0,
                invalidated_at=NULL, waiting_reason_code=excluded.waiting_reason_code,
                wait_token=excluded.wait_token,
                external_resolved_at=CASE WHEN excluded.next_action = 'wait' THEN NULL
                    ELSE task_checkpoints.external_resolved_at END,
                resolved_by=CASE WHEN excluded.next_action = 'wait' THEN NULL
                    ELSE task_checkpoints.resolved_by END,
                information_ref=CASE WHEN excluded.next_action = 'wait' THEN NULL
                    ELSE task_checkpoints.information_ref END"""
                + revision_assignment,
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
                    json.dumps(execution_result)
                    if execution_result is not None
                    else None,
                    json.dumps(validation_result)
                    if validation_result is not None
                    else None,
                    waiting_reason_code,
                    wait_token,
                ),
            )
            return cursor.lastrowid
        cursor = self.connection.execute(
            """INSERT INTO task_checkpoints
            (task_id, phase, next_action, plan_version, attempt_id, reason, context_data,
             next_step_id, completed_step_ids, plan_fingerprint, execution_result,
             validation_result, resume_count, invalidated_at, waiting_reason_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET phase=excluded.phase,
            next_action=excluded.next_action, plan_version=excluded.plan_version,
            attempt_id=excluded.attempt_id, reason=excluded.reason,
            context_data=excluded.context_data, next_step_id=excluded.next_step_id,
            completed_step_ids=excluded.completed_step_ids,
            plan_fingerprint=excluded.plan_fingerprint, created_at=CURRENT_TIMESTAMP,
            resumed_at=NULL, execution_result=excluded.execution_result,
            validation_result=excluded.validation_result, resume_count=0,
            invalidated_at=NULL, waiting_reason_code=excluded.waiting_reason_code""",
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
                waiting_reason_code,
            ),
        )
        return cursor.lastrowid

    def resolve_external_wait(
        self, task_id: int, wait_token: str, actor: str, information_ref: str
    ) -> bool:
        """Resolve only the current, still-open external-information wait."""
        cursor = self.connection.execute(
            """UPDATE task_checkpoints SET external_resolved_at = CURRENT_TIMESTAMP,
               resolved_by = ?, information_ref = ?
               WHERE task_id = ? AND wait_token = ? AND next_action = 'wait'
               AND waiting_reason_code = 'external_information'
               AND invalidated_at IS NULL AND external_resolved_at IS NULL
               AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status = 'waiting')""",
            (actor, information_ref, task_id, wait_token, task_id),
        )
        return cursor.rowcount == 1

    def get(self, task_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()

    def get_active(self, task_id: int) -> Row | None:
        """Return only a checkpoint that is still eligible for resume."""
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        if "invalidated_at" not in columns:
            return self.get(task_id)
        return self.connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? AND invalidated_at IS NULL",
            (task_id,),
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

    def claim_resume(self, task_id: int) -> bool:
        """Atomically claim an unconsumed checkpoint for one resumer."""
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        predicate = "task_id = ? AND resumed_at IS NULL"
        parameters: tuple[Any, ...] = (task_id,)
        if "invalidated_at" in columns:
            predicate += " AND invalidated_at IS NULL"
        if "resume_count" in columns:
            cursor = self.connection.execute(
                f"UPDATE task_checkpoints SET resumed_at = CURRENT_TIMESTAMP, resume_count = resume_count + 1 WHERE {predicate}",
                parameters,
            )
        else:
            cursor = self.connection.execute(
                f"UPDATE task_checkpoints SET resumed_at = CURRENT_TIMESTAMP WHERE {predicate}",
                parameters,
            )
        return cursor.rowcount == 1

    def claim_resume_lease(
        self,
        task_id: int,
        *,
        token: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> str | None:
        """Claim an available or expired checkpoint lease atomically."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        if not {"resume_claim_token", "resume_claim_expires_at"} <= columns:
            return None
        claim_token = token or str(uuid.uuid4())
        instant = now or datetime.now(UTC)
        expires = (instant + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        current = instant.strftime("%Y-%m-%d %H:%M:%S")
        predicate = "task_id = ? AND (resume_claim_token IS NULL OR resume_claim_expires_at < ?)"
        if "invalidated_at" in columns:
            predicate += " AND invalidated_at IS NULL"
        assignments = [
            "resume_claim_token = ?",
            "resume_claim_expires_at = ?",
            "resumed_at = COALESCE(resumed_at, CURRENT_TIMESTAMP)",
        ]
        parameters: list[Any] = [claim_token, expires]
        if "resume_count" in columns:
            assignments.append("resume_count = resume_count + 1")
        parameters.extend([task_id, current])
        cursor = self.connection.execute(
            f"UPDATE task_checkpoints SET {', '.join(assignments)} WHERE {predicate}",
            parameters,
        )
        return claim_token if cursor.rowcount == 1 else None

    def release_resume_lease(self, task_id: int, token: str) -> bool:
        cursor = self.connection.execute(
            """UPDATE task_checkpoints SET resume_claim_token = NULL,
               resume_claim_expires_at = NULL
               WHERE task_id = ? AND resume_claim_token = ?""",
            (task_id, token),
        )
        return cursor.rowcount == 1

    def ensure_resumed(self, task_id: int) -> None:
        """Retain the resume audit timestamp if a resumed run rewrote its row."""
        self.connection.execute(
            "UPDATE task_checkpoints SET resumed_at = COALESCE(resumed_at, CURRENT_TIMESTAMP) WHERE task_id = ?",
            (task_id,),
        )

    def invalidate(self, task_id: int, reason: str | None = None) -> bool:
        """Invalidate a checkpoint so it cannot be resumed after cancellation."""
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        if "invalidated_at" in columns:
            assignments = ["invalidated_at = CURRENT_TIMESTAMP"]
            if "resume_claim_token" in columns:
                assignments.append("resume_claim_token = NULL")
            if "resume_claim_expires_at" in columns:
                assignments.append("resume_claim_expires_at = NULL")
            cursor = self.connection.execute(
                f"UPDATE task_checkpoints SET {', '.join(assignments)}, reason = COALESCE(?, reason) WHERE task_id = ? AND invalidated_at IS NULL",
                (reason, task_id),
            )
        else:
            cursor = self.connection.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
            )
        return cursor.rowcount == 1
