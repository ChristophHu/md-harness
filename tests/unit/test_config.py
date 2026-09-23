"""Tests for execution configuration."""

import pytest

from harness.config import ConfigError, ExecutionConfig, PersistenceMode, load_config


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
