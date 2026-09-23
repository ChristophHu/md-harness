"""Transaction coordination for the SQLite-backed engine stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class TransactionError(RuntimeError):
    """Raised when stores cannot participate in one transaction."""


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
            resumed = self.checkpoint_store.mark_resumed(task_id)
            if resumed:
                self.event_store.record(
                    task_id, "task.resumed", {"next_action": next_action}
                )
            return resumed

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
