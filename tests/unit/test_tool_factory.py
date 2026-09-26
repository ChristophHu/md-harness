"""Tests for the central tool registry factory."""

import pytest

from harness.tools.factory import ToolRegistryFactory


def test_factory_registers_default_tools_and_scopes_paths(tmp_path):
    registry = ToolRegistryFactory.create(tmp_path)

    assert [definition.name for definition in registry.list()] == [
        "filesystem",
        "git",
        "sqlite",
        "test_runner",
    ]
    assert registry.get("filesystem").root == tmp_path.resolve()
    assert registry.get("git").path == tmp_path.resolve()
    assert registry.get("sqlite").path == (tmp_path / "state/harness.sqlite").resolve()


def test_factory_supports_enabled_subset_and_delete_policy(tmp_path):
    registry = ToolRegistryFactory.create(
        tmp_path,
        enabled_tools={"filesystem"},
        allow_delete=True,
    )

    assert [definition.name for definition in registry.list()] == ["filesystem"]
    assert registry.get("filesystem").allow_delete is True


def test_factory_accepts_custom_database_and_repository_name(tmp_path):
    database = tmp_path / "custom.sqlite"
    registry = ToolRegistryFactory.create(
        tmp_path,
        database=database,
        enabled_tools={"git", "sqlite"},
        repository_name="custom-project",
    )

    assert registry.get("git").name == "custom-project"
    assert registry.get("sqlite").path == database.resolve()


def test_factory_rejects_unknown_tools(tmp_path):
    with pytest.raises(ValueError, match="unknown tools"):
        ToolRegistryFactory.create(tmp_path, enabled_tools={"unknown"})
