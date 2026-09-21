"""Agent and task-assignment persistence."""

from __future__ import annotations

from sqlite3 import Connection, Row


class AgentStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def create(
        self, name: str, *, description: str = "", capabilities: str = "[]"
    ) -> int:
        return self.connection.execute(
            "INSERT INTO agents (name, description, capabilities) VALUES (?, ?, ?)",
            (name, description, capabilities),
        ).lastrowid

    def get(self, agent_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()

    def assign(
        self, task_id: int, agent_id: int, assignment_type: str = "execution"
    ) -> int:
        return self.connection.execute(
            "INSERT INTO task_assignments (task_id, agent_id, assignment_type) VALUES (?, ?, ?)",
            (task_id, agent_id, assignment_type),
        ).lastrowid

    def assignments(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_assignments WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
