"""Tests for deterministic planner behavior."""

import pytest

from harness.engine.context import (
    AcceptanceCriterion,
    ChangeRequest,
    DependencyContext,
    ExecutionContext,
    FileChange,
)
from harness.engine.context import (
    TestCriterion as Criterion,
)
from harness.engine.plan import ExecutionPlan
from harness.engine.planner import Planner
from harness.engine.result import ResultStatus


def context(**overrides):
    values = {
        "task_id": 1,
        "task_title": "Add healthcheck",
        "task_description": "Implement and test the SQLite healthcheck.",
        "approval_status": "approved",
        "acceptance_criteria": [AcceptanceCriterion(10, "Healthcheck works")],
        "test_criteria": [Criterion(20, "Run tests", command="pytest")],
        "available_tools": ["filesystem", "sqlite"],
    }
    values.update(overrides)
    return ExecutionContext(**values)


def test_planner_creates_structured_plan():
    result = Planner().plan(context())

    assert result.status is ResultStatus.SUCCESS
    plan = result.data["plan"]
    assert isinstance(plan, ExecutionPlan)
    assert plan.goal == "Add healthcheck"
    assert [step.id for step in plan.steps] == ["inspect", "implement", "validate"]
    assert plan.steps[1].acceptance_criteria == [10]
    assert plan.steps[2].test_criteria == [20]
    assert plan.steps[0].tool == "filesystem"
    assert plan.steps[2].tool == "sqlite"


def test_planner_creates_concrete_filesystem_arguments_for_file_task():
    result = Planner().plan(
        context(
            task_title="Create file: src/healthcheck.py",
            task_description="print('ok')",
            available_tools=["filesystem", "sqlite", "test_runner"],
            test_criteria=[
                Criterion(
                    20,
                    "tests pass",
                    command=["uv", "run", "pytest", "tests/test_healthcheck.py"],
                )
            ],
        )
    )

    plan = result.data["plan"]
    assert plan.steps[0].arguments == {"operation": "list", "path": "."}
    assert plan.steps[1].arguments == {
        "operation": "write",
        "path": "src/healthcheck.py",
        "content": "print('ok')",
    }
    assert plan.steps[2].arguments == {
        "operation": "read",
        "path": "src/healthcheck.py",
    }
    assert plan.steps[1].depends_on == ["inspect"]
    assert plan.steps[1].metadata["change_type"] == "create_or_update"
    assert plan.steps[2].metadata["evidence"] == "content_readback"
    test_step = plan.steps[-1]
    assert test_step.tool == "test_runner"
    assert test_step.arguments["command"] == [
        "uv",
        "run",
        "pytest",
        "tests/test_healthcheck.py",
    ]
    assert test_step.test_criteria == [20]


def test_planner_adds_local_git_verification_steps_when_git_is_available():
    plan = (
        Planner()
        .plan(
            context(
                task_title="Update file: README.md",
                available_tools=["filesystem", "git", "test_runner"],
                test_criteria=[
                    Criterion(20, "tests pass", command=["uv", "run", "pytest"])
                ],
            )
        )
        .data["plan"]
    )

    assert [step.id for step in plan.steps[-3:]] == [
        "git-status",
        "git-diff",
        "test-20",
    ]
    assert plan.steps[-3].arguments == {"operation": "status"}
    assert plan.steps[-2].arguments == {"operation": "diff"}
    assert plan.steps[-2].depends_on == ["git-status"]
    assert plan.steps[-1].test_criteria == [20]


@pytest.mark.parametrize(
    ("criterion", "error"),
    [
        (Criterion(20, "tests", command="pytest"), "test_command_invalid"),
        (
            Criterion(20, "tests", command=["sh", "-c", "pytest"]),
            "test_command_not_allowed",
        ),
        (
            Criterion(20, "tests", command=["uv", "run", "pytest"], timeout_seconds=0),
            "test_timeout_invalid",
        ),
        (
            Criterion(
                20,
                "tests",
                command=["uv", "run", "pytest", "../tests"],
            ),
            "test_path_invalid",
        ),
        (
            Criterion(
                20, "tests", command=["uv", "run", "pytest"], working_directory="../"
            ),
            "test_workdir_invalid",
        ),
    ],
)
def test_planner_rejects_unsafe_automated_test_criteria(criterion, error):
    result = Planner().plan(
        context(
            task_title="Create file: src/healthcheck.py",
            available_tools=["filesystem", "sqlite", "test_runner"],
            test_criteria=[criterion],
        )
    )
    assert result.status is ResultStatus.FAILED
    assert result.errors == [error]


def test_planner_requires_test_runner_for_automated_criteria():
    result = Planner().plan(
        context(
            task_title="Create file: src/healthcheck.py",
            available_tools=["filesystem", "sqlite"],
            test_criteria=[Criterion(20, "tests", command=["uv", "run", "pytest"])],
        )
    )
    assert result.errors == ["test_runner_missing"]


def test_planner_keeps_manual_criteria_out_of_test_runner_steps():
    plan = (
        Planner()
        .plan(
            context(
                task_title="Create file: src/healthcheck.py",
                available_tools=["filesystem", "sqlite"],
                test_criteria=[Criterion(20, "manual", test_type="manual")],
            )
        )
        .data["plan"]
    )
    assert all(step.tool != "test_runner" for step in plan.steps)


def test_change_request_normalizes_declarative_mapping():
    change = ChangeRequest.from_mapping(
        {
            "files": [{"path": "src/a.py", "operation": "create", "content": "x"}],
            "tests": [{"command": ["uv", "run", "pytest"], "test_criteria": [20]}],
        }
    )
    assert change.files[0].path == "src/a.py"
    assert change.tests[0].test_criteria == [20]
    with pytest.raises(TypeError, match="change must"):
        ChangeRequest.from_mapping([])
    with pytest.raises(TypeError, match="file change"):
        ChangeRequest.from_mapping({"files": ["bad"]})
    with pytest.raises(TypeError, match="test request"):
        ChangeRequest.from_mapping({"tests": [{"command": "pytest"}]})


def test_planner_builds_declarative_create_update_and_test_plan():
    result = Planner().plan(
        context(
            task_title="Implement healthcheck",
            available_tools=["filesystem", "git", "test_runner"],
            metadata={
                "change": {
                    "files": [
                        {
                            "path": "src/health.py",
                            "operation": "create",
                            "content": "ok",
                        }
                    ],
                    "tests": [
                        {"command": ["uv", "run", "pytest"], "test_criteria": [20]}
                    ],
                }
            },
        )
    )
    assert result.status is ResultStatus.SUCCESS
    plan = result.data["plan"]
    assert [step.id for step in plan.steps] == [
        "inspect",
        "read-1",
        "write-1",
        "verify-1",
        "git-diff",
        "test-1",
    ]
    assert plan.steps[2].metadata["allowed_files"] == ["src/health.py"]
    assert plan.steps[-1].test_criteria == [20]


@pytest.mark.parametrize(
    "change, error",
    [
        ({"files": []}, "change_empty"),
        (
            {"files": [{"path": "../x", "operation": "create", "content": "x"}]},
            "change_path_invalid",
        ),
        ({"files": [{"path": "x", "operation": "delete"}]}, "change_operation_invalid"),
        ({"files": [{"path": "x", "operation": "create"}]}, "change_content_missing"),
        (
            {"files": [{"path": "x", "operation": "update", "content": "x"}]},
            "change_expected_state_missing",
        ),
        (
            {
                "files": [
                    {
                        "path": "x",
                        "operation": "update",
                        "content": "x",
                        "search": "old",
                    }
                ]
            },
            "change_replacement_missing",
        ),
    ],
)
def test_planner_rejects_invalid_declarative_changes(change, error):
    result = Planner().plan(
        context(
            available_tools=["filesystem", "sqlite", "test_runner"],
            metadata={"change": change},
        )
    )
    assert result.errors == [error]


def test_planner_rejects_declared_change_without_filesystem():
    result = Planner().plan(
        context(
            available_tools=["git", "sqlite"],
            metadata={
                "change": {
                    "files": [{"path": "x", "operation": "create", "content": "x"}]
                }
            },
        )
    )
    assert result.errors == ["change_tool_missing"]


def test_planner_accepts_change_request_object_and_read_only_file():
    request = ChangeRequest(files=[FileChange("README.md", "read")])
    result = Planner().plan(
        context(available_tools=["filesystem", "sqlite"], metadata={"change": request})
    )
    assert result.status is ResultStatus.SUCCESS


def test_planner_rejects_invalid_change_metadata_and_declared_test():
    invalid_mapping = Planner().plan(context(metadata={"change": "invalid"}))
    assert invalid_mapping.errors == ["change_definition_invalid"]
    invalid_test = Planner().plan(
        context(
            available_tools=["filesystem", "sqlite", "test_runner"],
            metadata={"change": {"tests": [{"command": ["sh", "-c", "pytest"]}]}},
        )
    )
    assert invalid_test.errors == ["test_command_not_allowed"]


def test_planner_builds_replace_metadata():
    result = Planner().plan(
        context(
            available_tools=["filesystem", "sqlite"],
            metadata={
                "change": {
                    "files": [
                        {
                            "path": "src/a.py",
                            "operation": "update",
                            "content": "new",
                            "search": "old",
                            "replacement": "new",
                        }
                    ]
                }
            },
        )
    )
    assert result.status is ResultStatus.SUCCESS
    assert result.data["plan"].steps[2].metadata["search"] == "old"
    assert result.data["plan"].steps[2].metadata["replacement"] == "new"


def test_plan_step_keeps_legacy_constructor_compatible_and_supports_metadata():
    from harness.engine.plan import PlanStep

    step = PlanStep("id", "description", "action", metadata={"line_hint": [2, 4]})

    assert step.depends_on == []
    assert step.metadata == {"line_hint": [2, 4]}


def test_planner_waits_for_approval_or_external_context():
    result = Planner().plan(context(approval_status="pending"))

    assert result.status is ResultStatus.WAITING
    assert result.successful is False


def test_planner_waits_when_task_is_blocked():
    result = Planner().plan(context(task_status="waiting", approval_status="approved"))

    assert result.status is ResultStatus.WAITING


def test_planner_rejects_incomplete_context():
    missing_description = Planner().plan(context(task_description=""))
    missing_acceptance = Planner().plan(context(acceptance_criteria=[]))

    assert missing_description.status is ResultStatus.FAILED
    assert missing_description.errors == ["title_or_description_missing"]
    assert missing_acceptance.status is ResultStatus.FAILED
    assert missing_acceptance.errors == ["acceptance_criteria_missing"]


def test_planner_records_dependencies_and_previous_attempts():
    plan = (
        Planner()
        .plan(
            context(
                dependencies=[DependencyContext(2, "done", resolved=True)],
                previous_attempts=[{"status": "failed"}],
            )
        )
        .data["plan"]
    )

    assert "Task dependencies are resolved before execution." in plan.assumptions
    assert "Previous execution attempts exist and should be reviewed." in plan.risks


def test_planner_rejects_missing_implementation_tool():
    result = Planner().plan(context(available_tools=["sqlite"]))

    assert result.status is ResultStatus.FAILED
    assert result.errors == ["implementation_tool_missing"]


def test_planner_rejects_missing_validation_tool():
    result = Planner().plan(context(available_tools=["git"]))

    assert result.status is ResultStatus.FAILED
    assert result.errors == ["validation_tool_missing"]


def test_planner_waits_for_unresolved_blocking_dependency():
    result = Planner().plan(
        context(dependencies=[DependencyContext(2, "executing", resolved=False)])
    )

    assert result.status is ResultStatus.WAITING


def test_planner_versions_replans_and_uses_fallback_reason():
    result = Planner().plan(
        context(last_plan={"version": 2}, last_validation={"message": ""})
    )

    plan = result.data["plan"]
    assert plan.version == 3
    assert plan.replanned_from == 2
    assert plan.reason == "Previous validation was not successful."
