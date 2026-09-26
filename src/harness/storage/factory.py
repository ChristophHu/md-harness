"""Central construction of the SQLite store bundle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from harness.storage.artifact_store import ArtifactStore
from harness.storage.checkpoint_store import CheckpointStore
from harness.storage.event_store import EventStore
from harness.storage.task_store import TaskStore
from harness.storage.transaction import TransactionManager


@dataclass(frozen=True, slots=True)
class StoreBundle:
    """All persistence stores participating in one database transaction."""

    connection: sqlite3.Connection
    task_store: TaskStore
    event_store: EventStore
    artifact_store: ArtifactStore
    checkpoint_store: CheckpointStore
    transaction_manager: TransactionManager


class StoreFactory:
    """Build a complete, consistently connected persistence bundle."""

    @staticmethod
    def create(
        connection: sqlite3.Connection, workspace: str | Path | None = None
    ) -> StoreBundle:
        task_store = TaskStore(connection)
        event_store = EventStore(connection)
        artifact_store = ArtifactStore(connection, workspace)
        checkpoint_store = CheckpointStore(connection)
        manager = TransactionManager(
            connection, task_store, event_store, artifact_store, checkpoint_store
        )
        return StoreBundle(
            connection,
            task_store,
            event_store,
            artifact_store,
            checkpoint_store,
            manager,
        )
