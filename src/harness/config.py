"""Application configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when configuration values are invalid."""


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Limits and switches for one engine execution."""

    dry_run: bool = True
    max_retries: int = 2
    max_cycles: int = 3

    def __post_init__(self) -> None:
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
            for key in ("dry_run", "max_retries", "max_cycles")
            if key in execution
        }
    )
    data["execution"] = settings
    return data
