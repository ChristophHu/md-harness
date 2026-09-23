from harness.application import build_orchestrator
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
""",
        encoding="utf-8",
    )

    orchestrator, connection = build_orchestrator(config)
    try:
        assert orchestrator.max_retries == 4
        assert orchestrator.max_cycles == 7
        assert orchestrator.persistence_mode is PersistenceMode.REQUIRED
        assert orchestrator.context_builder.dry_run is False
        assert (
            orchestrator.executor.registry is orchestrator.context_builder.tool_registry
        )
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


def test_build_orchestrator_rejects_disabled_persistence(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("execution:\n  persistence_mode: disabled\n", encoding="utf-8")
    try:
        build_orchestrator(config)
    except ConfigError as error:
        assert "disabled persistence" in str(error)
    else:
        raise AssertionError("disabled persistence must be rejected")
