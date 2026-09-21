"""Task artifact persistence."""

from sqlite3 import Connection, Row


class ArtifactStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def register(
        self,
        task_id: int,
        path: str,
        artifact_type: str | None = None,
        checksum: str | None = None,
    ) -> int:
        return self.connection.execute(
            "INSERT INTO task_artifacts (task_id, path, artifact_type, checksum) VALUES (?, ?, ?, ?)",
            (task_id, path, artifact_type, checksum),
        ).lastrowid

    def list_for_task(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
