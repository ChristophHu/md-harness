"""Agent profile, model boundary and runner tests."""

import io
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from harness.agents.registry import AgentProfile, AgentProfileError, AgentRegistry
from harness.agents.runner import AgentRunner, OpenAIResponsesModel
from harness.config import ConfigError, load_config
from harness.engine.context import (
    AcceptanceCriterion,
    DependencyContext,
    ExecutionContext,
)
from harness.engine.orchestrator import Orchestrator
from harness.engine.result import ResultStatus, WaitReason
from harness.storage.agent_store import AgentStore
from harness.storage.database import connect, initialize_database
from harness.storage.task_store import TaskStore
from harness.storage.transaction import TransactionError
from harness.tools.factory import ToolRegistryFactory


def profile(**overrides):
    values = {
        "name": "developer",
        "version": 1,
        "instructions": "Make safe changes",
        "task_types": ("task",),
        "tools": ("filesystem",),
        "model": "test-model",
        "max_steps": 2,
    }
    values.update(overrides)
    return AgentProfile(**values)


def context(task_id):
    return ExecutionContext(
        task_id=task_id,
        task_title="Task",
        task_description="Do work",
        task_type="task",
        task_status="ready",
        approval_status="approved",
        acceptance_criteria=[AcceptanceCriterion(1, "Works")],
        available_tools=["filesystem", "sqlite"],
        workspace="/tmp",
    )


def step(**overrides):
    data = {
        "id": "write",
        "description": "Write file",
        "action": "implement",
        "tool": "filesystem",
        "arguments_json": json.dumps(
            {"operation": "write", "path": "x.txt", "content": "hello"}
        ),
        "acceptance_criteria": [1],
        "test_criteria": [],
    }
    data.update(overrides)
    return data


@pytest.fixture
def runner(tmp_path):
    connection = connect(initialize_database(tmp_path / "agents.sqlite"))
    task_id = TaskStore(connection).create("Task")
    registry = ToolRegistryFactory.create(
        tmp_path, enabled_tools={"filesystem", "sqlite"}
    )
    model = Mock()
    model.generate.return_value = {"steps": [step()]}
    runner = AgentRunner(
        AgentRegistry([profile()]), AgentStore(connection), registry, model
    )
    yield runner, context(task_id), connection
    connection.close()


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"name": ""}, "name"),
        ({"version": 0}, "version"),
        ({"version": True}, "version"),
        ({"instructions": ""}, "instructions"),
        ({"model": ""}, "model"),
        ({"task_types": ()}, "task_types"),
        ({"task_types": (1,)}, "task_types"),
        ({"tools": ()}, "tools"),
        ({"tools": (1,)}, "tools"),
        ({"tools": ("filesystem", "filesystem")}, "unique"),
        ({"max_steps": 0}, "max_steps"),
        ({"max_steps": True}, "max_steps"),
        ({"enabled": "yes"}, "enabled"),
    ],
)
def test_profile_rejects_invalid_fields(changes, message):
    with pytest.raises(AgentProfileError, match=message):
        profile(**changes)


def test_profile_mapping_registry_and_fingerprint():
    data = {
        "name": "developer",
        "version": 1,
        "instructions": "Make safe changes",
        "task_types": ["task"],
        "tools": ["filesystem"],
        "model": "test-model",
    }
    parsed = AgentProfile.from_mapping(data)
    assert parsed.max_steps == 8
    assert parsed.fingerprint == AgentProfile.from_mapping(data).fingerprint
    assert parsed.fingerprint != replace(parsed, version=2).fingerprint
    registry = AgentRegistry([parsed, profile(name="reviewer", task_types=("review",))])
    assert registry.select("task") == parsed
    assert registry.select("review", "reviewer").name == "reviewer"
    with pytest.raises(AgentProfileError, match="duplicate"):
        AgentRegistry([parsed, parsed])
    with pytest.raises(AgentProfileError, match="unavailable"):
        registry.select("task", "reviewer")
    with pytest.raises(AgentProfileError, match="no enabled"):
        registry.select("other")
    with pytest.raises(AgentProfileError, match="unavailable"):
        AgentRegistry([replace(parsed, enabled=False)]).select("task", "developer")


@pytest.mark.parametrize("data", [None, {}, {"task_types": "task", "tools": []}])
def test_profile_mapping_rejects_bad_shape(data):
    with pytest.raises(AgentProfileError):
        AgentProfile.from_mapping(data)


def test_profile_mapping_rejects_missing_required_name():
    with pytest.raises(AgentProfileError, match="invalid agent profile fields"):
        AgentProfile.from_mapping({"task_types": ["task"], "tools": ["filesystem"]})


@pytest.mark.parametrize(
    "agents,message",
    [
        ("invalid", "agents.enabled"),
        ({"enabled": "yes"}, "agents.enabled"),
        ({"profiles": "invalid"}, "agents.profiles"),
        ({"profiles": [{}]}, "task_types and tools"),
        ({"enabled": True}, "at least one profile"),
        (
            {
                "profiles": [
                    {
                        "name": "a",
                        "version": 1,
                        "instructions": "safe",
                        "task_types": ["task"],
                        "tools": ["filesystem"],
                        "model": "m",
                    },
                    {
                        "name": "a",
                        "version": 1,
                        "instructions": "safe",
                        "task_types": ["task"],
                        "tools": ["filesystem"],
                        "model": "m",
                    },
                ]
            },
            "duplicate",
        ),
    ],
)
def test_agent_config_rejects_bad_settings(tmp_path, agents, message):
    path = tmp_path / "config.yaml"
    import yaml

    path.write_text(yaml.safe_dump({"agents": agents}), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_orchestrator_scopes_context_without_claim(runner):
    agent_runner, state, _ = runner
    builder = Mock(build=Mock(return_value=state))
    orchestrator = Orchestrator(builder, agent_runner=agent_runner)
    assert orchestrator.build_context(state.task_id) is state
    assert state.available_tools == ["filesystem"]


def test_store_assignment_binding_and_attempt_attribution(runner):
    agent_runner, state, connection = runner
    store = agent_runner.store
    agent = store.create("developer")
    assert store.assigned_name(state.task_id) is None
    store.assign(state.task_id, agent)
    assert store.assigned_name(state.task_id) == "developer"
    agent_runner.scope(state, bind=True)
    assert state.available_tools == ["filesystem"]
    assert state.assigned_agent == "developer"
    assert store.binding(state.task_id)["profile_version"] == 1
    assert (
        store.bind(state.task_id, profile())["profile_fingerprint"]
        == profile().fingerprint
    )
    with pytest.raises(AgentProfileError, match="cannot reassign"):
        store.assign(state.task_id, agent)
    attempt = TaskStore(connection).record_attempt(state.task_id, "running")
    assert (
        connection.execute(
            "SELECT agent FROM task_attempts WHERE id = ?", (attempt,)
        ).fetchone()[0]
        == "developer"
    )
    connection.execute("UPDATE agents SET enabled = 0 WHERE id = ?", (agent,))
    again = context(state.task_id)
    agent_runner.scope(again)
    assert "disabled" in again.metadata["agent_error"]
    assert again.available_tools == []


def test_runner_stops_on_pinned_profile_change(runner):
    agent_runner, state, _ = runner
    agent_runner.scope(state, bind=True)
    agent_runner.registry = AgentRegistry([replace(profile(), version=2)])
    resumed = context(state.task_id)
    agent_runner.scope(resumed)
    assert "changed" in resumed.metadata["agent_error"]
    assert agent_runner.plan(resumed).status is ResultStatus.WAITING
    assert agent_runner.plan(resumed).wait_reason is WaitReason.MANUAL_REPLAN


def test_agent_binding_is_rechecked_at_tool_boundary(runner):
    agent_runner, state, connection = runner
    with pytest.raises(AgentProfileError, match="not bound"):
        agent_runner.assert_bound(state)
    agent_runner.scope(state)
    with pytest.raises(AgentProfileError, match="not bound"):
        agent_runner.assert_bound(state)
    agent_runner.scope(state, bind=True)
    agent_runner.assert_bound(state)
    orchestrator = Orchestrator(Mock(), agent_runner=agent_runner)
    orchestrator._check_agent_ownership(state)

    TaskStore(connection).update(state.task_id, assigned_agent="other")
    agent_runner.registry = AgentRegistry([profile(), profile(name="other")])
    with pytest.raises(AgentProfileError, match="changed"):
        agent_runner.assert_bound(state)
    with pytest.raises(TransactionError, match="changed"):
        orchestrator._check_agent_ownership(state)

    TaskStore(connection).update(state.task_id, assigned_agent=None)
    other_id = agent_runner.store.create("other")
    connection.execute(
        "INSERT INTO task_assignments (task_id, agent_id) VALUES (?, ?)",
        (state.task_id, other_id),
    )
    with pytest.raises(AgentProfileError, match="changed"):
        agent_runner.assert_bound(state)


def test_agent_binding_rejects_profile_reconfiguration(runner):
    agent_runner, state, _ = runner
    agent_runner.scope(state, bind=True)
    agent_runner.registry = AgentRegistry([replace(profile(), version=2)])
    with pytest.raises(AgentProfileError, match="changed"):
        agent_runner.assert_bound(state)


def test_runner_generates_scoped_plan_and_marks_effectful_step(runner):
    agent_runner, state, _ = runner
    agent_runner.scope(state, bind=True)
    state.last_plan = {"version": 3}
    result = agent_runner.plan(state)
    assert result.status is ResultStatus.SUCCESS
    plan = result.data["plan"]
    assert plan.version == 4 and plan.replanned_from == 3
    assert plan.steps[0].metadata["requires_approval"] is True
    assert plan.steps[0].acceptance_criteria == [1]
    assert agent_runner.model.generate.call_args.args[2][0]["name"] == "filesystem"


def test_runner_read_only_step_needs_no_scoped_approval(runner):
    agent_runner, state, _ = runner
    agent_runner.model.generate.return_value = {
        "steps": [
            step(
                arguments_json=json.dumps({"operation": "read", "path": "x.txt"}),
            )
        ]
    }
    agent_runner.scope(state)
    assert agent_runner.plan(state).data["plan"].steps[0].metadata == {}


@pytest.mark.parametrize(
    "change,message",
    [
        ({"steps": []}, "step count"),
        ({"steps": [step(), step(id="second"), step(id="third")]}, "step count"),
        ({"steps": ["bad"]}, "step is invalid"),
        ({"steps": [step(id="")]}, "step is invalid"),
        ({"steps": [step(arguments_json=0)]}, "step is invalid"),
        ({"steps": [step(tool="sqlite")]}, "forbidden tool"),
        ({"steps": [step(arguments_json="[]")]}, "arguments must be a mapping"),
        ({"steps": [step(acceptance_criteria="1")]}, "criterion IDs"),
        ({"steps": [step(test_criteria=[True])]}, "criterion IDs"),
        ({"steps": [step(acceptance_criteria=[99])]}, "unknown criteria"),
        ({"steps": [step(test_criteria=[99])]}, "unknown criteria"),
        (
            {"steps": [step(arguments_json='{"operation":"write"}')]},
            "missing arguments",
        ),
        ({"steps": [step(), step()]}, "must be unique"),
        ({"oops": []}, "'steps'"),
        ({"steps": [step(arguments_json="bad json")]}, "Expecting value"),
    ],
)
def test_runner_rejects_bad_model_plans(runner, change, message):
    agent_runner, state, _ = runner
    agent_runner.model.generate.return_value = change
    agent_runner.scope(state)
    result = agent_runner.plan(state)
    assert result.status is ResultStatus.FAILED
    assert message in result.message


def test_runner_rejects_missing_binding_and_prerequisites(runner):
    agent_runner, state, _ = runner
    assert agent_runner.plan(state).status is ResultStatus.WAITING
    agent_runner.scope(state)
    state.approval_status = "pending"
    assert agent_runner.plan(state).wait_reason is WaitReason.APPROVAL
    state.approval_status = "approved"
    state.dependencies = [DependencyContext(2, "executing", resolved=False)]
    assert agent_runner.plan(state).wait_reason is WaitReason.EXTERNAL_INFORMATION
    state.dependencies = []
    state.acceptance_criteria = []
    assert agent_runner.plan(state).status is ResultStatus.FAILED


def test_model_uses_structured_responses_without_exposing_tools(monkeypatch, runner):
    _, state, _ = runner
    state.previous_events = [
        {"event_type": "task.planning", "payload": "ignored"},
        {"event_type": "task.execution.completed", "payload": "observation"},
    ]
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response(
            json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {"content": [{"type": "output_text", "text": '{"steps":[]}'}]}
                    ],
                }
            ).encode()
        )

    monkeypatch.setattr("harness.agents.runner.urlopen", fake_open)
    model = OpenAIResponsesModel(Mock(get=Mock(return_value="secret")), timeout=12)
    assert model.generate(profile(), state, []) == {"steps": []}
    payload = json.loads(captured["request"].data)
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert "tools" not in payload
    assert json.loads(payload["input"])["previous_execution_events"] == ["observation"]
    assert captured["request"].get_header("Authorization") == "Bearer secret"
    model.secret_provider.get.assert_called_with("openai_api_key")
    assert captured["timeout"] == 12


def test_model_handles_missing_key_incomplete_and_empty_output(monkeypatch, runner):
    _, state, _ = runner
    model = OpenAIResponsesModel(Mock(get=Mock(return_value=None)))
    with pytest.raises(AgentProfileError, match="API key"):
        model.generate(profile(), state, [])
    model.secret_provider.get.return_value = "secret"

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(
        "harness.agents.runner.urlopen",
        lambda *_a, **_k: Response(b'{"status":"incomplete"}'),
    )
    with pytest.raises(AgentProfileError, match="did not complete"):
        model.generate(profile(), state, [])
    monkeypatch.setattr(
        "harness.agents.runner.urlopen",
        lambda *_a, **_k: Response(b'{"status":"completed","output":[]}'),
    )
    with pytest.raises(AgentProfileError, match="no plan"):
        model.generate(profile(), state, [])
