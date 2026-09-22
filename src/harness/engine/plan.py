"""Structured plans shared by the planner and executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PlanStep:
    """One ordered action in an execution plan."""

    id: str
    description: str
    action: str
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: list[int] = field(default_factory=list)
    test_criteria: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionPlan:
    """Complete plan produced for one execution context."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    version: int = 1
    replanned_from: int | None = None
    reason: str | None = None
