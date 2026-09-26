"""Real SQLite, approval and restart workflow for task agents."""

import json

from harness.application import build_orchestrator
from harness.engine.result import ResultStatus
from harness.storage.project_store import ProjectStore
from harness.storage.task_store import TaskStore


class PlannedModel:
    def __init__(self):
        self.calls = 0

    def generate(self, profile, context, tools):
        self.calls += 1
        assert profile.name == "developer"
        assert [tool["name"] for tool in tools] == ["filesystem"]
        assert context.task_title == "Write artifact"
        return {
            "steps": [
                {
                    "id": "write",
                    "description": "Write artifact",
                    "action": "implement",
                    "tool": "filesystem",
                    "arguments_json": json.dumps(
                        {
                            "operation": "write",
                            "path": "artifact.txt",
                            "content": "agent result",
                        }
                    ),
                    "acceptance_criteria": [1],
                    "test_criteria": [],
                },
                {
                    "id": "verify",
                    "description": "Verify artifact",
                    "action": "validate",
                    "tool": "filesystem",
                    "arguments_json": json.dumps(
                        {
                            "operation": "read",
                            "path": "artifact.txt",
                        }
                    ),
                    "acceptance_criteria": [],
                    "test_criteria": [],
                },
            ]
        }


def config_file(tmp_path, *, version=1):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""project:
  name: agent-test
  workspace_dir: workspace
storage:
  database: state/harness.sqlite
execution:
  dry_run: false
  persistence_mode: required
agents:
  enabled: true
  profiles:
    - name: developer
      version: {version}
      instructions: Plan verifiable file changes.
      task_types: [task]
      tools: [filesystem]
      model: test-model
      max_steps: 2
""",
        encoding="utf-8",
    )
    return config


def setup_task(orchestrator, connection, tmp_path):
    tasks = TaskStore(connection)
    project = ProjectStore(connection).create("agent-test", str(tmp_path / "workspace"))
    task_id = tasks.create(
        "Write artifact", project_id=project, description="Create a checked artifact"
    )
    connection.execute(
        "INSERT INTO task_acceptance_criteria (id, task_id, criterion) VALUES (1, ?, ?)",
        (task_id, "artifact exists"),
    )
    tasks.transition(task_id, "ready")
    tasks.approve(task_id, "owner")
    connection.commit()
    return task_id


def test_agent_approval_restart_and_tool_execution(tmp_path):
    config = config_file(tmp_path)
    orchestrator, connection = build_orchestrator(config)
    model = PlannedModel()
    orchestrator.agent_runner.model = model
    task_id = setup_task(orchestrator, connection, tmp_path)
    first = orchestrator.run(task_id)
    assert first.status is ResultStatus.WAITING
    assert not (tmp_path / "workspace/artifact.txt").exists()
    checkpoint = orchestrator.checkpoint_store.get_active(task_id)
    assert checkpoint["waiting_reason_code"] == "approval"
    assert connection.execute(
        "SELECT profile_name, profile_version FROM agent_task_bindings WHERE task_id = ?",
        (task_id,),
    ).fetchone()[:] == ("developer", 1)
    assert (
        connection.execute(
            "SELECT agent FROM task_attempts WHERE task_id = ? ORDER BY id LIMIT 1",
            (task_id,),
        ).fetchone()[0]
        == "developer"
    )
    token = checkpoint["wait_token"]
    connection.close()

    resumed, second_connection = build_orchestrator(config)
    second_model = PlannedModel()
    resumed.agent_runner.model = second_model
    assert resumed.approve_wait(task_id, token, "reviewer", "write")
    outcome = resumed.resume(task_id)
    assert outcome.status is ResultStatus.SUCCESS
    assert (tmp_path / "workspace/artifact.txt").read_text() == "agent result"
    assert second_model.calls == 0
    assert TaskStore(second_connection).get(task_id)["status"] == "done"
    second_connection.close()

    config.write_text(config.read_text().replace("  enabled: true", "  enabled: false"))
    disabled, last_connection = build_orchestrator(config)
    assert disabled.run(task_id).status is ResultStatus.SUCCESS
    last_connection.close()


def test_changed_agent_profile_cannot_resume_old_approval(tmp_path):
    config = config_file(tmp_path)
    orchestrator, connection = build_orchestrator(config)
    orchestrator.agent_runner.model = PlannedModel()
    task_id = setup_task(orchestrator, connection, tmp_path)
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    token = orchestrator.checkpoint_store.get_active(task_id)["wait_token"]
    connection.close()

    config_file(tmp_path, version=2)
    restarted, second_connection = build_orchestrator(config)
    restarted.agent_runner.model = PlannedModel()
    assert restarted.approve_wait(task_id, token, "reviewer", "write")
    assert restarted.resume(task_id).status is ResultStatus.WAITING
    assert restarted.run(task_id).status is ResultStatus.WAITING
    assert restarted.checkpoint_store.get_active(task_id)["wait_token"] == token
    assert not (tmp_path / "workspace/artifact.txt").exists()
    second_connection.close()


def test_disabling_agents_cannot_fall_back_for_pinned_task(tmp_path):
    config = config_file(tmp_path)
    orchestrator, connection = build_orchestrator(config)
    orchestrator.agent_runner.model = PlannedModel()
    task_id = setup_task(orchestrator, connection, tmp_path)
    assert orchestrator.run(task_id).status is ResultStatus.WAITING
    connection.close()

    config.write_text(config.read_text().replace("  enabled: true", "  enabled: false"))
    restarted, second_connection = build_orchestrator(config)
    assert restarted.agent_runner is None
    assert restarted.resume(task_id).status is ResultStatus.WAITING
    assert restarted.run(task_id).status is ResultStatus.WAITING
    assert not (tmp_path / "workspace/artifact.txt").exists()
    second_connection.close()
