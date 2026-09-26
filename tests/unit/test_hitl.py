"""Tests for optional HITL policy and mode safety boundaries."""

import pytest

from harness.config import ConfigError, ExecutionConfig
from harness.engine.hitl import should_request_interaction
from harness.engine.orchestrator import Orchestrator
from harness.storage.human_interaction_store import HumanInteractionStore


@pytest.mark.parametrize(
    ("mode", "interaction_request", "expected"),
    [
        ("minimal", {"required": True}, True),
        ("minimal", {"required": False}, False),
        ("selective", {"request_data": {"risk_level": "high"}}, True),
        ("selective", {"request_data": {"risk_level": "low"}}, False),
        ("selective", {"request_data": {"review_required": True}}, True),
        ("interactive", {"required": False}, True),
    ],
)
def test_hitl_policy_modes(mode, interaction_request, expected):
    assert should_request_interaction(mode, interaction_request) is expected


def test_interactive_mode_requires_persistent_wait_state():
    with pytest.raises(ConfigError, match="requires transactional checkpoint"):
        Orchestrator(
            object(),
            execution_config=ExecutionConfig(hitl_mode="interactive"),
        )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "alien"}, "unsupported type"),
        ({"type": "string", "enum": "yes"}, "enum must be a list"),
        ({"type": "object", "required": [1]}, "must be strings"),
    ],
)
def test_interaction_schema_rejects_invalid_contracts(schema, message):
    with pytest.raises(ValueError, match=message):
        HumanInteractionStore.validate_schema(schema)


@pytest.mark.parametrize(
    ("schema", "response", "message"),
    [
        (None, "yes", "must be an object"),
        ({"type": "boolean"}, "yes", "JSON type"),
        ({"type": "string", "enum": ["yes"]}, "no", "allowed values"),
        ({"type": "object", "properties": []}, {}, "properties must be an object"),
        ({"type": "object", "required": ["reason"]}, {}, "missing required"),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"unexpected": 1},
            "unexpected fields",
        ),
    ],
)
def test_interaction_response_rejects_invalid_answers(schema, response, message):
    with pytest.raises((TypeError, ValueError), match=message):
        HumanInteractionStore.validate_response(schema, response)
