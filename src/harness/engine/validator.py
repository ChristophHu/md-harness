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
    WaitReason,
)


@dataclass(slots=True)
class ValidationResult:
    """Structured validation details for one execution."""

    acceptance_criteria: dict[int, bool] = field(default_factory=dict)
    test_criteria: dict[int, bool] = field(default_factory=dict)
    missing_steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    content_checks: dict[str, bool] = field(default_factory=dict)
    diff_checks: dict[str, bool] = field(default_factory=dict)
    test_results: dict[int, bool] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
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
                WaitReason.EXTERNAL_INFORMATION,
            )
        if execution.status is ExecutionStatus.WAITING:
            return self._result(
                ResultStatus.WAITING,
                "Execution is waiting for an external prerequisite.",
                ValidationResult(next_action=NextAction.WAIT),
                execution.wait_reason,
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
            if step.status not in {ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED}
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
        content_checks, diff_checks, evidence_errors, changed_files, evidence = (
            self._evidence_checks(plan, execution)
        )
        test_results = self._test_results(execution)
        failed_tests = [key for key, passed in test_results.items() if not passed]
        if (
            not all(acceptance.values())
            or not all(tests.values())
            or evidence_errors
            or failed_tests
        ):
            validation = ValidationResult(
                acceptance_criteria=acceptance,
                test_criteria=tests,
                content_checks=content_checks,
                diff_checks=diff_checks,
                test_results=test_results,
                errors=[
                    "criteria_not_satisfied",
                    *evidence_errors,
                    *[f"test_failed:{key}" for key in failed_tests],
                ],
                next_action=(
                    NextAction.REPLAN
                    if any(
                        error.startswith(
                            ("unexpected_file:", "forbidden_diff:", "missing_diff:")
                        )
                        for error in evidence_errors
                    )
                    else NextAction.RETRY_EXECUTION
                ),
                changed_files=changed_files,
                evidence=evidence,
            )
            return self._result(
                ResultStatus.FAILED,
                "Acceptance or test criteria are not satisfied.",
                validation,
            )

        validation = ValidationResult(
            acceptance_criteria=acceptance,
            test_criteria=tests,
            content_checks=content_checks,
            diff_checks=diff_checks,
            test_results=test_results,
            changed_files=changed_files,
            evidence=evidence,
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
    def _evidence_checks(
        plan: ExecutionPlan, execution: ExecutionResult
    ) -> tuple[
        dict[str, bool], dict[str, bool], list[str], list[str], list[dict[str, object]]
    ]:
        """Check read-back content and local Git evidence for concrete plans."""
        executed = {step.step_id: step for step in execution.steps}
        writes = {
            step.arguments["path"]: step.arguments["content"]
            for step in plan.steps
            if step.tool == "filesystem"
            and step.arguments.get("operation") == "write"
            and "path" in step.arguments
            and "content" in step.arguments
        }
        content_checks = {}
        errors = []
        evidence: list[dict[str, object]] = []
        for path, expected in writes.items():
            read_step = next(
                (
                    step
                    for step in plan.steps
                    if step.tool == "filesystem"
                    and step.arguments.get("operation") == "read"
                    and step.arguments.get("path") == path
                ),
                None,
            )
            actual = (
                executed.get(read_step.id).result
                if read_step and read_step.id in executed
                else None
            )
            content_checks[path] = actual == expected
            evidence.append(
                {"type": "file_content", "path": path, "passed": content_checks[path]}
            )
            if not content_checks[path]:
                errors.append(f"content_mismatch:{path}")

        git_evidence = "\n".join(
            str(executed[step.id].result)
            for step in plan.steps
            if step.tool == "git"
            and step.arguments.get("operation") in {"status", "diff"}
            and step.id in executed
        )
        diff_checks = {path: path in git_evidence for path in writes if git_evidence}
        errors.extend(
            f"git_change_missing:{path}"
            for path, present in diff_checks.items()
            if not present
        )
        changed_files = sorted(set(execution.changed_files))
        allowed = set().union(
            *(set(step.metadata.get("allowed_files", [])) for step in plan.steps)
        )
        if allowed:
            errors.extend(
                f"unexpected_file:{path}"
                for path in changed_files
                if path not in allowed
            )
        required_diff = [
            text
            for step in plan.steps
            for text in step.metadata.get("required_in_diff", [])
        ]
        forbidden_diff = [
            text
            for step in plan.steps
            for text in step.metadata.get("forbidden_in_diff", [])
        ]
        errors.extend(
            f"missing_diff:{text}" for text in required_diff if text not in git_evidence
        )
        errors.extend(
            f"forbidden_diff:{text}" for text in forbidden_diff if text in git_evidence
        )
        evidence.extend(
            {
                "type": "changed_files",
                "files": changed_files,
                "passed": not any(
                    error.startswith("unexpected_file:") for error in errors
                ),
            }
            for _ in [0]
        )
        return content_checks, diff_checks, errors, changed_files, evidence

    @staticmethod
    def _test_results(execution: ExecutionResult) -> dict[int, bool]:
        results: dict[int, bool] = {}
        for step in execution.steps:
            if step.tool != "test_runner":
                continue
            data = step.result.data if hasattr(step.result, "data") else step.result
            if not isinstance(data, dict):
                continue
            passed = (
                data.get("exit_code") == 0
                and data.get("passed") is True
                and not data.get("timed_out", False)
            )
            for criterion_id in step.test_criteria:
                results[criterion_id] = passed
        return results

    @staticmethod
    def _result(
        status: ResultStatus,
        message: str,
        validation: ValidationResult,
        wait_reason: WaitReason | None = None,
    ) -> EngineResult:
        return EngineResult(
            status=status,
            message=message,
            errors=validation.errors,
            wait_reason=wait_reason,
            data={"validation": validation, "next_action": validation.next_action},
        )
