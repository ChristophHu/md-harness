"""Canonical identities for a plan-bound tool approval."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint(value: Any) -> str:
    """Hash JSON-compatible data without depending on key insertion order."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_for_step(plan: Any, step: Any, scope: str) -> dict[str, Any]:
    """Describe precisely the step and permission being requested."""
    from dataclasses import asdict

    return {
        "plan_version": plan.version,
        "plan_fingerprint": fingerprint(asdict(plan)),
        "step_id": step.id,
        "tool": step.tool,
        "arguments_fingerprint": fingerprint(step.arguments),
        "permission_scope": scope,
    }
