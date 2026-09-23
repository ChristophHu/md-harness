"""Coordinate one task run through the engine components."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from harness.config import ConfigError, ExecutionConfig, PersistenceMode
from harness.engine.context import ExecutionContext
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.result import (
    EngineResult,
    InvalidNextActionError,
    NextAction,
    ResultStatus,
)
from harness.storage.factory import StoreBundle
from harness.storage.transaction import EngineUnitOfWork, TransactionManager
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
        persistence_mode: PersistenceMode | str = PersistenceMode.OPTIONAL,
        transaction_manager: TransactionManager | None = None,
        stores: StoreBundle | None = None,
        checkpoint_store: Any | None = None,
        execution_config: ExecutionConfig | None = None,
    ) -> None:
        if execution_config is not None:
            max_cycles = execution_config.max_cycles
            max_retries = execution_config.max_retries
            persistence_mode = execution_config.persistence_mode
        if stores is not None:
            if any(
                value is not None
                for value in (
                    task_store,
                    event_store,
                    artifact_store,
                    transaction_manager,
                )
            ):
                raise ConfigError(
                    "stores bundle cannot be combined with individual stores"
                )
            task_store = stores.task_store
            event_store = stores.event_store
            artifact_store = stores.artifact_store
            checkpoint_store = stores.checkpoint_store
            self._validate_builder_stores(context_builder, stores)
            transaction_manager = stores.transaction_manager
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
        self.checkpoint_store = checkpoint_store
        if transaction_manager is not None:
            transaction_manager._validate_connections(
                (task_store, event_store, artifact_store)
            )
        self.transaction_manager = transaction_manager
        self.unit_of_work = (
            EngineUnitOfWork(
                transaction_manager, task_store, event_store, artifact_store
            )
            if transaction_manager is not None
            and task_store is not None
            and event_store is not None
            and artifact_store is not None
            else None
        )
        try:
            self.persistence_mode = PersistenceMode(persistence_mode)
        except ValueError as error:
            raise ConfigError(
                "persistence_mode must be required, optional or disabled"
            ) from error
        if self.persistence_mode is PersistenceMode.REQUIRED and not all(
            (task_store, event_store, artifact_store)
        ):
            raise ConfigError(
                "required persistence mode needs task, event and artifact stores"
            )
        if self.persistence_mode is PersistenceMode.DISABLED and stores is not None:
            raise ConfigError("disabled persistence mode cannot use a store bundle")
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.max_cycles = max_cycles
        self.max_retries = max_retries

    @staticmethod
    def _validate_builder_stores(
        context_builder: ContextBuilder, stores: StoreBundle
    ) -> None:
        expected = {
            "task_store": stores.task_store,
            "event_store": stores.event_store,
            "artifact_store": stores.artifact_store,
            "checkpoint_store": stores.checkpoint_store,
        }
        for name, store in expected.items():
            if getattr(context_builder, name, None) is not store:
                raise ConfigError(f"context builder {name} does not match store bundle")

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
            attempt_id = None
            if action is NextAction.REPLAN:
                if self.unit_of_work is not None:
                    attempt_id = self.unit_of_work.start_cycle(
                        task_id, "planning", "task.planning"
                    )
                else:
                    attempt_id = self._start_attempt(task_id)
                    self._transition(task_id, "planning")
                    self._event(task_id, "task.planning")
                planning = self.planner.plan(context)
                if not planning.successful:
                    self._finish_attempt(attempt_id, "failed", planning.message)
                    self._fail_task(task_id, planning.message, "task.planning.failed")
                    return planning
                plan = planning.data["plan"]
                self._event(task_id, "task.plan.created", self._serialize(plan))

            if self.unit_of_work is not None:
                if attempt_id is None:  # pragma: no cover - defensive invariant
                    if action is NextAction.RETRY_EXECUTION:
                        with self.unit_of_work.phase():
                            attempt_id = self.task_store.record_attempt(
                                task_id, "running"
                            )
                            self.event_store.record(task_id, "task.executing")
                    else:
                        attempt_id = self.unit_of_work.start_cycle(
                            task_id, "executing", "task.executing"
                        )
                else:
                    self._transition(task_id, "executing")
                    self._event(task_id, "task.executing")
            else:
                attempt_id = self._start_attempt(task_id)
                self._transition(task_id, "executing")
                self._event(task_id, "task.executing")
            execution_result = self.executor.execute(context, plan)
            self._persist_execution(task_id, execution_result)
            execution = execution_result.data.get("execution")
            try:
                next_action = self._action(
                    getattr(execution, "next_action", None)
                    or execution_result.data.get("next_action")
                )
            except InvalidNextActionError as error:
                return self._handle_invalid_action(
                    task_id, attempt_id, error, "task.execution.invalid_next_action"
                )
            if execution_result.successful and next_action in {
                NextAction.WAIT,
                NextAction.STOP,
            }:
                self._event(
                    task_id,
                    "task.execution.next_action",
                    {"next_action": next_action.value},
                )
                self._finish_attempt(
                    attempt_id,
                    "waiting" if next_action is NextAction.WAIT else "completed",
                )
                if next_action is NextAction.WAIT:
                    self._transition(task_id, "waiting")
                    self._event(task_id, "task.waiting")
                else:
                    self._transition(task_id, "done")
                    self._event(task_id, "task.done")
                if next_action is NextAction.WAIT:
                    return EngineResult(
                        status=ResultStatus.WAITING,
                        message=execution_result.message,
                        errors=execution_result.errors,
                        data=execution_result.data,
                    )
                return execution_result
            if not execution_result.successful:
                self._event(
                    task_id,
                    "task.execution.failed",
                    {
                        "error": execution_result.message,
                        "errors": execution_result.errors,
                    },
                )
                self._event(
                    task_id,
                    "task.execution.next_action",
                    {"next_action": next_action.value if next_action else None},
                )
                next_action = next_action or NextAction.REPLAN
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
                if next_action is NextAction.WAIT:
                    self._finish_attempt(
                        attempt_id, "waiting", execution_result.message
                    )
                    self._transition(task_id, "waiting")
                    self._event(task_id, "task.waiting")
                    return EngineResult(
                        status=ResultStatus.WAITING,
                        message=execution_result.message,
                        errors=execution_result.errors,
                        data=execution_result.data,
                    )
                if next_action is NextAction.STOP:
                    self._finish_attempt(attempt_id, "failed", execution_result.message)
                    self._fail_task(
                        task_id, execution_result.message, "task.execution.failed"
                    )
                    return execution_result
                self._finish_attempt(attempt_id, "failed", execution_result.message)
                self._fail_task(
                    task_id, execution_result.message, "task.execution.failed"
                )
                return execution_result

            self._event(
                task_id,
                "task.execution.next_action",
                {"next_action": (next_action or NextAction.VALIDATE).value},
            )
            if next_action is NextAction.RETRY_EXECUTION:
                retries += 1
                if retries > self.max_retries:
                    result = EngineResult.failure("Maximum execution retries exceeded.")
                    self._finish_attempt(attempt_id, "failed", result.message)
                    self._fail_task(task_id, result.message, "task.failed")
                    return result
                self._finish_attempt(attempt_id, "failed", execution_result.message)
                self._event(
                    task_id,
                    "task.retrying",
                    {"retry_number": retries, "max_retries": self.max_retries},
                )
                action = NextAction.RETRY_EXECUTION
                continue
            if next_action is NextAction.REPLAN:
                self._finish_attempt(attempt_id, "failed", execution_result.message)
                self._event(
                    task_id, "task.replanning", {"reason": "execution requested replan"}
                )
                action = NextAction.REPLAN
                continue

            self._transition(task_id, "validating")
            self._event(task_id, "task.validating")
            execution = execution_result.data["execution"]
            validation = self.validator.validate(context, plan, execution)
            self._event(
                task_id, "task.validation.completed", self._serialize(validation)
            )
            try:
                action = self._action(validation.data.get("next_action"))
            except InvalidNextActionError as error:
                return self._handle_invalid_action(
                    task_id, attempt_id, error, "task.validation.invalid_next_action"
                )
            if validation.status is ResultStatus.SUCCESS:
                self._finish_attempt(attempt_id, "completed")
                self._transition(task_id, "done")
                self._event(task_id, "task.done")
                return validation
            if action is NextAction.WAIT:
                self._save_checkpoint(
                    task_id, "waiting", NextAction.WAIT, attempt_id, validation.message
                )
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
            if self.transaction_manager is None:
                self.task_store.transition(task_id, status)
            else:
                with self.transaction_manager.atomic():
                    self.task_store.transition(task_id, status)

    def _event(self, task_id: int, event_type: str, payload: Any = None) -> None:
        if self.event_store is not None:
            if self.transaction_manager is None:
                self.event_store.record(task_id, event_type, payload)
            else:
                with self.transaction_manager.atomic():
                    self.event_store.record(task_id, event_type, payload)

    def _start_attempt(self, task_id: int) -> int | None:
        if self.task_store is None or not hasattr(self.task_store, "record_attempt"):
            return None
        if self.transaction_manager is None:
            return self.task_store.record_attempt(task_id, "running")
        with self.transaction_manager.atomic():
            return self.task_store.record_attempt(task_id, "running")

    def _finish_attempt(
        self, attempt_id: int | None, status: str, error: str | None = None
    ) -> None:
        if (
            attempt_id is not None
            and self.task_store is not None
            and hasattr(self.task_store, "complete_attempt")
        ):
            if self.transaction_manager is None:
                self.task_store.complete_attempt(attempt_id, status, error)
            else:
                with self.transaction_manager.atomic():
                    self.task_store.complete_attempt(attempt_id, status, error)

    def _fail_task(self, task_id: int, message: str, event_type: str) -> None:
        if self.transaction_manager is None:
            self._transition(task_id, "failed")
            self._event(task_id, event_type, {"error": message})
            if event_type != "task.failed":
                self._event(
                    task_id, "task.failed", {"error": message, "source": event_type}
                )
            return
        try:
            with self.transaction_manager.atomic():
                self.task_store.transition(task_id, "failed")
                self.event_store.record(task_id, event_type, {"error": message})
                if event_type != "task.failed":
                    self.event_store.record(
                        task_id, "task.failed", {"error": message, "source": event_type}
                    )
        except Exception:  # noqa: BLE001 - persistence fallback must handle any write failure
            self.transaction_manager.record_failure(
                self.task_store, self.event_store, task_id, message
            )

    def _handle_invalid_action(
        self,
        task_id: int,
        attempt_id: int | None,
        error: InvalidNextActionError,
        event_type: str,
    ) -> EngineResult:
        """Persist an invalid workflow action as a terminal failure."""
        self._finish_attempt(attempt_id, "failed", error.error.message)
        self._event(task_id, event_type, self._serialize(error.error))
        self._fail_task(task_id, error.error.message, event_type)
        return EngineResult(
            status=ResultStatus.FAILED,
            message=error.error.message,
            errors=[error.error.message],
            data={"error": self._serialize(error.error)},
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

    def _persist_execution(self, task_id: int, result: EngineResult) -> None:
        """Persist execution event and artifacts in one phase transaction."""
        if self.unit_of_work is None:
            self._event(task_id, "task.execution.completed", self._serialize(result))
            self._register_artifacts(task_id, result)
            return
        execution = result.data.get("execution")
        with self.unit_of_work.phase():
            self.event_store.record(
                task_id, "task.execution.completed", self._serialize(result)
            )
            if execution is not None:
                paths = set(execution.artifacts)
                for step in execution.steps:
                    paths.update(step.artifacts)
                for path in sorted(paths):
                    self.artifact_store.register(task_id, path, "execution-artifact")

    def _save_checkpoint(
        self,
        task_id: int,
        phase: str,
        action: NextAction,
        attempt_id: int | None,
        reason: str,
    ) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(
                task_id, phase, action.value, attempt_id=attempt_id, reason=reason
            )

    def resume(self, task_id: int) -> EngineResult:
        """Resume a task with a persisted waiting checkpoint."""
        checkpoint = (
            self.checkpoint_store.get(task_id) if self.checkpoint_store else None
        )
        if checkpoint is None:
            return self.run(task_id)
        if checkpoint["resumed_at"] is None:
            self.checkpoint_store.mark_resumed(task_id)
        return self.run(task_id)

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
        if isinstance(value, NextAction):
            return value
        try:
            return NextAction(value)
        except ValueError as error:
            raise InvalidNextActionError(value) from error
