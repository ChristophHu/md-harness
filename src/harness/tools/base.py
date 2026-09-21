"""Common tool contracts, metadata and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolError(RuntimeError):
    """Base error for tool registration and execution."""


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    permission: PermissionLevel
    parameters: tuple[ToolParameter, ...] = ()


@dataclass(frozen=True)
class ToolContext:
    workspace: str | None = None
    dry_run: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class for tools exposed to agents and the orchestrator."""

    definition: ToolDefinition

    def __init__(self, *, context: ToolContext | None = None) -> None:
        object.__setattr__(self, "context", context or ToolContext())

    @property
    def tool_name(self) -> str:
        return self.definition.name

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Validate required and unknown arguments before execution."""
        allowed = {parameter.name for parameter in self.definition.parameters}
        unknown = set(arguments) - allowed
        if unknown:
            raise ToolError(
                f"unknown arguments for {self.tool_name}: {sorted(unknown)}"
            )
        missing = {
            parameter.name
            for parameter in self.definition.parameters
            if parameter.required and parameter.name not in arguments
        }
        if missing:
            raise ToolError(
                f"missing arguments for {self.tool_name}: {sorted(missing)}"
            )

    @abstractmethod
    def execute(self, **arguments: Any) -> Any:
        """Execute a validated tool operation."""
        raise NotImplementedError


class ToolRegistry:
    """Registry for discovering and invoking tools by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.tool_name in self._tools:
            raise ToolError(f"tool already registered: {tool.tool_name}")
        self._tools[tool.tool_name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolError(f"unknown tool: {name}") from error

    def list(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def execute(self, name: str, **arguments: Any) -> Any:
        tool = self.get(name)
        tool.validate_arguments(arguments)
        return tool.execute(**arguments)
