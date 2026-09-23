"""Create deterministic execution plans from task contexts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.engine.context import ChangeRequest, ExecutionContext, FileChange
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
        if isinstance(plan, EngineResult):
            return plan
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

    def _build_plan(self, context: ExecutionContext) -> ExecutionPlan | EngineResult:
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
        concrete_steps = self._concrete_file_steps(context, acceptance_ids, test_ids)
        if isinstance(concrete_steps, EngineResult):
            return concrete_steps
        declared = context.metadata.get("change")
        if declared is not None:
            try:
                change = (
                    declared
                    if isinstance(declared, ChangeRequest)
                    else ChangeRequest.from_mapping(declared)
                )
            except (TypeError, ValueError) as error:
                return EngineResult.failure(str(error), "change_definition_invalid")
            declared_steps = self._declared_steps(
                context, change, acceptance_ids, test_ids
            )
            if isinstance(declared_steps, EngineResult):
                return declared_steps
            return ExecutionPlan(
                goal=context.task_title,
                steps=declared_steps,
                assumptions=assumptions,
                risks=risks,
                version=version,
                replanned_from=previous_version,
                reason=reason,
            )
        steps = concrete_steps or [
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
        ]
        return ExecutionPlan(
            goal=context.task_title,
            steps=steps,
            assumptions=assumptions,
            risks=risks,
            version=version,
            replanned_from=previous_version,
            reason=reason,
        )

    @staticmethod
    def _declared_steps(
        context: ExecutionContext,
        change: ChangeRequest,
        acceptance_ids: list[int],
        test_ids: list[int],
    ) -> list[PlanStep] | EngineResult:
        if "filesystem" not in context.available_tools:
            return EngineResult.failure(
                "Filesystem tool is required for declared changes.",
                "change_tool_missing",
            )
        steps: list[PlanStep] = [
            PlanStep(
                "inspect",
                "Inspect workspace",
                "inspect",
                "filesystem",
                {"operation": "list", "path": "."},
            )
        ]
        previous = "inspect"
        for index, item in enumerate(change.files, 1):
            error = Planner._validate_file_change(item)
            if error is not None:
                return error
            metadata = {
                "change_type": item.operation,
                "allowed_files": [item.path],
                "evidence": "content_readback",
            }
            if item.search is not None:
                metadata["search"] = item.search
            if item.replacement is not None:
                metadata["replacement"] = item.replacement
            if item.content is not None:
                metadata["expected_content"] = item.content
            read_id = f"read-{index}"
            steps.append(
                PlanStep(
                    read_id,
                    f"Read {item.path}",
                    "inspect",
                    "filesystem",
                    {"operation": "read", "path": item.path},
                    depends_on=[previous],
                )
            )
            write_args = {
                "operation": "write",
                "path": item.path,
                "content": item.content or "",
            }
            write_id = f"write-{index}"
            steps.append(
                PlanStep(
                    write_id,
                    f"Apply {item.operation} to {item.path}",
                    "implement",
                    "filesystem",
                    write_args,
                    acceptance_criteria=item.acceptance_criteria or acceptance_ids,
                    depends_on=[read_id],
                    metadata=metadata,
                )
            )
            verify_id = f"verify-{index}"
            steps.append(
                PlanStep(
                    verify_id,
                    f"Verify {item.path}",
                    "validate",
                    "filesystem",
                    {"operation": "read", "path": item.path},
                    depends_on=[write_id],
                    metadata=metadata,
                )
            )
            previous = verify_id
        if "git" in context.available_tools:
            steps.append(
                PlanStep(
                    "git-diff",
                    "Inspect declared changes",
                    "git_diff",
                    "git",
                    {"operation": "diff"},
                    depends_on=[previous],
                    metadata={"evidence": "git_diff"},
                )
            )
            previous = "git-diff"
        for index, request in enumerate(change.tests, 1):
            criterion_ids = request.test_criteria or test_ids
            criterion = type(
                "Criterion",
                (),
                {
                    "command": request.command,
                    "timeout_seconds": request.timeout_seconds,
                    "working_directory": request.working_directory,
                    "test_type": "automated",
                },
            )()
            error = Planner._validate_test_criterion(context, criterion)
            if error is not None:
                return error
            steps.append(
                PlanStep(
                    f"test-{index}",
                    "Run declared tests",
                    "run_tests",
                    "test_runner",
                    {
                        "command": request.command,
                        "timeout": request.timeout_seconds,
                        **(
                            {"working_directory": request.working_directory}
                            if request.working_directory
                            else {}
                        ),
                    },
                    test_criteria=criterion_ids,
                    depends_on=[previous],
                )
            )
            previous = f"test-{index}"
        if not change.files and not change.tests:
            return EngineResult.failure(
                "At least one declared change or test is required.", "change_empty"
            )
        return steps

    @staticmethod
    def _validate_file_change(item: FileChange) -> EngineResult | None:
        path = Path(item.path)
        if not item.path or path.is_absolute() or ".." in path.parts:
            return EngineResult.failure(
                "Change path is outside the workspace.", "change_path_invalid"
            )
        if item.operation not in {"create", "update", "read"}:
            return EngineResult.failure(
                "Change operation is not supported.", "change_operation_invalid"
            )
        if item.operation in {"create", "update"} and item.content is None:
            return EngineResult.failure(
                "Write changes require content.", "change_content_missing"
            )
        if (
            item.operation == "update"
            and item.expected_content is None
            and item.search is None
        ):
            return EngineResult.failure(
                "Updates require expected_content or search.",
                "change_expected_state_missing",
            )
        if item.search is not None and item.replacement is None:
            return EngineResult.failure(
                "Search changes require replacement.", "change_replacement_missing"
            )
        return None

    @staticmethod
    def _concrete_file_steps(
        context: ExecutionContext, acceptance_ids: list[int], test_ids: list[int]
    ) -> list[PlanStep] | EngineResult | None:
        """Create a safe, executable plan for the MVP file-task convention."""
        match = re.fullmatch(r"(?:Create|Update) file:\s*(\S+)", context.task_title)
        if match is None or "filesystem" not in context.available_tools:
            return None
        path = match.group(1)
        steps = [
            PlanStep(
                "inspect",
                "Inspect the target workspace.",
                "inspect",
                "filesystem",
                {"operation": "list", "path": "."},
            ),
            PlanStep(
                "implement",
                context.task_description,
                "implement",
                "filesystem",
                {
                    "operation": "write",
                    "path": path,
                    "content": context.task_description,
                },
                acceptance_criteria=acceptance_ids,
                depends_on=["inspect"],
                metadata={"change_type": "create_or_update"},
            ),
            PlanStep(
                "validate",
                "Read back the created file.",
                "validate",
                "filesystem",
                {"operation": "read", "path": path},
                test_criteria=test_ids,
                depends_on=["implement"],
                metadata={"evidence": "content_readback"},
            ),
        ]
        if "git" in context.available_tools:
            steps.extend(
                [
                    PlanStep(
                        "git-status",
                        "Inspect the Git working tree after the change.",
                        "git_status",
                        "git",
                        {"operation": "status"},
                        depends_on=["validate"],
                        metadata={"evidence": "git_status"},
                    ),
                    PlanStep(
                        "git-diff",
                        "Inspect the Git diff for the change.",
                        "git_diff",
                        "git",
                        {"operation": "diff"},
                        depends_on=["git-status"],
                        metadata={"evidence": "git_diff"},
                    ),
                ]
            )
        for criterion in context.test_criteria:
            if criterion.test_type != "automated":
                continue
            error = Planner._validate_test_criterion(context, criterion)
            if error is not None:
                return error
            steps.append(
                PlanStep(
                    f"test-{criterion.id}",
                    criterion.criterion,
                    "run_tests",
                    "test_runner",
                    {
                        "command": criterion.command,
                        "timeout": criterion.timeout_seconds,
                        **(
                            {"working_directory": criterion.working_directory}
                            if criterion.working_directory
                            else {}
                        ),
                    },
                    test_criteria=[criterion.id],
                    depends_on=["implement", "validate"],
                )
            )
        return steps

    @staticmethod
    def _validate_test_criterion(
        context: ExecutionContext, criterion: Any
    ) -> EngineResult | None:
        command = criterion.command
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            return EngineResult.failure(
                "Automated test command must be a non-empty argument list.",
                "test_command_invalid",
            )
        allowed = (("uv", "run", "pytest"), ("python", "-m", "pytest"))
        if not any(tuple(command[: len(prefix)]) == prefix for prefix in allowed):
            return EngineResult.failure(
                "Test command is not allowlisted.", "test_command_not_allowed"
            )
        if criterion.timeout_seconds <= 0:
            return EngineResult.failure(
                "Test timeout must be positive.", "test_timeout_invalid"
            )
        prefix_length = 3
        for argument in command[prefix_length:]:
            if argument.startswith("-"):
                continue
            path = Path(argument)
            if path.is_absolute() or ".." in path.parts:
                return EngineResult.failure(
                    "Test path is outside the workspace.", "test_path_invalid"
                )
        workdir = criterion.working_directory or "."
        if Path(workdir).is_absolute() or ".." in Path(workdir).parts:
            return EngineResult.failure(
                "Test working directory is outside the workspace.",
                "test_workdir_invalid",
            )
        if "test_runner" not in context.available_tools:
            return EngineResult.failure(
                "Test runner is not available.", "test_runner_missing"
            )
        return None

    @staticmethod
    def _first_tool(context: ExecutionContext, *names: str) -> str | None:
        return next((name for name in names if name in context.available_tools), None)
