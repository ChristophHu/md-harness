"""Central construction of the tools available to an engine run."""

from __future__ import annotations

from pathlib import Path

from harness.tools.base import ToolRegistry
from harness.tools.filesystem import FilesystemTool
from harness.tools.git import GitRepository
from harness.tools.sqlite import SQLiteTool
from harness.tools.test_runner import TestRunner


class ToolRegistryFactory:
    """Create consistently configured registries for a workspace."""

    DEFAULT_TOOLS = frozenset({"filesystem", "git", "sqlite", "test_runner"})

    @classmethod
    def create(
        cls,
        workspace: str | Path,
        *,
        database: str | Path | None = None,
        enabled_tools: set[str] | None = None,
        allow_delete: bool = False,
        repository_name: str | None = None,
    ) -> ToolRegistry:
        """Build a registry with tools restricted to the supplied workspace."""
        root = Path(workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        selected = cls.DEFAULT_TOOLS if enabled_tools is None else set(enabled_tools)
        unknown = selected - cls.DEFAULT_TOOLS
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")

        registry = ToolRegistry()
        if "filesystem" in selected:
            registry.register(FilesystemTool(root, allow_delete=allow_delete))
        if "git" in selected:
            registry.register(GitRepository(root, repository_name or root.name))
        if "sqlite" in selected:
            database_path = (
                Path(database)
                if database is not None
                else root / "state/harness.sqlite"
            )
            registry.register(SQLiteTool(database_path))
        if "test_runner" in selected:
            registry.register(TestRunner(root))
        return registry
