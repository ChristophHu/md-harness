"""Validate execution results against plans and task criteria."""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.engine.context import ExecutionContext
from harness.engine.plan import ExecutionPlan
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    NextAction,
    ResultStatus,
)


@dataclass(slots=True)
class ValidationResult:
    """Structured validation details for one execution."""

    acceptance_criteria: dict[int, bool] = field(default_factory=dict)
    test_criteria: dict[int, bool] = field(default_factory=dict)
    missing_steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    next_action: NextAction = NextAction.STOP


class Validator:
    """Validate without executing tools or changing persistent state."""

    def validate(
        self,
        context: ExecutionContext,
        plan: ExecutionPlan,
        execution: ExecutionResult,
    ) -> EngineResult:
        """Return a structured workflow result for the completed execution."""
        if any(
            dependency.dependency_type == "blocks" and not dependency.resolved
            for dependency in context.dependencies
        ):
            return self._result(
                ResultStatus.WAITING,
                "A blocking dependency is unresolved.",
                ValidationResult(next_action=NextAction.WAIT),
            )
        if execution.status is ExecutionStatus.WAITING:
            return self._result(
                ResultStatus.WAITING,
                "Execution is waiting for an external prerequisite.",
                ValidationResult(next_action=NextAction.WAIT),
            )
        if execution.status is ExecutionStatus.FAILED:
            action = (
                NextAction.REPLAN
                if execution.next_action is NextAction.REPLAN
                else NextAction.RETRY_EXECUTION
            )
            validation = ValidationResult(
                errors=list(execution.errors), next_action=action
            )
            return self._result(
                ResultStatus.FAILED, "Execution did not succeed.", validation
            )

        executed = {step.step_id: step for step in execution.steps}
        missing_steps = [step.id for step in plan.steps if step.id not in executed]
        failed_steps = [
            step.step_id
            for step in execution.steps
            if step.status is not ExecutionStatus.SUCCESS
        ]
        if failed_steps:
            validation = ValidationResult(
                errors=[f"step_failed:{step_id}" for step_id in failed_steps],
                next_action=NextAction.RETRY_EXECUTION,
            )
            return self._result(
                ResultStatus.FAILED, "One or more execution steps failed.", validation
            )
        if missing_steps:
            validation = ValidationResult(
                missing_steps=missing_steps,
                errors=["plan_steps_missing"],
                next_action=NextAction.REPLAN,
            )
            return self._result(
                ResultStatus.FAILED,
                "Execution plan was not fully executed.",
                validation,
            )

        acceptance = self._criteria_status(context, execution, "acceptance_criteria")
        tests = self._criteria_status(context, execution, "test_criteria")
        if not all(acceptance.values()) or not all(tests.values()):
            validation = ValidationResult(
                acceptance_criteria=acceptance,
                test_criteria=tests,
                errors=["criteria_not_satisfied"],
                next_action=NextAction.RETRY_EXECUTION,
            )
            return self._result(
                ResultStatus.FAILED,
                "Acceptance or test criteria are not satisfied.",
                validation,
            )

        validation = ValidationResult(
            acceptance_criteria=acceptance,
            test_criteria=tests,
            next_action=NextAction.STOP,
        )
        return self._result(ResultStatus.SUCCESS, "Validation successful.", validation)

    @staticmethod
    def _criteria_status(
        context: ExecutionContext,
        execution: ExecutionResult,
        attribute: str,
    ) -> dict[int, bool]:
        criteria = getattr(context, attribute)
        ids = {criterion.id for criterion in criteria}
        completed: set[int] = set()
        for step in execution.steps:
            completed.update(getattr(step, attribute))
        return {criterion_id: criterion_id in completed for criterion_id in ids}

    @staticmethod
    def _result(
        status: ResultStatus,
        message: str,
        validation: ValidationResult,
    ) -> EngineResult:
        return EngineResult(
            status=status,
            message=message,
            errors=validation.errors,
            data={"validation": validation, "next_action": validation.next_action},
        )
