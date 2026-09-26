"""Task artifact persistence with normalized, idempotent registrations."""

import hashlib
from pathlib import Path
from sqlite3 import Connection, Row


class ArtifactStore:
    def __init__(self, connection: Connection, workspace: str | Path | None = None):
        self.connection = connection
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None

    def register(
        self,
        task_id: int,
        path: str,
        artifact_type: str | None = None,
        checksum: str | None = None,
        size_bytes: int | None = None,
    ) -> int:
        normalized = self._normalize(path)
        if checksum is None and self.workspace is not None:
            candidate = self.workspace / normalized
            if candidate.is_file():
                checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
                size_bytes = candidate.stat().st_size
        if size_bytes is not None and size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        row = self.connection.execute(
            """INSERT INTO task_artifacts
            (task_id, path, artifact_type, checksum, size_bytes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, path, checksum) DO UPDATE SET
                artifact_type = COALESCE(excluded.artifact_type, artifact_type),
                size_bytes = COALESCE(excluded.size_bytes, size_bytes)
            RETURNING id""",
            (task_id, normalized, artifact_type, checksum, size_bytes),
        ).fetchone()
        return int(row[0])

    def _normalize(self, path: str) -> str:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must be relative to the workspace")
        normalized = candidate.as_posix()
        if not normalized or normalized == ".":
            raise ValueError("artifact path must not be empty")
        return normalized

    def list_for_task(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
