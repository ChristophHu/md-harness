from unittest.mock import Mock

from harness.application import Application, ApplicationFactory, build_orchestrator
from harness.config import ConfigError, ExecutionConfig, PersistenceMode


def test_build_orchestrator_wires_execution_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
project:
  name: test-project
  workspace_dir: workspace
storage:
  database: state/test.sqlite
execution:
  dry_run: false
  max_retries: 4
  max_cycles: 7
  persistence_mode: required
  hitl:
    mode: interactive
""",
        encoding="utf-8",
    )

    orchestrator, connection = build_orchestrator(config)
    try:
        assert orchestrator.max_retries == 4
        assert orchestrator.max_cycles == 7
        assert orchestrator.persistence_mode is PersistenceMode.REQUIRED
        assert orchestrator.hitl_mode.value == "interactive"
        assert orchestrator.context_builder.hitl_mode == "interactive"
        assert orchestrator.context_builder.dry_run is False
        assert orchestrator.secret_provider.providers[0].service_prefix == "dev-harness"
        assert orchestrator.secret_provider.providers[0]._service("github_token") == (
            "dev-harness/github-token"
        )
        assert (
            orchestrator.executor.registry is orchestrator.context_builder.tool_registry
        )
    finally:
        connection.close()


def test_build_orchestrator_supports_api_thread_mode(tmp_path):
    from threading import Thread

    config = tmp_path / "config.yaml"
    config.write_text("project:\n  workspace_dir: .\n", encoding="utf-8")
    orchestrator, connection = build_orchestrator(config, api_mode=True)
    try:
        outcome = []
        worker = Thread(
            target=lambda: outcome.append(connection.execute("SELECT 1").fetchone()[0])
        )
        worker.start()
        worker.join()
        assert outcome == [1]
        assert orchestrator is not None
    finally:
        connection.close()


def test_orchestrator_execution_config_overrides_constructor_defaults():
    from harness.engine.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        type("Builder", (), {})(),
        execution_config=ExecutionConfig(
            dry_run=False,
            max_retries=5,
            max_cycles=8,
            persistence_mode=PersistenceMode.OPTIONAL,
        ),
    )
    assert (orchestrator.max_retries, orchestrator.max_cycles) == (5, 8)
    assert orchestrator.persistence_mode is PersistenceMode.OPTIONAL


def test_application_factory_owns_and_closes_connection(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("project:\n  workspace_dir: .\n", encoding="utf-8")
    application = ApplicationFactory.create(config)
    assert (
        application.orchestrator.context_builder.tool_registry
        is application.orchestrator.executor.registry
    )
    application.close()


def test_build_orchestrator_rejects_disabled_persistence(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("execution:\n  persistence_mode: disabled\n", encoding="utf-8")
    try:
        build_orchestrator(config)
    except ConfigError as error:
        assert "disabled persistence" in str(error)
    else:
        raise AssertionError("disabled persistence must be rejected")


def test_build_orchestrator_uses_vault_path_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("project:\n  workspace_dir: .\n", encoding="utf-8")
    vault = tmp_path / "project-vault"
    (tmp_path / ".env").write_text(f"OBSIDIAN_VAULT_PATH={vault}\n", encoding="utf-8")
    orchestrator, connection = build_orchestrator(config)
    try:
        assert orchestrator.decision_vault.root == vault
        assert orchestrator.context_builder.knowledge_loader is not None
    finally:
        connection.close()


def test_application_forwards_human_and_tool_reconciliation_calls():
    orchestrator = Mock()
    connection = Mock()
    application = Application(orchestrator, connection)
    assert (
        application.list_human_interactions(1)
        is orchestrator.list_human_interactions.return_value
    )
    assert (
        application.answer_human_interaction(1, "interaction", "owner", "yes")
        is orchestrator.answer_human_interaction.return_value
    )
    assert (
        application.suggest_human_interaction(1, "question")
        is orchestrator.suggest_human_interaction.return_value
    )
    assert (
        application.publish_vault_decisions(limit=2)
        is orchestrator.publish_vault_decisions.return_value
    )
    assert (
        application.resolve_tool_invocation(
            1, "wait", "invocation", "owner", "completed", "proof"
        )
        is orchestrator.resolve_tool_invocation.return_value
    )
    assert (
        application.list_tool_reconciliations(1)
        is orchestrator.list_tool_reconciliations.return_value
    )
    application.close()
    connection.close.assert_called_once_with()
