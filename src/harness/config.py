"""Application configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when configuration values are invalid."""


class PersistenceMode(StrEnum):
    """Controls whether engine state must be persisted."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Limits and switches for one engine execution."""

    dry_run: bool = True
    max_retries: int = 2
    max_cycles: int = 3
    persistence_mode: PersistenceMode = PersistenceMode.OPTIONAL

    def __post_init__(self) -> None:
        if not isinstance(self.dry_run, bool):
            raise ConfigError("dry_run must be a boolean")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool):
            raise ConfigError("max_retries must be an integer")
        if not isinstance(self.max_cycles, int) or isinstance(self.max_cycles, bool):
            raise ConfigError("max_cycles must be an integer")
        if not isinstance(self.persistence_mode, PersistenceMode):
            try:
                object.__setattr__(
                    self, "persistence_mode", PersistenceMode(self.persistence_mode)
                )
            except ValueError as error:
                raise ConfigError(
                    "persistence_mode must be required, optional or disabled"
                ) from error
        if self.max_retries < 0:
            raise ConfigError("max_retries must be non-negative")
        if self.max_cycles < 1:
            raise ConfigError("max_cycles must be positive")


def load_config(path: str | Path) -> dict[str, Any]:
    """Load YAML configuration and validate execution settings."""
    config_path = Path(path).expanduser()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"could not load config: {config_path}") from error
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping")
    execution = data.get("execution", {})
    if not isinstance(execution, dict):
        raise ConfigError("execution configuration must be a mapping")
    settings = ExecutionConfig(
        **{
            key: execution[key]
            for key in ("dry_run", "max_retries", "max_cycles", "persistence_mode")
            if key in execution
        }
    )
    data["execution"] = settings
    return data
