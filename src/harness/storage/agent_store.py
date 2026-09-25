"""Agent and task-assignment persistence."""

from __future__ import annotations

from sqlite3 import Connection, Row

from harness.agents.registry import AgentProfile, AgentProfileError


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
        if self.binding(task_id) is not None:
            raise AgentProfileError("cannot reassign a pinned agent task")
        return self.connection.execute(
            "INSERT INTO task_assignments (task_id, agent_id, assignment_type) VALUES (?, ?, ?)",
            (task_id, agent_id, assignment_type),
        ).lastrowid

    def assignments(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_assignments WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def assigned_name(self, task_id: int) -> str | None:
        """An active explicit assignment overrides the legacy task name."""
        row = self.connection.execute(
            """SELECT agents.name, agents.enabled FROM task_assignments
               JOIN agents ON agents.id = task_assignments.agent_id
               WHERE task_assignments.task_id = ?
               AND task_assignments.status = 'active'
               AND task_assignments.assignment_type = 'execution'
               ORDER BY task_assignments.id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is not None and not row["enabled"]:
            raise AgentProfileError(f"assigned agent is disabled: {row['name']}")
        return row["name"] if row else None

    def binding(self, task_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_task_bindings WHERE task_id = ?", (task_id,)
        ).fetchone()

    def bind(self, task_id: int, profile: AgentProfile) -> Row:
        """Pin the selected profile; a later reconfiguration cannot replace it."""
        self.connection.execute(
            """INSERT OR IGNORE INTO agent_task_bindings
               (task_id, profile_name, profile_version, profile_fingerprint)
               VALUES (?, ?, ?, ?)""",
            (task_id, profile.name, profile.version, profile.fingerprint),
        )
        binding = self.binding(task_id)
        assert binding is not None
        return binding
