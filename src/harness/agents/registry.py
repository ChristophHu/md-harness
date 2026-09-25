"""Versioned, immutable task-agent profiles and deterministic selection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any


class AgentProfileError(ValueError):
    """An agent profile or assignment cannot be used safely."""


@dataclass(frozen=True, slots=True)
class AgentProfile:
    name: str
    version: int
    instructions: str
    task_types: tuple[str, ...]
    tools: tuple[str, ...]
    model: str
    max_steps: int = 8
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise AgentProfileError("agent name must not be empty")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise AgentProfileError("agent version must be positive")
        if (
            not isinstance(self.instructions, str)
            or not self.instructions.strip()
            or not isinstance(self.model, str)
            or not self.model.strip()
        ):
            raise AgentProfileError("agent instructions and model are required")
        if not self.task_types or not all(
            isinstance(v, str) and v for v in self.task_types
        ):
            raise AgentProfileError("agent task_types must be non-empty strings")
        if not self.tools or not all(isinstance(v, str) and v for v in self.tools):
            raise AgentProfileError("agent tools must be non-empty strings")
        if len(set(self.tools)) != len(self.tools):
            raise AgentProfileError("agent tools must be unique")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps < 1
        ):
            raise AgentProfileError("agent max_steps must be positive")
        if not isinstance(self.enabled, bool):
            raise AgentProfileError("agent enabled must be a boolean")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> AgentProfile:
        if not isinstance(data, dict):
            raise AgentProfileError("agent profile must be a mapping")
        if not isinstance(data.get("task_types"), list) or not isinstance(
            data.get("tools"), list
        ):
            raise AgentProfileError("agent task_types and tools must be lists")
        try:
            return cls(
                name=data["name"],
                version=data["version"],
                instructions=data["instructions"],
                task_types=tuple(data["task_types"]),
                tools=tuple(data["tools"]),
                model=data["model"],
                max_steps=data.get("max_steps", 8),
                enabled=data.get("enabled", True),
            )
        except (KeyError, TypeError) as error:
            raise AgentProfileError("invalid agent profile fields") from error

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


class AgentRegistry:
    def __init__(self, profiles: list[AgentProfile]):
        self.profiles = {profile.name: profile for profile in profiles}
        if len(self.profiles) != len(profiles):
            raise AgentProfileError("duplicate agent profile name")

    def select(self, task_type: str, assigned: str | None = None) -> AgentProfile:
        if assigned is not None:
            profile = self.profiles.get(assigned)
            if (
                profile is None
                or not profile.enabled
                or task_type not in profile.task_types
            ):
                raise AgentProfileError(f"assigned agent is unavailable: {assigned}")
            return profile
        for profile in self.profiles.values():
            if profile.enabled and task_type in profile.task_types:
                return profile
        raise AgentProfileError(f"no enabled agent for task type: {task_type}")
