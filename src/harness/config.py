"""Application configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
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


class HITLMode(StrEnum):
    """Controls optional human review points, never security approvals."""

    MINIMAL = "minimal"
    SELECTIVE = "selective"
    INTERACTIVE = "interactive"


@dataclass(frozen=True, slots=True)
class SecretConfig:
    """Secret source and logical-name mapping for local/CI providers."""

    provider: str = "keychain_then_environment"
    service_prefix: str = "dev-harness"
    account: str | None = None
    names: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or (
            self.provider != "keychain_then_environment"
        ):
            raise ConfigError("secrets.provider must be keychain_then_environment")
        if not isinstance(self.service_prefix, str) or not self.service_prefix.strip():
            raise ConfigError("secrets.service_prefix must not be empty")
        if self.account is not None and (
            not isinstance(self.account, str) or not self.account.strip()
        ):
            raise ConfigError("secrets.account must not be empty")
        if self.names is not None and (
            not isinstance(self.names, Mapping)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key.strip()
                or not value.strip()
                for key, value in self.names.items()
            )
        ):
            raise ConfigError("secrets.names keys and values must be non-empty strings")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Limits and switches for one engine execution."""

    dry_run: bool = True
    max_retries: int = 2
    max_cycles: int = 3
    persistence_mode: PersistenceMode = PersistenceMode.OPTIONAL
    max_replans: int = 2
    hitl_mode: HITLMode = HITLMode.MINIMAL

    def __post_init__(self) -> None:
        if not isinstance(self.dry_run, bool):
            raise ConfigError("dry_run must be a boolean")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool):
            raise ConfigError("max_retries must be an integer")
        if not isinstance(self.max_cycles, int) or isinstance(self.max_cycles, bool):
            raise ConfigError("max_cycles must be an integer")
        if not isinstance(self.max_replans, int) or isinstance(self.max_replans, bool):
            raise ConfigError("max_replans must be an integer")
        if not isinstance(self.persistence_mode, PersistenceMode):
            try:
                object.__setattr__(
                    self, "persistence_mode", PersistenceMode(self.persistence_mode)
                )
            except ValueError as error:
                raise ConfigError(
                    "persistence_mode must be required, optional or disabled"
                ) from error
        if not isinstance(self.hitl_mode, HITLMode):
            try:
                object.__setattr__(self, "hitl_mode", HITLMode(self.hitl_mode))
            except ValueError as error:
                raise ConfigError(
                    "hitl.mode must be minimal, selective or interactive"
                ) from error
        if self.max_retries < 0:
            raise ConfigError("max_retries must be non-negative")
        if self.max_cycles < 1:
            raise ConfigError("max_cycles must be positive")
        if self.max_replans < 0:
            raise ConfigError("max_replans must be non-negative")


def load_config(path: str | Path) -> dict[str, Any]:
    """Load YAML configuration and validate execution settings."""
    config_path = Path(path).expanduser()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"could not load config: {config_path}") from error
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping")
    secrets = data.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ConfigError("secrets configuration must be a mapping")
    names = secrets.get("names", {})
    if not isinstance(names, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in names.items()
    ):
        raise ConfigError("secrets.names must map strings to strings")
    secret_settings = SecretConfig(
        provider=secrets.get("provider", "keychain_then_environment"),
        service_prefix=secrets.get("service_prefix", "dev-harness"),
        account=secrets.get("account"),
        names=names,
    )
    execution = data.get("execution", {})
    if not isinstance(execution, dict):
        raise ConfigError("execution configuration must be a mapping")
    hitl = execution.get("hitl", {})
    if not isinstance(hitl, dict):
        raise ConfigError("execution.hitl configuration must be a mapping")
    settings = ExecutionConfig(
        **{
            key: execution[key]
            for key in (
                "dry_run",
                "max_retries",
                "max_cycles",
                "max_replans",
                "persistence_mode",
            )
            if key in execution
        },
        hitl_mode=hitl.get("mode", execution.get("hitl_mode", HITLMode.MINIMAL)),
    )
    data["execution"] = settings
    data["secrets"] = secret_settings
    return data
