"""Create deterministic execution plans from task contexts."""

from __future__ import annotations

from harness.engine.context import ExecutionContext
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import EngineResult


class Planner:
    """Validate a context and turn it into an execution-only plan.

    The planner never writes files, invokes tools, starts tests or changes
    persistent task state. Those operations belong to later engine stages.
    """

    def plan(self, context: ExecutionContext) -> EngineResult:
        """Return a plan or a structured waiting/failed result."""
        prerequisite = self._validate_context(context)
        if prerequisite is not None:
            return prerequisite

        plan = self._build_plan(context)
        return EngineResult.success("Plan created", plan=plan)

    def _validate_context(self, context: ExecutionContext) -> EngineResult | None:
        if context.approval_status != "approved":
            return EngineResult.waiting(
                "Task approval or required external information is missing."
            )
        if not context.task_title.strip() or not context.task_description.strip():
            return EngineResult.failure(
                "Task context is incomplete.", "title_or_description_missing"
            )
        if not context.acceptance_criteria:
            return EngineResult.failure(
                "Task context is incomplete.", "acceptance_criteria_missing"
            )
        if context.task_status == "waiting":
            return EngineResult.waiting("Task is waiting for an external prerequisite.")
        if any(
            dependency.dependency_type == "blocks" and not dependency.resolved
            for dependency in context.dependencies
        ):
            return EngineResult.waiting("A blocking task dependency is not resolved.")
        if self._first_tool(context, "filesystem", "git") is None:
            return EngineResult.failure(
                "No implementation tool is available.", "implementation_tool_missing"
            )
        if self._first_tool(context, "sqlite", "filesystem") is None:
            return EngineResult.failure(
                "No validation tool is available.", "validation_tool_missing"
            )
        return None

    def _build_plan(self, context: ExecutionContext) -> ExecutionPlan:
        acceptance_ids = [criterion.id for criterion in context.acceptance_criteria]
        test_ids = [criterion.id for criterion in context.test_criteria]
        implementation_tool = self._first_tool(context, "filesystem", "git")
        test_tool = self._first_tool(context, "sqlite", "filesystem")
        assumptions = ["The target workspace is available to the executor."]
        risks = []
        if context.dependencies:
            assumptions.append("Task dependencies are resolved before execution.")
        if context.previous_attempts:
            risks.append("Previous execution attempts exist and should be reviewed.")

        previous_version = (context.last_plan or {}).get("version")
        version = previous_version + 1 if isinstance(previous_version, int) else 1
        reason = None
        if context.last_validation:
            reason = (
                context.last_validation.get("message")
                or "Previous validation was not successful."
            )
        return ExecutionPlan(
            goal=context.task_title,
            steps=[
                PlanStep(
                    id="inspect",
                    description="Inspect the workspace and relevant project context.",
                    action="inspect",
                    tool=implementation_tool,
                ),
                PlanStep(
                    id="implement",
                    description=context.task_description,
                    action="implement",
                    tool=implementation_tool,
                    acceptance_criteria=acceptance_ids,
                ),
                PlanStep(
                    id="validate",
                    description="Run the defined validation criteria.",
                    action="validate",
                    tool=test_tool,
                    test_criteria=test_ids,
                ),
            ],
            assumptions=assumptions,
            risks=risks,
            version=version,
            replanned_from=previous_version,
            reason=reason,
        )

    @staticmethod
    def _first_tool(context: ExecutionContext, *names: str) -> str | None:
        return next((name for name in names if name in context.available_tools), None)
