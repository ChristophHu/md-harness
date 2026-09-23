"""Transaction coordination for the SQLite-backed engine stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class TransactionError(RuntimeError):
    """Raised when stores cannot participate in one transaction."""


@dataclass(frozen=True, slots=True)
class ValidationWrite:
    """Complete, precomputed persistence decision for one validator result."""

    task_id: int
    attempt_id: int | None
    validation: Any
    outcome: str
    message: str
    checkpoint: dict[str, Any] | None = None
    event_type: str | None = None
    event_payload: Any = None
    replan: tuple[int, int] | None = None


class TransactionManager:
    """Coordinate atomic persistence blocks on one SQLite connection."""

    def __init__(self, connection: sqlite3.Connection, *stores: Any) -> None:
        self.connection = connection
        self._validate_connections(stores)

    def _validate_connections(self, stores: tuple[Any, ...]) -> None:
        for store in stores:
            if store is None:
                continue
            store_connection = getattr(store, "connection", None)
            if store_connection is not self.connection:
                raise TransactionError(
                    "all stores must share the transaction connection"
                )

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Commit the block or roll it back when an exception escapes."""
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def record_failure(
        self, task_store: Any, event_store: Any, task_id: int, message: str
    ) -> None:
        """Persist a failure in a fresh transaction after a failed block."""
        self._validate_connections((task_store, event_store))
        try:
            with self.atomic():
                task_store.transition(task_id, "failed")
                event_store.record(
                    task_id, "task.persistence.failed", {"error": message}
                )
        except Exception as error:
            raise TransactionError("could not persist transaction failure") from error


class EngineUnitOfWork:
    """Group one engine phase into a single persistence transaction."""

    def __init__(
        self,
        manager: TransactionManager,
        task_store: Any,
        event_store: Any,
        artifact_store: Any,
        checkpoint_store: Any | None = None,
    ) -> None:
        self.manager = manager
        self.task_store = task_store
        self.event_store = event_store
        self.artifact_store = artifact_store
        self.checkpoint_store = checkpoint_store
        manager._validate_connections(
            (task_store, event_store, artifact_store, checkpoint_store)
        )

    def resume(self, task_id: int, next_action: str) -> bool:
        if self.checkpoint_store is None:
            return False
        with self.phase():
            claim = getattr(self.checkpoint_store, "claim_resume", None)
            resumed = (
                claim(task_id)
                if claim is not None
                else self.checkpoint_store.mark_resumed(task_id)
            )
            if resumed:
                self.event_store.record(
                    task_id, "task.resumed", {"next_action": next_action}
                )
            return resumed

    def claim_resume_lease(
        self, task_id: int, token: str, lease_seconds: int = 300
    ) -> bool:
        """Acquire a resumable lease and record the resume event atomically."""
        if self.checkpoint_store is None:
            return False
        with self.phase():
            get_task = getattr(self.task_store, "get", None)
            task = get_task(task_id) if get_task is not None else None
            if task is not None and task["status"] in {"done", "failed", "cancelled"}:
                return False
            claim = getattr(self.checkpoint_store, "claim_resume_lease", None)
            if claim is None:
                legacy_claim = getattr(self.checkpoint_store, "claim_resume", None)
                claimed = (
                    legacy_claim(task_id)
                    if legacy_claim is not None
                    else self.checkpoint_store.mark_resumed(task_id)
                )
                if claimed:
                    self.event_store.record(
                        task_id, "task.resumed", {"claim_token": token}
                    )
                return claimed
            claimed = claim(task_id, token=token, lease_seconds=lease_seconds)
            if claimed is None:
                return False
            self.event_store.record(task_id, "task.resumed", {"claim_token": token})
            return True

    def release_resume_lease(self, task_id: int, token: str) -> bool:
        """Release a lease after the resumed workflow returns."""
        if self.checkpoint_store is None:
            return False
        with self.phase():
            return self.checkpoint_store.release_resume_lease(task_id, token)

    def resolve_external_wait(
        self, task_id: int, wait_token: str, actor: str, information_ref: str
    ) -> bool:
        """Resolve one external wait and write its audit event in one transaction.

        Returns False for an idempotent repeat with the same reference.
        """
        if not actor.strip() or not information_ref.strip() or not wait_token.strip():
            raise ValueError("wait_token, actor and information_ref must be non-empty")
        if self.checkpoint_store is None:
            raise TransactionError("checkpoint store is required")
        with self.phase():
            if self.checkpoint_store.resolve_external_wait(
                task_id, wait_token, actor, information_ref
            ):
                self.event_store.record(
                    task_id,
                    "task.external_wait.resolved",
                    {
                        "wait_token": wait_token,
                        "actor": actor,
                        "information_ref": information_ref,
                    },
                )
                return True
            task = self.task_store.get(task_id)
            checkpoint = self.checkpoint_store.get_active(task_id)
            if task is None or task["status"] != "waiting" or checkpoint is None:
                raise ValueError("task has no active waiting checkpoint")
            if checkpoint["wait_token"] != wait_token:
                raise ValueError("stale wait token")
            if checkpoint["waiting_reason_code"] != "external_information":
                raise ValueError("checkpoint is not waiting for external information")
            if checkpoint["external_resolved_at"] is not None:
                if checkpoint["information_ref"] == information_ref:
                    return False
                raise ValueError("external wait was resolved with another reference")
            raise ValueError("external wait could not be resolved")

    def execution_wait(
        self,
        task_id: int,
        attempt_id: int,
        checkpoint: dict[str, Any],
        execution: Any,
        next_action: str,
    ) -> None:
        """Persist an execution wait checkpoint and waiting decision atomically."""
        with self.phase():
            self.checkpoint_store.save(task_id, **checkpoint)
            self.event_store.record(
                task_id,
                "task.execution.next_action",
                {"next_action": next_action},
            )
            self.task_store.complete_attempt(
                attempt_id, "waiting", checkpoint["reason"]
            )
            self.task_store.transition(task_id, "waiting")
            self.event_store.record(
                task_id, "task.waiting", {"reason": checkpoint["reason"]}
            )

    @contextmanager
    def phase(self) -> Iterator[sqlite3.Connection]:
        """Commit all writes in the phase or roll them back together."""
        with self.manager.atomic() as connection:
            yield connection

    def cycle_start(self, task_id: int, status: str, event_type: str) -> None:
        with self.phase():
            self.task_store.transition(task_id, status)
            self.task_store.record_attempt(task_id, "running")
            self.event_store.record(task_id, event_type)

    def start_cycle(self, task_id: int, status: str, event_type: str) -> int:
        """Atomically create the attempt and persist the cycle start."""
        with self.phase():
            self.task_store.transition(task_id, status)
            attempt_id = self.task_store.record_attempt(task_id, "running")
            self.event_store.record(task_id, event_type)
            return attempt_id

    def execution_result(self, task_id: int, attempt_id: int, result: Any) -> None:
        with self.phase():
            self.event_store.record(task_id, "task.execution.completed", result)
            for path in sorted(set(result.artifacts)):
                self.artifact_store.register(task_id, path, "execution-artifact")
            self.task_store.complete_attempt(attempt_id, "completed")

    def decision(
        self, task_id: int, status: str, event_type: str, payload: Any = None
    ) -> None:
        with self.phase():
            self.task_store.transition(task_id, status)
            self.event_store.record(task_id, event_type, payload)

    def finish_decision(
        self,
        task_id: int,
        attempt_id: int,
        attempt_status: str,
        status: str,
        event_type: str,
        payload: Any = None,
    ) -> None:
        """Atomically finish an attempt, transition the task and record an event."""
        with self.phase():
            self.task_store.complete_attempt(attempt_id, attempt_status)
            self.task_store.transition(task_id, status)
            self.event_store.record(task_id, event_type, payload)

    def complete_done(self, task_id: int, attempt_id: int | None) -> None:
        """Complete a successful task and retire its checkpoint atomically."""
        with self.phase():
            if attempt_id is not None:
                self.task_store.complete_attempt(attempt_id, "completed")
            self.task_store.transition(task_id, "done")
            self.event_store.record(task_id, "task.done")
            if self.checkpoint_store is not None:
                self.checkpoint_store.invalidate(task_id)

    def validation_decision(self, write: ValidationWrite) -> None:
        """Commit the validator result and its complete workflow decision."""
        if write.checkpoint is not None and self.checkpoint_store is None:
            raise TransactionError(
                "resumable validation decision requires checkpoint store"
            )
        with self.phase() as connection:
            task = self.task_store.get(write.task_id)
            if task is None or task["status"] != "validating":
                raise TransactionError("validation task is not validating")
            if write.attempt_id is not None:
                attempt = connection.execute(
                    "SELECT task_id, status FROM task_attempts WHERE id = ?",
                    (write.attempt_id,),
                ).fetchone()
                if (
                    attempt is None
                    or attempt["task_id"] != write.task_id
                    or attempt["status"] != "running"
                ):
                    raise TransactionError("validation attempt is not active")
            self.apply_validation_decision(
                self.task_store, self.event_store, self.checkpoint_store, write
            )

    @staticmethod
    def apply_validation_decision(
        task_store: Any, event_store: Any, checkpoint_store: Any, write: ValidationWrite
    ) -> None:
        """Write one decision; the caller supplies the transaction boundary."""
        task_id = write.task_id
        record = getattr(event_store, "record", lambda *_args: None)
        transition = getattr(task_store, "transition", lambda *_args: None)
        complete_attempt = getattr(task_store, "complete_attempt", lambda *_args: None)
        record(task_id, "task.validation.completed", write.validation)
        if write.checkpoint is not None and checkpoint_store is not None:
            checkpoint_store.save(task_id, **write.checkpoint)
        if write.attempt_id is not None:
            attempt_status = (
                "completed"
                if write.outcome == "done"
                else "waiting"
                if write.outcome == "wait"
                else "failed"
            )
            complete_attempt(
                write.attempt_id,
                attempt_status,
                write.message if attempt_status != "completed" else None,
            )
        if write.outcome == "done":
            transition(task_id, "done")
            record(task_id, "task.done")
        elif write.outcome == "wait":
            transition(task_id, "waiting")
            record(task_id, "task.waiting", {"reason": write.message})
        else:
            record(
                task_id,
                "task.validation.failed",
                {"error": write.message},
            )
            if write.outcome == "failed":
                transition(task_id, "failed")
                if write.event_type and write.event_type != "task.validation.failed":
                    record(task_id, write.event_type, write.event_payload)
                record(task_id, "task.failed", {"error": write.message})
            else:
                if write.replan is not None:
                    parent_version, version = write.replan
                    record_replan = getattr(task_store, "record_replan", None)
                    if record_replan is not None:
                        record_replan(
                            task_id,
                            "validation_failed",
                            version,
                            parent_plan_version=parent_version,
                            attempt_id=write.attempt_id,
                        )
                record(task_id, write.event_type, write.event_payload)
        if write.outcome in {"done", "failed"} and hasattr(
            checkpoint_store, "invalidate"
        ):
            checkpoint_store.invalidate(task_id)

    def cancel(self, task_id: int, reason: str) -> None:
        """Atomically cancel the task and clean up all resumable state."""
        with self.phase():
            self.task_store.cancel(task_id, reason)
            if self.checkpoint_store is not None:
                self.checkpoint_store.invalidate(task_id, reason)
            self.event_store.record(task_id, "task.cancelled", {"reason": reason})
