"""Persistence helpers for structured human-in-the-loop requests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from sqlite3 import Connection, Row
from typing import Any


class HumanInteractionStore:
    """Store interaction records; transaction boundaries belong to the UOW."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def create(
        self,
        task_id: int,
        wait_token: str,
        request: Mapping[str, Any],
        *,
        plan_version: int | None = None,
        plan_fingerprint: str | None = None,
    ) -> str:
        interaction_id = str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO task_human_interactions
               (interaction_id, task_id, wait_token, kind, prompt, response_schema,
                request_data, resume_action, plan_version, plan_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                interaction_id,
                task_id,
                wait_token,
                request["kind"],
                request["prompt"],
                json.dumps(request["response_schema"], sort_keys=True),
                json.dumps(request.get("request_data", {}), sort_keys=True),
                request["resume_action"],
                plan_version,
                plan_fingerprint,
            ),
        )
        return interaction_id

    def get(self, interaction_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM task_human_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()

    def for_wait(self, task_id: int, wait_token: str) -> Row | None:
        return self.connection.execute(
            """SELECT * FROM task_human_interactions
               WHERE task_id = ? AND wait_token = ?""",
            (task_id, wait_token),
        ).fetchone()

    def list_open(self, task_id: int) -> list[Row]:
        return list(
            self.connection.execute(
                """SELECT i.* FROM task_human_interactions i
                   JOIN task_checkpoints c ON c.task_id = i.task_id
                   WHERE i.task_id = ? AND i.status = 'pending'
                   AND c.wait_token = i.wait_token AND c.invalidated_at IS NULL
                   ORDER BY i.created_at, i.interaction_id""",
                (task_id,),
            ).fetchall()
        )

    @staticmethod
    def validate_schema(schema: Mapping[str, Any]) -> None:
        """Reject malformed response schemas before persisting requests."""
        supported = {
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        }
        expected_type = schema.get("type")
        if expected_type not in supported:
            raise ValueError("response schema declares an unsupported type")
        if "enum" in schema and not isinstance(schema["enum"], list):
            raise ValueError("response schema enum must be a list")
        if expected_type == "object":
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if not isinstance(properties, Mapping) or not isinstance(required, list):
                raise ValueError(
                    "object response schema properties/required are invalid"
                )
            if any(not isinstance(key, str) for key in required):
                raise ValueError("response schema required fields must be strings")
            for child in properties.values():
                if not isinstance(child, Mapping):
                    raise TypeError("response schema property must be an object")
                HumanInteractionStore.validate_schema(child)

    @staticmethod
    def validate_response(schema: Mapping[str, Any], response: Any) -> None:
        """Validate the useful JSON-Schema subset used by interaction contracts."""
        if not isinstance(schema, Mapping):
            raise TypeError("response_schema must be an object")
        expected_type = schema.get("type")
        checks = {
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: (
                isinstance(value, int) and not isinstance(value, bool)
            ),
            "number": lambda value: (
                isinstance(value, (int, float)) and not isinstance(value, bool)
            ),
            "boolean": lambda value: isinstance(value, bool),
            "null": lambda value: value is None,
        }
        if expected_type not in checks or not checks[expected_type](response):
            raise ValueError(f"response must have JSON type {expected_type}")
        if "enum" in schema and response not in schema["enum"]:
            raise ValueError("response is not one of the allowed values")
        if expected_type == "object":
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                raise ValueError("response schema properties must be an object")
            missing = set(schema.get("required", ())) - response.keys()
            if missing:
                raise ValueError(
                    f"response is missing required fields: {sorted(missing)}"
                )
            if schema.get("additionalProperties") is False:
                extra = response.keys() - properties.keys()
                if extra:
                    raise ValueError(f"response has unexpected fields: {sorted(extra)}")
            for key, value in response.items():
                if key in properties:
                    HumanInteractionStore.validate_response(properties[key], value)
