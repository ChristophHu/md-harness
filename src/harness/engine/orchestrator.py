"""Coordinate one task run through the engine components."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.config import ConfigError, ExecutionConfig, PersistenceMode
from harness.engine.context import ExecutionContext
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    FailureRecord,
    InvalidNextActionError,
    NextAction,
    ResultStatus,
    StepExecution,
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
        max_replans: int = 2,
        persistence_mode: PersistenceMode | str = PersistenceMode.OPTIONAL,
        transaction_manager: TransactionManager | None = None,
        stores: StoreBundle | None = None,
        checkpoint_store: Any | None = None,
        execution_config: ExecutionConfig | None = None,
    ) -> None:
        if execution_config is not None:
            max_cycles = execution_config.max_cycles
            max_retries = execution_config.max_retries
            max_replans = execution_config.max_replans
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
                transaction_manager,
                task_store,
                event_store,
                artifact_store,
                checkpoint_store,
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
        if max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        self.max_cycles = max_cycles
        self.max_retries = max_retries
        self.max_replans = max_replans
        self.run_id = str(uuid4())

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

    def _claim_task(self, task_id: int) -> bool | None:
        """Claim a ready task, or identify an already active concurrent run."""
        if self.task_store is None or not hasattr(self.task_store, "claim"):
            return None
        task = self.task_store.get(task_id)
        if task is None:
            return None
        if task["status"] == "ready":
            if self.transaction_manager is None:
                return self._claim_with_identity(task_id)
            with self.transaction_manager.atomic():
                return self._claim_with_identity(task_id)
        if task["status"] in {"planning", "executing", "validating"}:
            return False
        return None

    def _claim_with_identity(self, task_id: int) -> bool:
        try:
            return self.task_store.claim(task_id, run_id=self.run_id)
        except TypeError:
            return self.task_store.claim(task_id)

    def run(
        self,
        task_id: int,
        *,
        _resume_plan: Any | None = None,
        _resume_action: NextAction | None = None,
    ) -> EngineResult:
        """Run the complete pipeline once the stage components are available."""
        claimed = self._claim_task(task_id)
        if claimed is False:
            return EngineResult.waiting("Task is already claimed by another run.")
        context = self.build_context(task_id)
        if not all((self.planner, self.executor, self.validator)):
            return EngineResult.waiting(
                "Planner, executor and validator must be configured before execution."
            )

        action = _resume_action or NextAction.REPLAN
        plan = _resume_plan
        retries = 0
        replans = 0
        for _ in range(self.max_cycles):
            context = self.build_context(task_id)
            attempt_id = None
            if action is NextAction.REPLAN:
                if self.unit_of_work is not None:
                    if claimed:
                        with self.unit_of_work.phase():
                            attempt_id = self.task_store.record_attempt(
                                task_id, "running"
                            )
                            self.event_store.record(task_id, "task.planning")
                    else:
                        attempt_id = self.unit_of_work.start_cycle(
                            task_id, "planning", "task.planning"
                        )
                else:
                    attempt_id = self._start_attempt(task_id)
                    self._transition(task_id, "planning")
                    self._event(task_id, "task.planning")
                try:
                    planning = self.planner.plan(context)
                except Exception as error:  # noqa: BLE001 - stage boundary
                    return self._handle_stage_exception(
                        task_id, attempt_id, "planning", error
                    )
                if not planning.successful:
                    self._finish_attempt(attempt_id, "failed", planning.message)
                    self._fail_task(task_id, planning.message, "task.planning.failed")
                    return planning
                plan = planning.data["plan"]
                self._event(task_id, "task.plan.created", self._serialize(plan))
                if _resume_plan is None:
                    self._save_checkpoint(
                        task_id,
                        "executing",
                        NextAction.VALIDATE,
                        attempt_id,
                        "plan persisted for resumability",
                        context_data={"plan": self._serialize(plan)},
                        plan_version=getattr(plan, "version", None),
                        next_step_id=(
                            plan.steps[0].id
                            if hasattr(plan, "steps") and plan.steps
                            else None
                        ),
                    )

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
            try:
                execution_result = self.executor.execute(context, plan)
            except Exception as error:  # noqa: BLE001 - stage boundary
                return self._handle_stage_exception(
                    task_id, attempt_id, "execution", error
                )
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
                    if next_action is NextAction.REPLAN:
                        replans += 1
                        if replans > self.max_replans:
                            result = EngineResult.failure("Maximum replans exceeded.")
                            self._fail_task(
                                task_id, result.message, "task.replan.exhausted"
                            )
                            return result
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
                replans += 1
                if replans > self.max_replans:
                    result = EngineResult.failure("Maximum replans exceeded.")
                    self._finish_attempt(attempt_id, "failed", result.message)
                    self._fail_task(task_id, result.message, "task.replan.exhausted")
                    return result
                self._finish_attempt(attempt_id, "failed", execution_result.message)
                self._event(
                    task_id, "task.replanning", {"reason": "execution requested replan"}
                )
                self._record_replan(
                    task_id, plan, attempt_id, "execution_requested_replan"
                )
                action = NextAction.REPLAN
                continue

            self._transition(task_id, "validating")
            self._event(task_id, "task.validating")
            execution = execution_result.data["execution"]
            try:
                validation = self.validator.validate(context, plan, execution)
            except Exception as error:  # noqa: BLE001 - stage boundary
                return self._handle_stage_exception(
                    task_id, attempt_id, "validation", error
                )
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
                    task_id,
                    "waiting",
                    NextAction.WAIT,
                    attempt_id,
                    validation.message,
                    context_data={
                        "plan": self._serialize(plan),
                        "execution": self._serialize(execution),
                    },
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
                    replans += 1
                    if replans > self.max_replans:
                        result = EngineResult.failure("Maximum replans exceeded.")
                        self._finish_attempt(attempt_id, "failed", result.message)
                        self._fail_task(
                            task_id, result.message, "task.replan.exhausted"
                        )
                        return result
                    self._finish_attempt(attempt_id, "failed", validation.message)
                    self._event(
                        task_id,
                        "task.replanning",
                        self._replanning_payload(plan, validation),
                    )
                    self._record_replan(task_id, plan, attempt_id, "validation_failed")
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

    def _handle_stage_exception(
        self,
        task_id: int,
        attempt_id: int | None,
        phase: str,
        error: Exception,
    ) -> EngineResult:
        """Convert an unexpected stage exception into a persisted failure."""
        retryable = isinstance(error, (TimeoutError, ConnectionError)) or bool(
            getattr(error, "retryable", False)
        )
        recovery = NextAction.RETRY_EXECUTION if retryable else NextAction.REPLAN
        record = FailureRecord(
            component=phase,
            error_class=type(error).__name__,
            message=str(error) or type(error).__name__,
            retryable=retryable,
            recovery_action=recovery,
        )
        message = f"Unexpected error during {phase}: {record.message}"
        event_type = f"task.{phase}.exception"
        self._finish_attempt(attempt_id, "failed", message)
        self._event(task_id, event_type, self._serialize(record))
        self._fail_task(task_id, message, event_type)
        return EngineResult(
            status=ResultStatus.FAILED,
            message=message,
            errors=[record.error_class],
            data={"failure": record, "recovery_action": recovery.value},
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
        *,
        context_data: dict[str, Any] | None = None,
        plan_version: int | None = None,
        next_step_id: str | None = None,
        completed_step_ids: list[str] | None = None,
    ) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(
                task_id,
                phase,
                action.value,
                attempt_id=attempt_id,
                reason=reason,
                context_data=context_data,
                plan_version=plan_version,
                next_step_id=next_step_id,
                completed_step_ids=completed_step_ids,
            )

    def _record_replan(
        self, task_id: int, plan: Any, attempt_id: int | None, reason_code: str
    ) -> None:
        if self.task_store is None or not hasattr(self.task_store, "record_replan"):
            return
        version = getattr(plan, "version", 1)
        self.task_store.record_replan(
            task_id,
            reason_code,
            version + 1,
            parent_plan_version=version,
            attempt_id=attempt_id,
        )

    def resume(self, task_id: int) -> EngineResult:
        """Resume a task with a persisted waiting checkpoint."""
        checkpoint = (
            self.checkpoint_store.get(task_id) if self.checkpoint_store else None
        )
        if checkpoint is None:
            return self.run(task_id)
        try:
            next_action = checkpoint["next_action"]
        except (KeyError, IndexError):
            next_action = None
        if checkpoint["resumed_at"] is None and self.unit_of_work is not None:
            self.unit_of_work.resume(task_id, next_action)
        elif checkpoint["resumed_at"] is None:
            resumed = self.checkpoint_store.mark_resumed(task_id)
            if resumed:
                self._event(task_id, "task.resumed", {"next_action": next_action})
        plan = None
        try:
            context_data = checkpoint["context_data"]
        except (KeyError, IndexError):
            context_data = None
        if context_data:
            try:
                payload = (
                    json.loads(context_data)
                    if isinstance(context_data, str)
                    else context_data
                )
                plan = (
                    self._deserialize_plan(payload.get("plan"))
                    if isinstance(payload, dict)
                    else None
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                plan = None
        try:
            action = NextAction(checkpoint["next_action"])
        except (KeyError, ValueError):
            action = NextAction.REPLAN
        result = self._resume_from_checkpoint(task_id, checkpoint, action, plan)
        if self.checkpoint_store is not None:
            self.checkpoint_store.mark_resumed(task_id)
        return result

    def _resume_from_checkpoint(
        self, task_id: int, checkpoint: Any, action: NextAction, plan: Any | None
    ) -> EngineResult:
        """Dispatch resume deterministically by the persisted checkpoint action."""
        if action is NextAction.RETRY_EXECUTION:
            return self._resume_retry_execution(task_id, plan)
        if action is NextAction.WAIT:
            return self._resume_wait(task_id, checkpoint)
        if action is NextAction.VALIDATE:
            return self._resume_validation(task_id, plan)
        if action is NextAction.REPLAN:
            return self._resume_replan(task_id)
        return EngineResult.failure(f"Unsupported resume action: {action.value}")

    def _resume_retry_execution(self, task_id: int, plan: Any | None) -> EngineResult:
        if plan is None:
            return self._resume_replan(task_id)
        return self.run(
            task_id, _resume_plan=plan, _resume_action=NextAction.RETRY_EXECUTION
        )

    def _resume_wait(self, task_id: int, checkpoint: Any) -> EngineResult:
        """Keep unresolved external or approval blockers in ``waiting``."""
        reason = str(checkpoint["reason"] or "waiting")
        task = self.task_store.get(task_id) if self.task_store is not None else None
        if task is not None and task["approval_status"] == "approved":
            return self.run(task_id)
        if self.task_store is not None and (
            task is None or task["status"] != "waiting"
        ):
            self._transition(task_id, "waiting")
        return EngineResult.waiting(reason)

    def _resume_validation(self, task_id: int, plan: Any | None) -> EngineResult:
        """Re-enter the validation-capable workflow without inventing a plan."""
        if plan is None:
            return self._resume_replan(task_id)
        checkpoint = (
            self.checkpoint_store.get(task_id)
            if self.checkpoint_store is not None
            else None
        )
        payload = checkpoint["context_data"] if checkpoint is not None else None
        if isinstance(payload, str):
            payload = json.loads(payload)
        execution = self._deserialize_execution(
            payload.get("execution") if isinstance(payload, dict) else None
        )
        if execution is None or self.validator is None:
            return self._resume_replan(task_id)
        context = self.build_context(task_id)
        return self.validator.validate(context, plan, execution)

    def _resume_replan(self, task_id: int) -> EngineResult:
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
            return Orchestrator._serialize(asdict(value))
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: Orchestrator._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [Orchestrator._serialize(item) for item in value]
        return value

    @staticmethod
    def _deserialize_plan(value: Any) -> Any | None:
        if not isinstance(value, dict) or not isinstance(value.get("goal"), str):
            return None
        from harness.engine.plan import ExecutionPlan, PlanStep

        steps = []
        for item in value.get("steps", []):
            if not isinstance(item, dict) or "id" not in item:
                return None
            steps.append(PlanStep(**item))
        return ExecutionPlan(
            goal=value["goal"],
            steps=steps,
            assumptions=list(value.get("assumptions", [])),
            risks=list(value.get("risks", [])),
            version=value.get("version", 1),
            replanned_from=value.get("replanned_from"),
            reason=value.get("reason"),
        )

    @staticmethod
    def _deserialize_execution(value: Any) -> ExecutionResult | None:
        if not isinstance(value, dict) or "status" not in value:
            return None
        try:
            steps = [
                StepExecution(
                    step_id=item["step_id"],
                    status=ExecutionStatus(item["status"]),
                    tool=item.get("tool"),
                    arguments=dict(item.get("arguments", {})),
                    result=item.get("result"),
                    artifacts=list(item.get("artifacts", [])),
                    changed_files=list(item.get("changed_files", [])),
                    acceptance_criteria=list(item.get("acceptance_criteria", [])),
                    test_criteria=list(item.get("test_criteria", [])),
                    error=item.get("error"),
                )
                for item in value.get("steps", [])
            ]
            return ExecutionResult(
                status=ExecutionStatus(value["status"]),
                steps=steps,
                changed_files=list(value.get("changed_files", [])),
                artifacts=list(value.get("artifacts", [])),
                errors=list(value.get("errors", [])),
                next_action=(
                    NextAction(value["next_action"])
                    if value.get("next_action")
                    else None
                ),
                attempt_id=value.get("attempt_id"),
                dry_run=bool(value.get("dry_run", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None

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
