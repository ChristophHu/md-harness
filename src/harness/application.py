"""Application composition root for configured harness runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.config import ConfigError, ExecutionConfig, PersistenceMode, load_config
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.orchestrator import Orchestrator
from harness.engine.planner import Planner
from harness.engine.validator import Validator
from harness.security.tool_policy import ToolSecurityPolicy
from harness.storage.database import connect, initialize_database
from harness.storage.factory import StoreFactory
from harness.storage.project_store import ProjectStore
from harness.tools.factory import ToolRegistryFactory


def build_orchestrator(config_path: str | Path) -> tuple[Orchestrator, Any]:
    """Build the configured engine and return it with its live connection."""
    path = Path(config_path).expanduser().resolve()
    raw = load_config(path)
    execution: ExecutionConfig = raw["execution"]
    project = raw.get("project", {})
    storage = raw.get("storage", {})
    workspace = Path(project.get("workspace_dir", ".")).expanduser()
    if not workspace.is_absolute():
        workspace = path.parent / workspace
    workspace = workspace.resolve()
    database = Path(storage.get("database", "state/harness.sqlite")).expanduser()
    if not database.is_absolute():
        database = (path.parent / database).resolve()

    if execution.persistence_mode is PersistenceMode.DISABLED:
        raise ConfigError(
            "disabled persistence is not supported by the CLI composition root"
        )

    database_path = initialize_database(database)
    connection = connect(database_path)
    stores = StoreFactory.create(connection)
    project_store = ProjectStore(connection)
    registry = ToolRegistryFactory.create(
        workspace,
        database=database,
        repository_name=project.get("name"),
    )
    context_builder = ContextBuilder.from_stores(
        stores,
        project_store,
        tool_registry=registry,
        dry_run=execution.dry_run,
    )
    policy = ToolSecurityPolicy(
        allow_destructive=False,
        require_approval=True,
    )
    orchestrator = Orchestrator(
        context_builder,
        planner=Planner(),
        executor=Executor(registry, policy),
        validator=Validator(),
        stores=stores,
        execution_config=execution,
    )
    return orchestrator, connection
