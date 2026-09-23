"""Tests for execution configuration."""

import pytest

from harness.config import (
    ConfigError,
    ExecutionConfig,
    PersistenceMode,
    SecretConfig,
    load_config,
)


def test_execution_config_defaults_and_load(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "project: {}\nexecution:\n  dry_run: false\n  max_retries: 4\n  max_cycles: 6\n  persistence_mode: required\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config["execution"] == ExecutionConfig(False, 4, 6, PersistenceMode.REQUIRED)


def test_execution_config_rejects_invalid_limits():
    with pytest.raises(ConfigError, match="max_retries"):
        ExecutionConfig(max_retries=-1)
    with pytest.raises(ConfigError, match="max_cycles"):
        ExecutionConfig(max_cycles=0)
    with pytest.raises(ConfigError, match="max_replans"):
        ExecutionConfig(max_replans=-1)


def test_execution_config_rejects_invalid_persistence_mode():
    with pytest.raises(ConfigError, match="persistence_mode"):
        ExecutionConfig(persistence_mode="invalid")


@pytest.mark.parametrize(
    "field", ["dry_run", "max_retries", "max_cycles", "max_replans"]
)
def test_execution_config_rejects_wrong_scalar_types(field):
    values = {"dry_run": True, "max_retries": 1, "max_cycles": 1, "max_replans": 1}
    values[field] = "invalid"
    with pytest.raises(ConfigError, match=field):
        ExecutionConfig(**values)


def test_load_config_handles_defaults_and_invalid_shapes(tmp_path):
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text("project: {}\n", encoding="utf-8")
    assert load_config(defaults)["execution"] == ExecutionConfig()

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("execution: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(invalid)

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("- item\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(scalar)


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="could not load"):
        load_config(tmp_path / "missing.yaml")


def test_secret_config_loads_keychain_mapping(tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text(
        "secrets:\n  provider: keychain_then_environment\n"
        "  service_prefix: local-harness\n  account: alice\n"
        "  names:\n    github_token: GITHUB_TOKEN\n",
        encoding="utf-8",
    )
    assert load_config(path)["secrets"] == SecretConfig(
        service_prefix="local-harness",
        account="alice",
        names={"github_token": "GITHUB_TOKEN"},
    )


@pytest.mark.parametrize(
    "contents, message",
    [
        ("secrets: []", "secrets configuration"),
        ("secrets:\n  provider: unknown", "secrets.provider"),
        ("secrets:\n  service_prefix: ' '", "service_prefix"),
        ("secrets:\n  account: ' '", "account"),
        ("secrets:\n  names: []", "secrets.names"),
        ("secrets:\n  names:\n    logical: 12", "secrets.names"),
        ("secrets:\n  names:\n    ' ': ENV", "names keys and values"),
    ],
)
def test_load_config_rejects_invalid_secret_settings(tmp_path, contents, message):
    path = tmp_path / "invalid-secrets.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_secret_config_defaults():
    assert SecretConfig().provider == "keychain_then_environment"
    assert SecretConfig().service_prefix == "dev-harness"
    assert SecretConfig().account is None
    assert SecretConfig().names is None
