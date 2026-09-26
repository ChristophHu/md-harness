"""Task event persistence."""

import json
from sqlite3 import Connection, Row


class EventStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def record(self, task_id: int, event_type: str, payload: object = None) -> int:
        value = (
            json.dumps(payload)
            if payload is not None and not isinstance(payload, str)
            else payload
        )
        return self.connection.execute(
            "INSERT INTO task_events (task_id, event_type, payload) VALUES (?, ?, ?)",
            (task_id, event_type, value),
        ).lastrowid

    def list_for_task(self, task_id: int) -> list[Row]:
        return self.connection.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
