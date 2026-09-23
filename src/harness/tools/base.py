"""Common tool contracts, metadata and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from harness.engine.result import ExecutionErrorType


class ToolError(RuntimeError):
    """Base error for tool registration and execution."""

    def __init__(
        self,
        message: str,
        error_type: ExecutionErrorType = ExecutionErrorType.TOOL_FAILURE,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Hard execution limits shared by tools."""

    max_output_bytes: int = 20_000
    max_changed_files: int = 100

    def __post_init__(self) -> None:
        if self.max_output_bytes <= 0 or self.max_changed_files < 1:
            raise ValueError("resource limits must be positive")


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
    limits: ResourceLimits = field(default_factory=ResourceLimits)


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
                f"unknown arguments for {self.tool_name}: {sorted(unknown)}",
                ExecutionErrorType.INVALID_ARGUMENTS,
            )
        missing = {
            parameter.name
            for parameter in self.definition.parameters
            if parameter.required and parameter.name not in arguments
        }
        if missing:
            raise ToolError(
                f"missing arguments for {self.tool_name}: {sorted(missing)}",
                ExecutionErrorType.INVALID_ARGUMENTS,
            )
        for parameter in self.definition.parameters:
            if parameter.name not in arguments:
                continue
            value = arguments[parameter.name]
            if parameter.type == "list" and not isinstance(value, list):
                raise ToolError(
                    f"{parameter.name} must be a list",
                    ExecutionErrorType.INVALID_ARGUMENTS,
                )
            if parameter.type == "string" and not isinstance(value, str):
                raise ToolError(
                    f"{parameter.name} must be a string",
                    ExecutionErrorType.INVALID_ARGUMENTS,
                )
            if parameter.type == "boolean" and not isinstance(value, bool):
                raise ToolError(
                    f"{parameter.name} must be a boolean",
                    ExecutionErrorType.INVALID_ARGUMENTS,
                )
            if parameter.type == "number" and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise ToolError(
                    f"{parameter.name} must be a number",
                    ExecutionErrorType.INVALID_ARGUMENTS,
                )
            if parameter.type in {"path", "list[path]"}:
                if parameter.type == "list[path]" and not isinstance(value, list):
                    raise ToolError(
                        f"{parameter.name} must be a list",
                        ExecutionErrorType.INVALID_ARGUMENTS,
                    )
                paths = value if parameter.type == "list[path]" else [value]
                for path in paths:
                    from pathlib import Path

                    candidate = Path(path)
                    if candidate.is_absolute() or ".." in candidate.parts:
                        raise ToolError(
                            f"{parameter.name} is outside the workspace",
                            ExecutionErrorType.WORKSPACE_ERROR,
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
            raise ToolError(
                f"unknown tool: {name}", ExecutionErrorType.TOOL_NOT_FOUND
            ) from error

    def list(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def execute(self, name: str, **arguments: Any) -> Any:
        tool = self.get(name)
        tool.validate_arguments(arguments)
        return tool.execute(**arguments)
