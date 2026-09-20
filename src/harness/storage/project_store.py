"""Project persistence operations."""
from sqlite3 import Connection, Row

class ProjectStore:
    def __init__(self, connection: Connection): self.connection = connection
    def create(self, name: str, path: str) -> int:
        return self.connection.execute("INSERT INTO projects (name, path) VALUES (?, ?)", (name, path)).lastrowid
    def get(self, project_id: int) -> Row | None:
        return self.connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    def list(self) -> list[Row]:
        return self.connection.execute("SELECT * FROM projects ORDER BY id").fetchall()

