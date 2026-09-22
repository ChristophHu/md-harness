"""Coordinate one task run through the engine components."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from harness.engine.context import ExecutionContext
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.result import EngineResult, NextAction, ResultStatus
from harness.tools.base import ToolRegistry


class Orchestrator:
    """Entry point connecting context construction and engine stages.

    Planner, executor and validator are injected deliberately. Their concrete
    contracts are added independently, while the orchestrator already owns
    the shared context boundary.
    """

    def __init__(
        self,
        context_builder: ContextBuilder,
        *,
        planner: Any | None = None,
        executor: Any | None = None,
        validator: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        task_store: Any | None = None,
        event_store: Any | None = None,
        artifact_store: Any | None = None,
        max_cycles: int = 3,
        max_retries: int = 2,
    ) -> None:
        self.context_builder = context_builder
        self.planner = planner
        self.executor = executor or (Executor(tool_registry) if tool_registry else None)
        if tool_registry is not None:
            builder_registry = getattr(context_builder, "tool_registry", None)
            if builder_registry is not None and builder_registry is not tool_registry:
                raise ValueError(
                    "context builder and executor must share one tool registry"
                )
            if builder_registry is None and hasattr(context_builder, "tool_registry"):
                context_builder.tool_registry = tool_registry
        self.validator = validator
        self.task_store = task_store
        self.event_store = event_store
        self.artifact_store = artifact_store
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.max_cycles = max_cycles
        self.max_retries = max_retries

    def build_context(self, task_id: int) -> ExecutionContext:
        """Build the consistent context used by all engine stages."""
        return self.context_builder.build(task_id)

    def run(self, task_id: int) -> EngineResult:
        """Run the complete pipeline once the stage components are available."""
        context = self.build_context(task_id)
        if not all((self.planner, self.executor, self.validator)):
            return EngineResult.waiting(
                "Planner, executor and validator must be configured before execution."
            )

        action = NextAction.REPLAN
        retries = 0
        for _ in range(self.max_cycles):
            context = self.build_context(task_id)
            attempt_id = self._start_attempt(task_id)
            if action is NextAction.REPLAN:
                self._transition(task_id, "planning")
                self._event(task_id, "task.planning")
                planning = self.planner.plan(context)
                if not planning.successful:
                    self._finish_attempt(attempt_id, "failed", planning.message)
                    self._fail_task(task_id, planning.message, "task.planning.failed")
                    return planning
                plan = planning.data["plan"]
                self._event(task_id, "task.plan.created", self._serialize(plan))

            self._transition(task_id, "executing")
            self._event(task_id, "task.executing")
            execution_result = self.executor.execute(context, plan)
            self._event(
                task_id, "task.execution.completed", self._serialize(execution_result)
            )
            self._register_artifacts(task_id, execution_result)
            if not execution_result.successful:
                self._event(
                    task_id,
                    "task.execution.failed",
                    {
                        "error": execution_result.message,
                        "errors": execution_result.errors,
                    },
                )
                next_action = self._action(execution_result.data.get("next_action"))
                if next_action in {NextAction.REPLAN, NextAction.RETRY_EXECUTION}:
                    if next_action is NextAction.RETRY_EXECUTION:
                        retries += 1
                        if retries > self.max_retries:
                            result = EngineResult.failure(
                                "Maximum execution retries exceeded."
                            )
                            self._finish_attempt(attempt_id, "failed", result.message)
                            self._fail_task(task_id, result.message, "task.failed")
                            return result
                    self._finish_attempt(attempt_id, "failed", execution_result.message)
                    self._event(
                        task_id,
                        "task.retrying",
                        {"retry_number": retries, "max_retries": self.max_retries},
                    )
                    action = next_action
                    continue
                self._finish_attempt(attempt_id, "failed", execution_result.message)
                self._fail_task(
                    task_id, execution_result.message, "task.execution.failed"
                )
                return execution_result

            self._transition(task_id, "validating")
            self._event(task_id, "task.validating")
            execution = execution_result.data["execution"]
            validation = self.validator.validate(context, plan, execution)
            self._event(
                task_id, "task.validation.completed", self._serialize(validation)
            )
            action = self._action(validation.data.get("next_action"))
            if validation.status is ResultStatus.SUCCESS:
                self._finish_attempt(attempt_id, "completed")
                self._transition(task_id, "done")
                self._event(task_id, "task.done")
                return validation
            if action is NextAction.WAIT:
                self._finish_attempt(attempt_id, "waiting")
                self._transition(task_id, "waiting")
                self._event(task_id, "task.waiting")
                return validation
            if action in {NextAction.REPLAN, NextAction.RETRY_EXECUTION}:
                self._event(
                    task_id,
                    "task.validation.failed",
                    {"error": validation.message, "errors": validation.errors},
                )
                if action is NextAction.RETRY_EXECUTION:
                    retries += 1
                    if retries > self.max_retries:
                        result = EngineResult.failure(
                            "Maximum execution retries exceeded."
                        )
                        self._finish_attempt(attempt_id, "failed", result.message)
                        self._fail_task(task_id, result.message, "task.failed")
                        return result
                    self._finish_attempt(attempt_id, "failed", validation.message)
                    self._event(
                        task_id,
                        "task.retrying",
                        {"retry_number": retries, "max_retries": self.max_retries},
                    )
                if action is NextAction.REPLAN:
                    self._finish_attempt(attempt_id, "failed", validation.message)
                    self._event(
                        task_id,
                        "task.replanning",
                        self._replanning_payload(plan, validation),
                    )
                continue
            self._finish_attempt(attempt_id, "failed", validation.message)
            self._event(
                task_id,
                "task.validation.failed",
                {"error": validation.message, "errors": validation.errors},
            )
            self._fail_task(task_id, validation.message, "task.validation.failed")
            return validation

        result = EngineResult.failure("Maximum orchestration cycles exceeded.")
        self._fail_task(task_id, result.message, "task.failed")
        return result

    def _transition(self, task_id: int, status: str) -> None:
        if self.task_store is not None:
            self.task_store.transition(task_id, status)

    def _event(self, task_id: int, event_type: str, payload: Any = None) -> None:
        if self.event_store is not None:
            self.event_store.record(task_id, event_type, payload)

    def _start_attempt(self, task_id: int) -> int | None:
        if self.task_store is None or not hasattr(self.task_store, "record_attempt"):
            return None
        return self.task_store.record_attempt(task_id, "running")

    def _finish_attempt(
        self, attempt_id: int | None, status: str, error: str | None = None
    ) -> None:
        if (
            attempt_id is not None
            and self.task_store is not None
            and hasattr(self.task_store, "complete_attempt")
        ):
            self.task_store.complete_attempt(attempt_id, status, error)

    def _fail_task(self, task_id: int, message: str, event_type: str) -> None:
        self._transition(task_id, "failed")
        self._event(task_id, event_type, {"error": message})
        if event_type != "task.failed":
            self._event(
                task_id, "task.failed", {"error": message, "source": event_type}
            )

    def _register_artifacts(self, task_id: int, result: EngineResult) -> None:
        """Persist artifacts reported by the executor."""
        if self.artifact_store is None:
            return
        execution = result.data.get("execution")
        if execution is None:
            return
        paths = set(execution.artifacts)
        for step in execution.steps:
            paths.update(step.artifacts)
        for path in sorted(paths):
            self.artifact_store.register(task_id, path, "execution-artifact")

    def cancel(self, task_id: int, reason: str = "Cancelled by user.") -> EngineResult:
        """Cancel a task and record the cancellation event."""
        self._transition(task_id, "cancelled")
        self._event(task_id, "task.cancelled", {"reason": reason})
        return EngineResult(
            status=ResultStatus.SUCCESS, message=reason, data={"cancelled": True}
        )

    @staticmethod
    def _serialize(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        return value

    @classmethod
    def _replanning_payload(cls, plan: Any, validation: EngineResult) -> dict[str, Any]:
        validation_data = validation.data.get("validation", {})
        if is_dataclass(validation_data):
            validation_data = asdict(validation_data)
        return {
            "reason": validation.message,
            "previous_plan_version": getattr(plan, "version", None),
            "next_plan_version": getattr(plan, "version", 0) + 1,
            "validation": validation_data,
            "errors": list(validation.errors),
        }

    @staticmethod
    def _action(value: NextAction | str | None) -> NextAction | None:
        if value is None:
            return None
        return value if isinstance(value, NextAction) else NextAction(value)
