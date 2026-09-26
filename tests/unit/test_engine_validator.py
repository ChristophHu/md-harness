"""Tests for execution validation."""

from harness.engine.context import (
    AcceptanceCriterion,
    DependencyContext,
    ExecutionContext,
)
from harness.engine.context import (
    TestCriterion as Criterion,
)
from harness.engine.plan import ExecutionPlan, PlanStep
from harness.engine.result import (
    ExecutionResult,
    ExecutionStatus,
    NextAction,
    ResultStatus,
    StepExecution,
)
from harness.engine.validator import ValidationResult, Validator


def context(**overrides):
    values = {
        "task_id": 1,
        "task_title": "Task",
        "acceptance_criteria": [AcceptanceCriterion(1, "Works")],
        "test_criteria": [Criterion(2, "Tests pass")],
    }
    values.update(overrides)
    return ExecutionContext(**values)


def plan():
    return ExecutionPlan(
        "Task",
        [
            PlanStep("implement", "Implement", "implement", acceptance_criteria=[1]),
            PlanStep("validate", "Validate", "validate", test_criteria=[2]),
        ],
    )


def execution(**overrides):
    values = {
        "status": ExecutionStatus.SUCCESS,
        "steps": [
            StepExecution(
                "implement", ExecutionStatus.SUCCESS, acceptance_criteria=[1]
            ),
            StepExecution("validate", ExecutionStatus.SUCCESS, test_criteria=[2]),
        ],
    }
    values.update(overrides)
    return ExecutionResult(**values)


def test_validator_accepts_successful_execution():
    result = Validator().validate(context(), plan(), execution())

    assert result.status is ResultStatus.SUCCESS
    validation = result.data["validation"]
    assert isinstance(validation, ValidationResult)
    assert validation.acceptance_criteria == {1: True}
    assert validation.test_criteria == {2: True}
    assert validation.next_action is NextAction.STOP


def test_validator_waits_for_dependency_or_execution():
    dependency = DependencyContext(9, "executing", resolved=False)
    result = Validator().validate(
        context(dependencies=[dependency]), plan(), execution()
    )
    assert result.status is ResultStatus.WAITING
    assert result.data["next_action"] is NextAction.WAIT

    waiting = Validator().validate(
        context(), plan(), execution(status=ExecutionStatus.WAITING)
    )
    assert waiting.status is ResultStatus.WAITING


def test_validator_propagates_execution_failure_and_replan():
    result = Validator().validate(
        context(),
        plan(),
        execution(
            status=ExecutionStatus.FAILED,
            errors=["tool failed"],
            next_action=NextAction.REPLAN,
        ),
    )

    assert result.status is ResultStatus.FAILED
    assert result.errors == ["tool failed"]
    assert result.data["next_action"] is NextAction.REPLAN


def test_validator_requests_retry_for_failed_step():
    failed = execution(steps=[StepExecution("implement", ExecutionStatus.FAILED)])
    result = Validator().validate(context(), plan(), failed)

    assert result.status is ResultStatus.FAILED
    assert result.data["next_action"] is NextAction.RETRY_EXECUTION


def test_validator_requests_replan_for_missing_step():
    result = Validator().validate(
        context(),
        plan(),
        execution(steps=[StepExecution("implement", ExecutionStatus.SUCCESS)]),
    )

    assert result.status is ResultStatus.FAILED
    assert result.data["validation"].missing_steps == ["validate"]
    assert result.data["next_action"] is NextAction.REPLAN


def test_validator_requests_retry_for_unsatisfied_criteria():
    incomplete = execution(
        steps=[
            StepExecution("implement", ExecutionStatus.SUCCESS),
            StepExecution("validate", ExecutionStatus.SUCCESS, test_criteria=[2]),
        ]
    )
    result = Validator().validate(context(), plan(), incomplete)

    assert result.status is ResultStatus.FAILED
    assert result.data["validation"].acceptance_criteria == {1: False}
    assert result.data["next_action"] is NextAction.RETRY_EXECUTION


def test_validator_maps_test_runner_results_to_criteria():
    test_plan = ExecutionPlan(
        "Task",
        [PlanStep("tests", "Tests", "run_tests", "test_runner", test_criteria=[2])],
    )
    successful = execution(
        steps=[
            StepExecution(
                "tests",
                ExecutionStatus.SUCCESS,
                tool="test_runner",
                test_criteria=[2],
                result={"exit_code": 0, "passed": True, "timed_out": False},
            )
        ]
    )
    result = Validator().validate(context(), test_plan, successful)
    assert result.data["validation"].test_results == {2: True}

    empty = execution(
        steps=[
            StepExecution(
                "tests",
                ExecutionStatus.SUCCESS,
                tool="test_runner",
                test_criteria=[2],
                result="raw",
            )
        ]
    )
    assert Validator()._test_results(empty) == {}


def test_validator_checks_file_content_and_git_evidence():
    concrete_plan = ExecutionPlan(
        "Create file",
        [
            PlanStep(
                "write",
                "Write",
                "implement",
                "filesystem",
                {"operation": "write", "path": "a.txt", "content": "expected"},
                acceptance_criteria=[1],
            ),
            PlanStep(
                "read",
                "Read",
                "validate",
                "filesystem",
                {"operation": "read", "path": "a.txt"},
                test_criteria=[2],
            ),
            PlanStep("status", "Status", "git_status", "git", {"operation": "status"}),
        ],
    )
    successful = execution(
        steps=[
            StepExecution(
                "write",
                ExecutionStatus.SUCCESS,
                result="written",
                acceptance_criteria=[1],
            ),
            StepExecution(
                "read", ExecutionStatus.SUCCESS, result="expected", test_criteria=[2]
            ),
            StepExecution("status", ExecutionStatus.SUCCESS, result="?? a.txt"),
        ]
    )
    result = Validator().validate(context(), concrete_plan, successful)
    assert result.status is ResultStatus.SUCCESS
    assert result.data["validation"].content_checks == {"a.txt": True}
    assert result.data["validation"].diff_checks == {"a.txt": True}

    mismatch = execution(
        steps=[
            StepExecution("write", ExecutionStatus.SUCCESS, acceptance_criteria=[1]),
            StepExecution(
                "read", ExecutionStatus.SUCCESS, result="wrong", test_criteria=[2]
            ),
            StepExecution("status", ExecutionStatus.SUCCESS, result=""),
        ]
    )
    failed = Validator().validate(context(), concrete_plan, mismatch)
    assert failed.status is ResultStatus.FAILED
    assert "content_mismatch:a.txt" in failed.errors
    assert failed.data["validation"].diff_checks == {}


def test_validator_rejects_unexpected_files_and_diff_rules():
    plan = ExecutionPlan(
        "Change",
        [
            PlanStep(
                "diff",
                "Diff",
                "validate",
                "git",
                {"operation": "diff"},
                metadata={
                    "allowed_files": ["src/ok.py"],
                    "required_in_diff": ["expected line"],
                    "forbidden_in_diff": ["secret"],
                },
            )
        ],
    )
    result = Validator().validate(
        context(),
        plan,
        execution(
            steps=[
                StepExecution(
                    "diff", ExecutionStatus.SUCCESS, result="expected line secret"
                ),
            ],
            changed_files=["src/ok.py", "src/unexpected.py"],
        ),
    )
    assert result.status is ResultStatus.FAILED
    assert "unexpected_file:src/unexpected.py" in result.errors
    assert "forbidden_diff:secret" in result.errors
    assert result.data["validation"].next_action is NextAction.REPLAN
