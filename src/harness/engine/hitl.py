"""Policy helpers for optional human-in-the-loop checkpoints."""

from __future__ import annotations

from typing import Any

from harness.config import HITLMode


def should_request_interaction(mode: HITLMode | str, request: dict[str, Any]) -> bool:
    """Return whether an optional request becomes a wait in the selected mode."""
    selected = HITLMode(mode)
    if request.get("required", False):
        return True
    if selected is HITLMode.MINIMAL:
        return False
    if selected is HITLMode.INTERACTIVE:
        return True
    details = request.get("request_data", {})
    return bool(
        details.get("review_required")
        or details.get("risk_level") in {"high", "critical"}
    )


def plan_review_request(plan: Any) -> dict[str, Any]:
    """Build a bound review request from the exact plan to be executed."""
    steps = [
        {
            "id": step.id,
            "description": step.description,
            "tool": step.tool,
            "arguments": step.arguments,
        }
        for step in plan.steps
    ]
    return {
        "kind": "plan_review",
        "prompt": "Review this plan before execution.",
        "response_schema": {
            "type": "object",
            "required": ["decision"],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["approved", "changes_requested"],
                },
                "feedback": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "request_data": {
            "goal": plan.goal,
            "steps": steps,
            "risk_level": "high" if plan.risks else "low",
        },
        "resume_action": "retry_execution",
        "required": False,
    }
