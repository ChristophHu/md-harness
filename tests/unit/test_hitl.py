"""Tests for optional HITL policy and mode safety boundaries."""

import pytest

from harness.config import ConfigError, ExecutionConfig
from harness.engine.hitl import should_request_interaction
from harness.engine.orchestrator import Orchestrator


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
