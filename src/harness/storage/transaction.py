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
