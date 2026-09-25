"""Application composition root for configured harness runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from harness.agents.registry import AgentRegistry
from harness.agents.runner import AgentRunner, OpenAIResponsesModel
from harness.config import ConfigError, ExecutionConfig, PersistenceMode, load_config
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.orchestrator import Orchestrator
from harness.engine.planner import Planner
from harness.engine.validator import Validator
from harness.knowledge.decision_vault import DecisionVault
from harness.security.secrets import default_secret_provider
from harness.security.tool_policy import ToolSecurityPolicy
from harness.storage.agent_store import AgentStore
from harness.storage.database import connect, initialize_database
from harness.storage.factory import StoreFactory
from harness.storage.project_store import ProjectStore
from harness.tools.factory import ToolRegistryFactory


def build_orchestrator(config_path: str | Path) -> tuple[Orchestrator, Any]:
    """Build the configured engine and return it with its live connection."""
    path = Path(config_path).expanduser().resolve()
    raw = load_config(path)
    execution: ExecutionConfig = raw["execution"]
    secret_settings = raw["secrets"]
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
    stores = StoreFactory.create(connection, workspace)
    project_store = ProjectStore(connection)
    env_file = (
        path.parent.parent / ".env"
        if path.parent.name == "config"
        else path.parent / ".env"
    )
    env_values = dotenv_values(env_file)
    vault_setting = os.environ.get("OBSIDIAN_VAULT_PATH") or env_values.get(
        "OBSIDIAN_VAULT_PATH"
    )
    if not vault_setting:
        sources = raw.get("knowledge", {}).get("sources", [])
        vault_setting = next(
            (
                source.get("path")
                for source in sources
                if source.get("name") == "obsidian-vault"
                and source.get("enabled", True)
            ),
            None,
        )
    decision_vault = DecisionVault(connection, vault_setting) if vault_setting else None
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
        hitl_mode=execution.hitl_mode.value,
        knowledge_loader=decision_vault.for_task if decision_vault else None,
    )
    policy = ToolSecurityPolicy(
        allow_destructive=False,
        require_approval=True,
    )
    secret_provider = default_secret_provider(
        service_prefix=secret_settings.service_prefix,
        account=secret_settings.account,
        names=secret_settings.names,
    )
    agent_settings = raw["agents"]
    agent_store = AgentStore(connection)
    agent_runner = None
    if agent_settings["enabled"]:
        agent_runner = AgentRunner(
            AgentRegistry(agent_settings["profiles"]),
            agent_store,
            registry,
            OpenAIResponsesModel(secret_provider),
        )
    orchestrator = Orchestrator(
        context_builder,
        planner=agent_runner or Planner(),
        executor=Executor(registry, policy),
        validator=Validator(),
        stores=stores,
        execution_config=execution,
        secret_provider=secret_provider,
        agent_runner=agent_runner,
        agent_store=agent_store,
    )
    orchestrator.decision_vault = decision_vault
    if decision_vault is not None:
        decision_vault.publish_pending()
    return orchestrator, connection


@dataclass(frozen=True, slots=True)
class Application:
    """Fully wired application and its owned database connection."""

    orchestrator: Orchestrator
    connection: Any

    def list_human_interactions(self, task_id: int) -> list[dict[str, Any]]:
        """List pending human requests through the application API."""
        return self.orchestrator.list_human_interactions(task_id)

    def answer_human_interaction(
        self, task_id: int, interaction_id: str, actor: str, response: Any
    ) -> bool:
        """Submit a typed interaction response through the application API."""
        return self.orchestrator.answer_human_interaction(
            task_id, interaction_id, actor, response
        )

    def suggest_human_interaction(
        self, task_id: int, question: str
    ) -> list[dict[str, Any]]:
        return self.orchestrator.suggest_human_interaction(task_id, question)

    def publish_vault_decisions(self, *, limit: int = 100) -> int:
        return self.orchestrator.publish_vault_decisions(limit=limit)

    def resolve_tool_invocation(
        self,
        task_id: int,
        wait_token: str,
        invocation_id: str,
        actor: str,
        outcome: str,
        evidence_ref: str,
    ) -> bool:
        return self.orchestrator.resolve_tool_invocation(
            task_id, wait_token, invocation_id, actor, outcome, evidence_ref
        )

    def list_tool_reconciliations(self, task_id: int) -> list[dict[str, Any]]:
        return self.orchestrator.list_tool_reconciliations(task_id)

    def close(self) -> None:
        self.connection.close()


class ApplicationFactory:
    """Central composition root for configured harness applications."""

    @staticmethod
    def create(config_path: str | Path) -> Application:
        orchestrator, connection = build_orchestrator(config_path)
        return Application(orchestrator, connection)
