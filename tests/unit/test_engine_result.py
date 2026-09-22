"""Tests for standardized engine results."""

from datetime import UTC, datetime

from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    NextAction,
    ResultStatus,
    StepExecution,
    ToolExecutionResult,
)


def test_success_result() -> None:
    result = EngineResult.success("Plan created", plan=["run tests"])

    assert result.status is ResultStatus.SUCCESS
    assert result.successful is True
    assert result.message == "Plan created"
    assert result.data == {"plan": ["run tests"]}
    assert result.errors == []
    assert result.artifacts == []
    assert result.tests == {}


def test_failure_result() -> None:
    result = EngineResult.failure("Validation failed", "test_failed", "lint_failed")

    assert result.status is ResultStatus.FAILED
    assert result.successful is False
    assert result.message == "Validation failed"
    assert result.errors == ["test_failed", "lint_failed"]


def test_waiting_result() -> None:
    result = EngineResult.waiting("Approval required")

    assert result.status is ResultStatus.WAITING
    assert result.successful is False
    assert result.message == "Approval required"
    assert result.errors == []


def test_execution_result_models_step_and_tool_details() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    tool_result = ToolExecutionResult(
        output="ok",
        data={"files": ["a.py"]},
        exit_code=0,
    )
    step = StepExecution(
        step_id="implement",
        status=ExecutionStatus.SUCCESS,
        tool="filesystem",
        arguments={"operation": "write"},
        result=tool_result,
        artifacts=["report.txt"],
        changed_files=["a.py"],
        started_at=started,
        completed_at=completed,
    )
    execution = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        steps=[step],
        changed_files=["a.py"],
        artifacts=["report.txt"],
        next_action=NextAction.VALIDATE,
        attempt_id=4,
        dry_run=True,
        started_at=started,
        completed_at=completed,
    )

    assert execution.steps[0].result.output == "ok"
    assert execution.next_action is NextAction.VALIDATE
    assert execution.attempt_id == 4
    assert execution.dry_run is True
    assert execution.completed_at == completed


def test_execution_status_and_next_action_values_are_stable() -> None:
    assert {status.value for status in ExecutionStatus} == {
        "success",
        "failed",
        "waiting",
        "skipped",
    }
    assert {action.value for action in NextAction} == {
        "validate",
        "retry_execution",
        "replan",
        "wait",
        "stop",
    }


def test_tool_execution_result_supports_error_and_exit_code() -> None:
    result = ToolExecutionResult(error="permission denied", exit_code=1)

    assert result.output is None
    assert result.data == {}
    assert result.error == "permission denied"
    assert result.exit_code == 1
