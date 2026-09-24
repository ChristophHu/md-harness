"""Project persistence operations."""

from __future__ import annotations

from sqlite3 import Connection, Row
from uuid import uuid4


class ProjectStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def create(
        self,
        name: str,
        path: str,
        *,
        description: str = "",
        parent_id: int | None = None,
    ) -> int:
        return self.connection.execute(
            "INSERT INTO projects (name, path, description, parent_id, project_key) VALUES (?, ?, ?, ?, ?)",
            (name, path, description, parent_id, uuid4().hex),
        ).lastrowid

    def get(self, project_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

    def list(self) -> list[Row]:
        return self.connection.execute("SELECT * FROM projects ORDER BY id").fetchall()

    def children(self, project_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM projects WHERE parent_id = ? ORDER BY id", (project_id,)
        ).fetchall()
