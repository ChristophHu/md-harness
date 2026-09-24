"""Coordinate one task run through the engine components."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.config import ConfigError, ExecutionConfig, HITLMode, PersistenceMode
from harness.engine.context import ExecutionContext
from harness.engine.context_builder import ContextBuilder
from harness.engine.executor import Executor
from harness.engine.hitl import plan_review_request, should_request_interaction
from harness.engine.result import (
    EngineResult,
    ExecutionResult,
    ExecutionStatus,
    FailureRecord,
    InvalidNextActionError,
    NextAction,
    ResultStatus,
    StepExecution,
    WaitReason,
)
from harness.security.approval import fingerprint, request_for_step
from harness.storage.factory import StoreBundle
from harness.storage.transaction import (
    EngineUnitOfWork,
    ExecutionOwner,
    PhaseDecision,
    TransactionError,
    TransactionManager,
    ValidationWrite,
)
from harness.tools.base import ToolRegistry


@dataclass(frozen=True, slots=True)
class _ValidationDecision:
    outcome: str
    result: EngineResult
    action: NextAction | None
    retries: int
    replans: int
    event_type: str | None = None


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
        resume_lease_seconds: int = 300,
        secret_provider: Any | None = None,
    ) -> None:
        if execution_config is not None:
            max_cycles = execution_config.max_cycles
            max_retries = execution_config.max_retries
            max_replans = execution_config.max_replans
            persistence_mode = execution_config.persistence_mode
            hitl_mode = execution_config.hitl_mode
        else:
            hitl_mode = HITLMode.MINIMAL
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
        self.hitl_mode = HITLMode(hitl_mode)
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
        self.secret_provider = secret_provider
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
        if self.hitl_mode is HITLMode.INTERACTIVE and (
            self.unit_of_work is None or self.checkpoint_store is None
        ):
            raise ConfigError(
                "interactive HITL mode requires transactional checkpoint persistence"
            )
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        self.max_cycles = max_cycles
        self.max_retries = max_retries
        self.max_replans = max_replans
        if resume_lease_seconds < 1:
            raise ValueError("resume_lease_seconds must be positive")
        self.resume_lease_seconds = resume_lease_seconds
        self.run_id = str(uuid4())
        self.decision_vault: Any | None = None

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
        context = self.context_builder.build(task_id)
        if self.transaction_manager is not None and isinstance(
            context, ExecutionContext
        ):
            context.hitl_mode = self.hitl_mode.value
            context.ownership_guard = self._check_ownership
            context.approval_checker = lambda step, tool: self._approval_allowed(
                context, step, tool
            )
            if self.unit_of_work is not None and self.checkpoint_store is not None:
                context.tool_invocation_begin = lambda plan, step, scope: (
                    self.unit_of_work.begin_tool_invocation(task_id, plan, step, scope)
                )
                context.tool_invocation_complete = lambda invocation_id, step: (
                    self.unit_of_work.complete_tool_invocation(
                        task_id, invocation_id, step
                    )
                )
                context.tool_invocation_pending = lambda: (
                    self.unit_of_work.tool_invocation_store.pending_for_task(task_id)
                )
        return context

    def _approval_allowed(
        self, context: ExecutionContext, step: Any, tool: Any
    ) -> bool:
        if self.unit_of_work is None or self.checkpoint_store is None:
            return not bool(step.metadata.get("requires_approval"))
        with self.unit_of_work.phase():
            task = self.task_store.get(context.task_id)
            if task is None or task["approval_status"] != "approved":
                return False
            checkpoint = self.checkpoint_store.get_active(context.task_id)
            if checkpoint is None or checkpoint["waiting_reason_code"] != "approval":
                return not bool(step.metadata.get("requires_approval"))
            token = checkpoint["wait_token"]
            request = self.unit_of_work.approval_store.get(token) if token else None
            if request is None:
                return False
            if request["step_id"] != step.id:
                return not bool(step.metadata.get("requires_approval"))
            if context.active_plan is None:
                return False
            expected = request_for_step(
                context.active_plan, step, tool.definition.permission.value
            )
            return self.unit_of_work.approval_store.ready(
                context.task_id, token, expected
            )

    def _check_ownership(self) -> None:
        if (
            self.transaction_manager is not None
            and self.transaction_manager.owner is not None
        ):
            with self.transaction_manager.atomic():
                pass

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
                claimed = self._claim_with_identity(task_id)
            if claimed:
                current = self.task_store.get(task_id)
                if "execution_epoch" in set(current.keys()):
                    self.transaction_manager.owner = ExecutionOwner(
                        task_id, current["claim_token"], current["execution_epoch"]
                    )
            return claimed
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
        _resume_claimed: bool = False,
        _resume_retries: int = 0,
        _resume_replans: int = 0,
        _resume_cycles: int = 0,
    ) -> EngineResult:
        """Run a pipeline while retaining and releasing its fencing identity."""
        if (
            not _resume_claimed
            and _resume_plan is None
            and self.unit_of_work is not None
            and self.checkpoint_store is not None
            and self.unit_of_work.tool_invocation_store.has_for_task(task_id)
        ):
            task = self.task_store.get(task_id)
            if (
                task is not None
                and task["status"] == "waiting"
                and self.checkpoint_store.get_active(task_id) is not None
            ):
                return self.resume(task_id)
        try:
            return self._run_pipeline(
                task_id,
                _resume_plan=_resume_plan,
                _resume_action=_resume_action,
                _resume_claimed=_resume_claimed,
                _resume_retries=_resume_retries,
                _resume_replans=_resume_replans,
                _resume_cycles=_resume_cycles,
            )
        except TransactionError:
            terminal = self._terminal_resume_result(task_id)
            if terminal is not None:
                return terminal
            return EngineResult.waiting("Task execution ownership was lost.")
        finally:
            if not _resume_claimed and self.transaction_manager is not None:
                owner = self.transaction_manager.owner
                self.transaction_manager.owner = None
                if owner is not None and owner.task_id == task_id:
                    with self.transaction_manager.atomic():
                        self.task_store.release_claim(task_id, owner.token)

    def _run_pipeline(
        self,
        task_id: int,
        *,
        _resume_plan: Any | None = None,
        _resume_action: NextAction | None = None,
        _resume_claimed: bool = False,
        _resume_retries: int = 0,
        _resume_replans: int = 0,
        _resume_cycles: int = 0,
    ) -> EngineResult:
        """Run the complete pipeline once the stage components are available."""
        claimed = None if _resume_claimed else self._claim_task(task_id)
        if claimed is False:
            return EngineResult.waiting("Task is already claimed by another run.")
        context = self.build_context(task_id)
        if not all((self.planner, self.executor, self.validator)):
            return EngineResult.waiting(
                "Planner, executor and validator must be configured before execution."
            )

        action = _resume_action or NextAction.REPLAN
        plan = _resume_plan
        retries = _resume_retries
        replans = _resume_replans
        for cycle in range(_resume_cycles, self.max_cycles):
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
                    self._check_ownership()
                    with (
                        self.transaction_manager.keep_lease_alive()
                        if self.transaction_manager is not None
                        else nullcontext()
                    ):
                        planning = self.planner.plan(context)
                    self._check_ownership()
                except TransactionError:
                    raise
                except Exception as error:  # noqa: BLE001 - stage boundary
                    return self._handle_stage_exception(
                        task_id, attempt_id, "planning", error
                    )
                if not planning.successful:
                    if (
                        planning.status is ResultStatus.WAITING
                        and planning.interaction_request
                    ):
                        try:
                            interaction_id = self._persist_planner_interaction(
                                task_id, attempt_id, planning, cycle, retries, replans
                            )
                        except (TypeError, ValueError) as error:
                            message = f"Invalid planner interaction request: {error}"
                            if self.unit_of_work is not None:
                                self.unit_of_work.commit_decision(
                                    PhaseDecision(
                                        task_id,
                                        attempt_id,
                                        "planning",
                                        (
                                            (
                                                "task.planning.invalid_interaction",
                                                {"error": str(error)},
                                            ),
                                            ("task.failed", {"error": message}),
                                        ),
                                        attempt_status="failed",
                                        attempt_error=message,
                                        task_status="failed",
                                        invalidate_checkpoint=True,
                                    )
                                )
                            return EngineResult.failure(message)
                        if interaction_id is not None:
                            return EngineResult.waiting(
                                planning.message,
                                planning.wait_reason,
                                interaction_request={"interaction_id": interaction_id},
                            )
                    if self.unit_of_work is not None and hasattr(
                        self.unit_of_work, "commit_decision"
                    ):
                        self.unit_of_work.commit_decision(
                            PhaseDecision(
                                task_id,
                                attempt_id,
                                "planning",
                                (
                                    (
                                        "task.planning.failed",
                                        {"error": planning.message},
                                    ),
                                    ("task.failed", {"error": planning.message}),
                                ),
                                attempt_status="failed",
                                attempt_error=planning.message,
                                task_status="failed",
                                invalidate_checkpoint=True,
                            )
                        )
                    else:
                        self._finish_attempt(attempt_id, "failed", planning.message)
                        self._fail_task(
                            task_id, planning.message, "task.planning.failed"
                        )
                    return planning
                plan = planning.data["plan"]
                review_request = None
                if self.hitl_mode is not HITLMode.MINIMAL and hasattr(plan, "steps"):
                    review_request = plan_review_request(plan)
                if (
                    review_request is not None
                    and self.unit_of_work is not None
                    and self.checkpoint_store is not None
                    and should_request_interaction(self.hitl_mode, review_request)
                ):
                    serialized_plan = self._serialize(plan)
                    interaction_id = self.unit_of_work.create_human_wait(
                        task_id,
                        attempt_id,
                        {
                            "phase": "planning",
                            "next_action": NextAction.WAIT.value,
                            "attempt_id": attempt_id,
                            "reason": review_request["prompt"],
                            "context_data": {
                                "plan": serialized_plan,
                                "validation_progress": {
                                    "retries": retries,
                                    "replans": replans,
                                    "cycles": cycle,
                                },
                            },
                            "plan_version": getattr(plan, "version", None),
                            "plan_fingerprint": fingerprint(serialized_plan),
                            "next_step_id": plan.steps[0].id if plan.steps else None,
                            "waiting_reason_code": WaitReason.PLAN_REVIEW.value,
                        },
                        review_request,
                        plan_version=getattr(plan, "version", None),
                        plan_fingerprint=fingerprint(serialized_plan),
                        events=(("task.plan.created", serialized_plan),),
                    )
                    return EngineResult.waiting(
                        review_request["prompt"],
                        WaitReason.PLAN_REVIEW,
                        interaction_request={"interaction_id": interaction_id},
                    )
                if self.unit_of_work is not None and hasattr(
                    self.unit_of_work, "commit_decision"
                ):
                    checkpoint = None
                    if _resume_plan is None and self.checkpoint_store is not None:
                        checkpoint = {
                            "phase": "executing",
                            "next_action": NextAction.VALIDATE.value,
                            "attempt_id": attempt_id,
                            "reason": "plan persisted for resumability",
                            "context_data": {
                                "plan": self._serialize(plan),
                                "validation_progress": {
                                    "retries": retries,
                                    "replans": replans,
                                    "cycles": cycle,
                                },
                            },
                            "plan_version": getattr(plan, "version", None),
                            "next_step_id": (
                                plan.steps[0].id
                                if hasattr(plan, "steps") and plan.steps
                                else None
                            ),
                        }
                    self.unit_of_work.commit_decision(
                        PhaseDecision(
                            task_id,
                            attempt_id,
                            "planning",
                            (("task.plan.created", self._serialize(plan)),),
                            checkpoint=checkpoint,
                        )
                    )
                else:
                    self._event(task_id, "task.plan.created", self._serialize(plan))
                if self.unit_of_work is None and _resume_plan is None:
                    self._save_checkpoint(
                        task_id,
                        "executing",
                        NextAction.VALIDATE,
                        attempt_id,
                        "plan persisted for resumability",
                        context_data={
                            "plan": self._serialize(plan),
                            "validation_progress": {
                                "retries": retries,
                                "replans": replans,
                                "cycles": cycle,
                            },
                        },
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
                            task = self.task_store.get(task_id)
                            if task is not None and task["status"] != "executing":
                                self.task_store.transition(task_id, "executing")
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
                self._check_ownership()
                with (
                    self.transaction_manager.keep_lease_alive()
                    if self.transaction_manager is not None
                    else nullcontext()
                ):
                    execution_result = self.executor.execute(context, plan)
                self._check_ownership()
            except TransactionError:
                raise
            except Exception as error:  # noqa: BLE001 - stage boundary
                return self._handle_stage_exception(
                    task_id, attempt_id, "execution", error
                )
            if self.unit_of_work is not None and hasattr(
                self.unit_of_work, "commit_decision"
            ):
                return self._finish_persistent_execution(
                    task_id,
                    attempt_id,
                    plan,
                    context,
                    execution_result,
                    retries=retries,
                    replans=replans,
                    cycle=cycle,
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
            if (
                execution_result.status is ResultStatus.WAITING
                and next_action is not NextAction.WAIT
            ):
                return self._handle_stage_exception(
                    task_id,
                    attempt_id,
                    "execution",
                    ValueError("WAITING execution requires next_action=WAIT"),
                )
            if next_action is NextAction.WAIT:
                try:
                    self._require_wait_reason(execution_result)
                except ValueError as error:
                    return self._handle_stage_exception(
                        task_id, attempt_id, "execution", error
                    )
            if execution_result.successful and next_action in {
                NextAction.WAIT,
                NextAction.STOP,
            }:
                if next_action is NextAction.WAIT:
                    self._persist_execution_wait(
                        task_id,
                        attempt_id,
                        plan,
                        execution_result,
                        retries=retries,
                        replans=replans,
                        cycle=cycle,
                    )
                    return EngineResult(
                        status=ResultStatus.WAITING,
                        message=execution_result.message,
                        errors=execution_result.errors,
                        data=execution_result.data,
                        wait_reason=execution_result.wait_reason,
                    )
                self._event(
                    task_id,
                    "task.execution.next_action",
                    {"next_action": next_action.value},
                )
                self._finish_attempt(
                    attempt_id,
                    "waiting" if next_action is NextAction.WAIT else "completed",
                )
                self._transition(task_id, "done")
                self._event(task_id, "task.done")
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
                    self._persist_execution_wait(
                        task_id,
                        attempt_id,
                        plan,
                        execution_result,
                        retries=retries,
                        replans=replans,
                        cycle=cycle,
                    )
                    return EngineResult(
                        status=ResultStatus.WAITING,
                        message=execution_result.message,
                        errors=execution_result.errors,
                        data=execution_result.data,
                        wait_reason=execution_result.wait_reason,
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
                self._check_ownership()
                with (
                    self.transaction_manager.keep_lease_alive()
                    if self.transaction_manager is not None
                    else nullcontext()
                ):
                    validation = self.validator.validate(context, plan, execution)
                self._check_ownership()
            except TransactionError:
                raise
            except Exception as error:  # noqa: BLE001 - stage boundary
                return self._handle_stage_exception(
                    task_id, attempt_id, "validation", error
                )
            decision = self._finish_validation(
                task_id,
                attempt_id,
                plan,
                execution,
                validation,
                retries=retries,
                replans=replans,
                cycle=cycle,
            )
            if decision.outcome in {"retry", "replan"}:
                retries, replans = decision.retries, decision.replans
                action = decision.action
                continue
            return decision.result

        result = EngineResult.failure("Maximum orchestration cycles exceeded.")
        self._fail_task(task_id, result.message, "task.failed")
        return result

    def _execution_checkpoint(
        self,
        task_id: int,
        attempt_id: int | None,
        plan: Any,
        result: EngineResult,
        action: NextAction,
        *,
        retries: int,
        replans: int,
        cycle: int,
    ) -> dict[str, Any]:
        execution = result.data.get("execution")
        completed = {
            step.step_id
            for step in getattr(execution, "steps", [])
            if step.status in {ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED}
        }
        steps = getattr(plan, "steps", [])
        next_step = next((step.id for step in steps if step.id not in completed), None)
        waiting = action is NextAction.WAIT
        return {
            "phase": "waiting"
            if waiting
            else "validating"
            if action is NextAction.VALIDATE
            else "executing",
            "next_action": action.value,
            "attempt_id": attempt_id,
            "reason": result.message,
            "context_data": {
                "plan": self._serialize(plan),
                "execution": self._serialize(execution),
                "tool_invocation_id": result.data.get("tool_invocation_id"),
                "validation_progress": {
                    "retries": retries,
                    "replans": replans,
                    "cycles": cycle
                    if waiting
                    else cycle + 1
                    if action in {NextAction.RETRY_EXECUTION, NextAction.REPLAN}
                    else cycle,
                },
            },
            "plan_version": getattr(plan, "version", None),
            "next_step_id": next_step
            if action in {NextAction.WAIT, NextAction.RETRY_EXECUTION}
            else None,
            "completed_step_ids": sorted(completed)
            if action in {NextAction.WAIT, NextAction.RETRY_EXECUTION}
            else [],
            "execution_result": self._serialize(result),
            "waiting_reason_code": self._require_wait_reason(result).value
            if waiting
            else None,
        }

    def _finish_persistent_execution(
        self,
        task_id: int,
        attempt_id: int,
        plan: Any,
        context: ExecutionContext,
        result: EngineResult,
        *,
        retries: int,
        replans: int,
        cycle: int,
    ) -> EngineResult:
        """Classify an executor result before committing all its effects."""
        execution = result.data.get("execution")
        error: Exception | None = None
        try:
            action = self._action(
                getattr(execution, "next_action", None)
                or result.data.get("next_action")
            )
            if result.status is ResultStatus.WAITING and action is not NextAction.WAIT:
                raise ValueError("WAITING execution requires next_action=WAIT")
            if action is NextAction.WAIT:
                self._require_wait_reason(result)
        except (InvalidNextActionError, ValueError) as caught:
            error = caught
            action = None
        if error is not None:
            message = str(error)
            error_events = (
                ("task.execution.completed", self._serialize(result)),
                ("task.execution.invalid_next_action", {"error": message}),
                ("task.failed", {"error": message}),
            )
            self.unit_of_work.commit_decision(
                PhaseDecision(
                    task_id,
                    attempt_id,
                    "executing",
                    error_events,
                    attempt_status="failed",
                    attempt_error=message,
                    task_status="failed",
                    invalidate_checkpoint=True,
                )
            )
            return EngineResult.failure(message)
        action = action or (
            NextAction.REPLAN if not result.successful else NextAction.VALIDATE
        )
        events: list[tuple[str, Any]] = [
            ("task.execution.completed", self._serialize(result)),
        ]
        if not result.successful:
            events.append(
                (
                    "task.execution.failed",
                    {
                        "error": result.message,
                        "errors": result.errors,
                    },
                )
            )
        events.append(("task.execution.next_action", {"next_action": action.value}))
        artifacts = set(getattr(execution, "artifacts", []))
        for step in getattr(execution, "steps", []):
            artifacts.update(step.artifacts)
        checkpoint = None
        attempt_status = None
        task_status = None
        attempt_error = None
        replan = None
        approval_request = None
        invalidate = False
        response = result
        if action is NextAction.WAIT:
            checkpoint = self._execution_checkpoint(
                task_id,
                attempt_id,
                plan,
                result,
                action,
                retries=retries,
                replans=replans,
                cycle=cycle,
            )
            if self.checkpoint_store is None:
                checkpoint = None
            approval_request = (
                self._approval_request(plan, execution)
                if result.wait_reason == WaitReason.APPROVAL and checkpoint is not None
                else None
            )
            if approval_request is not None and checkpoint is not None:
                checkpoint["plan_fingerprint"] = approval_request["plan_fingerprint"]
                events.append(("task.approval.requested", approval_request))
            attempt_status, task_status = "waiting", "waiting"
            attempt_error = result.message
            events.append(("task.waiting", {"reason": result.message}))
            response = EngineResult(
                ResultStatus.WAITING,
                result.message,
                result.errors,
                data=result.data,
                wait_reason=result.wait_reason,
            )
        elif result.successful and action is NextAction.STOP:
            attempt_status, task_status, invalidate = "completed", "done", True
            events.append(("task.done", None))
        elif action in {NextAction.RETRY_EXECUTION, NextAction.REPLAN}:
            retries += int(action is NextAction.RETRY_EXECUTION)
            replans += int(action is NextAction.REPLAN)
            limit_message = (
                "Maximum execution retries exceeded."
                if retries > self.max_retries
                else "Maximum replans exceeded."
                if replans > self.max_replans
                else "Maximum orchestration cycles exceeded."
                if cycle + 1 >= self.max_cycles
                else None
            )
            attempt_status, attempt_error = "failed", result.message
            if limit_message is not None:
                task_status, invalidate = "failed", True
                events.append(("task.failed", {"error": limit_message}))
                response = EngineResult.failure(limit_message)
            else:
                checkpoint = self._execution_checkpoint(
                    task_id,
                    attempt_id,
                    plan,
                    result,
                    action,
                    retries=retries,
                    replans=replans,
                    cycle=cycle,
                )
                if self.checkpoint_store is None:
                    checkpoint = None
                if action is NextAction.REPLAN:
                    version = getattr(plan, "version", 1)
                    replan = ("execution_requested_replan", version, version + 1)
                    events.append(
                        ("task.replanning", {"reason": "execution requested replan"})
                    )
                else:
                    events.append(
                        (
                            "task.retrying",
                            {
                                "retry_number": retries,
                                "max_retries": self.max_retries,
                            },
                        )
                    )
        elif not result.successful:
            attempt_status, task_status, invalidate = "failed", "failed", True
            attempt_error = result.message
            events.append(("task.failed", {"error": result.message}))
        else:
            action = NextAction.VALIDATE
            checkpoint = self._execution_checkpoint(
                task_id,
                attempt_id,
                plan,
                result,
                action,
                retries=retries,
                replans=replans,
                cycle=cycle,
            )
            if self.checkpoint_store is None:
                checkpoint = None
            task_status = "validating"
            events.append(("task.validating", None))
        self.unit_of_work.commit_decision(
            PhaseDecision(
                task_id,
                attempt_id,
                "executing",
                tuple(events),
                checkpoint=checkpoint,
                attempt_status=attempt_status,
                attempt_error=attempt_error,
                task_status=task_status,
                artifacts=tuple(sorted(artifacts)),
                replan=replan,
                approval_request=approval_request,
                invalidate_checkpoint=invalidate,
            )
        )
        if task_status is None and action in {
            NextAction.RETRY_EXECUTION,
            NextAction.REPLAN,
        }:
            return self.run(
                task_id,
                _resume_plan=plan if action is NextAction.RETRY_EXECUTION else None,
                _resume_action=action,
                _resume_claimed=True,
                _resume_retries=retries,
                _resume_replans=replans,
                _resume_cycles=cycle + 1,
            )
        if task_status != "validating":
            return response
        try:
            self._check_ownership()
            with self.transaction_manager.keep_lease_alive():
                validation = self.validator.validate(context, plan, execution)
            self._check_ownership()
        except TransactionError:
            raise
        except Exception as caught:  # noqa: BLE001 - validator boundary
            return self._handle_stage_exception(
                task_id, attempt_id, "validation", caught
            )
        decision = self._finish_validation(
            task_id,
            attempt_id,
            plan,
            execution,
            validation,
            retries=retries,
            replans=replans,
            cycle=cycle,
        )
        if decision.outcome in {"retry", "replan"}:
            return self.run(
                task_id,
                _resume_plan=plan if decision.outcome == "retry" else None,
                _resume_action=decision.action,
                _resume_claimed=True,
                _resume_retries=decision.retries,
                _resume_replans=decision.replans,
                _resume_cycles=cycle + 1,
            )
        return decision.result

    def _validation_decision(
        self,
        validation: EngineResult,
        *,
        retries: int,
        replans: int,
        cycle: int,
    ) -> _ValidationDecision:
        """Classify a validator result without changing persistent state."""
        try:
            action = self._action(validation.data.get("next_action"))
        except InvalidNextActionError as error:
            return _ValidationDecision(
                "failed",
                EngineResult(
                    ResultStatus.FAILED,
                    error.error.message,
                    errors=[error.error.message],
                    data={"error": self._serialize(error.error)},
                ),
                None,
                retries,
                replans,
                "task.validation.invalid_next_action",
            )
        if validation.status is ResultStatus.WAITING and action is not NextAction.WAIT:
            return _ValidationDecision(
                "failed",
                EngineResult.failure("WAITING validation requires next_action=WAIT"),
                None,
                retries,
                replans,
                "task.validation.invalid_next_action",
            )
        if action is NextAction.WAIT:
            try:
                self._require_wait_reason(validation)
            except ValueError as error:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure(str(error)),
                    None,
                    retries,
                    replans,
                    "task.validation.invalid_wait_reason",
                )
        if validation.status is ResultStatus.SUCCESS:
            if action not in {None, NextAction.STOP}:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure(
                        "Successful validation has conflicting next_action"
                    ),
                    None,
                    retries,
                    replans,
                    "task.validation.invalid_next_action",
                )
            return _ValidationDecision("done", validation, None, retries, replans)
        if action is NextAction.WAIT:
            return _ValidationDecision("wait", validation, action, retries, replans)
        if action is NextAction.RETRY_EXECUTION:
            retries += 1
            if retries > self.max_retries:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure("Maximum execution retries exceeded."),
                    None,
                    retries,
                    replans,
                )
            if cycle + 1 >= self.max_cycles:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure("Maximum orchestration cycles exceeded."),
                    None,
                    retries,
                    replans,
                )
            return _ValidationDecision("retry", validation, action, retries, replans)
        if action is NextAction.REPLAN:
            replans += 1
            if replans > self.max_replans:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure("Maximum replans exceeded."),
                    None,
                    retries,
                    replans,
                    "task.replan.exhausted",
                )
            if cycle + 1 >= self.max_cycles:
                return _ValidationDecision(
                    "failed",
                    EngineResult.failure("Maximum orchestration cycles exceeded."),
                    None,
                    retries,
                    replans,
                )
            return _ValidationDecision("replan", validation, action, retries, replans)
        return _ValidationDecision("failed", validation, None, retries, replans)

    def _finish_validation(
        self,
        task_id: int,
        attempt_id: int | None,
        plan: Any,
        execution: ExecutionResult,
        validation: EngineResult,
        *,
        retries: int,
        replans: int,
        cycle: int,
    ) -> _ValidationDecision:
        """Commit one complete decision before scheduling any further stage."""
        decision = self._validation_decision(
            validation, retries=retries, replans=replans, cycle=cycle
        )
        checkpoint = None
        if decision.outcome in {"wait", "retry", "replan"}:
            checkpoint = {
                "phase": "waiting" if decision.outcome == "wait" else "validating",
                "next_action": decision.action.value,
                "attempt_id": attempt_id,
                "reason": validation.message,
                "context_data": {
                    "plan": self._serialize(plan),
                    "execution": self._serialize(execution),
                    "validation_progress": {
                        "retries": decision.retries,
                        "replans": decision.replans,
                        "cycles": cycle if decision.outcome == "wait" else cycle + 1,
                    },
                },
                "plan_version": getattr(plan, "version", None),
                "execution_result": self._serialize(execution),
                "validation_result": self._serialize(validation),
                "waiting_reason_code": (
                    self._require_wait_reason(validation).value
                    if decision.outcome == "wait"
                    else None
                ),
            }
        event_type = decision.event_type
        event_payload: Any = {"error": decision.result.message}
        replan = None
        if decision.outcome == "retry":
            event_type = "task.retrying"
            event_payload = {
                "retry_number": decision.retries,
                "max_retries": self.max_retries,
            }
        elif decision.outcome == "replan":
            event_type = "task.replanning"
            event_payload = self._replanning_payload(plan, validation)
            version = getattr(plan, "version", 1)
            replan = (version, version + 1)
        write = ValidationWrite(
            task_id,
            attempt_id,
            self._serialize(validation),
            decision.outcome,
            decision.result.message,
            checkpoint,
            event_type,
            event_payload,
            replan,
        )
        if self.unit_of_work is not None:
            self.unit_of_work.validation_decision(write)
        else:
            EngineUnitOfWork.apply_validation_decision(
                self.task_store, self.event_store, self.checkpoint_store, write
            )
        return decision

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

    def _complete_done(self, task_id: int, attempt_id: int | None) -> None:
        if self.unit_of_work is not None and hasattr(
            self.unit_of_work, "complete_done"
        ):
            self.unit_of_work.complete_done(task_id, attempt_id)
            return
        self._finish_attempt(attempt_id, "completed")
        self._transition(task_id, "done")
        self._event(task_id, "task.done")
        if self.checkpoint_store is not None and hasattr(
            self.checkpoint_store, "invalidate"
        ):
            self.checkpoint_store.invalidate(task_id)

    def _fail_task(self, task_id: int, message: str, event_type: str) -> None:
        if self.unit_of_work is not None and hasattr(
            self.unit_of_work, "commit_decision"
        ):
            task = self.task_store.get(task_id)
            events = [(event_type, {"error": message})]
            if event_type != "task.failed":
                events.append(("task.failed", {"error": message, "source": event_type}))
            self.unit_of_work.commit_decision(
                PhaseDecision(
                    task_id,
                    None,
                    task["status"],
                    tuple(events),
                    task_status="failed",
                    invalidate_checkpoint=True,
                )
            )
            return
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
        if self.unit_of_work is not None and hasattr(
            self.unit_of_work, "commit_decision"
        ):
            self.unit_of_work.commit_decision(
                PhaseDecision(
                    task_id,
                    attempt_id,
                    "planning"
                    if phase == "planning"
                    else "executing"
                    if phase == "execution"
                    else "validating",
                    (
                        (event_type, self._serialize(record)),
                        ("task.failed", {"error": message, "source": event_type}),
                    ),
                    attempt_status="failed",
                    attempt_error=message,
                    task_status="failed",
                    invalidate_checkpoint=True,
                )
            )
            return EngineResult(
                status=ResultStatus.FAILED,
                message=message,
                errors=[record.error_class],
                data={"failure": record, "recovery_action": recovery.value},
            )
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

    def _persist_execution_wait(
        self,
        task_id: int,
        attempt_id: int | None,
        plan: Any,
        result: EngineResult,
        *,
        retries: int = 0,
        replans: int = 0,
        cycle: int = 0,
    ) -> None:
        """Persist execution progress before putting the task into waiting."""
        execution = result.data.get("execution")
        completed = {
            step.step_id
            for step in getattr(execution, "steps", [])
            if step.status in {ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED}
        }
        plan_steps = getattr(plan, "steps", [])
        next_step = next(
            (step.id for step in plan_steps if step.id not in completed), None
        )
        checkpoint = {
            "phase": "waiting",
            "next_action": NextAction.WAIT.value,
            "attempt_id": attempt_id,
            "reason": result.message,
            "context_data": {
                "plan": self._serialize(plan),
                "execution": self._serialize(execution),
                "validation_progress": {
                    "retries": retries,
                    "replans": replans,
                    "cycles": cycle,
                },
            },
            "plan_version": getattr(plan, "version", None),
            "next_step_id": next_step,
            "completed_step_ids": sorted(completed),
            "execution_result": self._serialize(result),
            "waiting_reason_code": self._require_wait_reason(result).value,
        }
        approval_request = (
            self._approval_request(plan, execution)
            if result.wait_reason == WaitReason.APPROVAL
            else None
        )
        if approval_request is not None:
            checkpoint["plan_fingerprint"] = approval_request["plan_fingerprint"]
        if self.unit_of_work is not None and attempt_id is not None:
            try:
                self.unit_of_work.execution_wait(
                    task_id,
                    attempt_id,
                    checkpoint,
                    execution,
                    NextAction.WAIT.value,
                    approval_request=approval_request,
                )
            except TypeError as error:
                if "approval_request" not in str(error):
                    raise
                self.unit_of_work.execution_wait(
                    task_id,
                    attempt_id,
                    checkpoint,
                    execution,
                    NextAction.WAIT.value,
                )
            return
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(task_id, **checkpoint)
        self._event(task_id, "task.execution.next_action", {"next_action": "wait"})
        self._finish_attempt(attempt_id, "waiting", result.message)
        self._transition(task_id, "waiting")
        self._event(task_id, "task.waiting", {"reason": result.message})

    @staticmethod
    def _approval_request(plan: Any, execution: Any) -> dict[str, Any] | None:
        for error in getattr(execution, "error_details", []):
            if not getattr(error, "details", {}).get("approval_required"):
                continue
            step = next(
                (
                    item
                    for item in getattr(plan, "steps", [])
                    if item.id == error.step_id
                ),
                None,
            )
            if step is not None and step.tool == error.tool:
                return request_for_step(plan, step, error.details["permission_scope"])
        return None

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
        execution_result: Any | None = None,
        validation_result: Any | None = None,
        wait_reason: WaitReason | None = None,
    ) -> None:
        if self.checkpoint_store is not None:
            checkpoint = {
                "attempt_id": attempt_id,
                "reason": reason,
                "context_data": context_data,
                "plan_version": plan_version,
                "next_step_id": next_step_id,
                "completed_step_ids": completed_step_ids,
                "execution_result": execution_result,
                "validation_result": validation_result,
                "waiting_reason_code": (
                    self._coerce_wait_reason(wait_reason).value
                    if action is NextAction.WAIT
                    else None
                ),
            }
            if self.transaction_manager is None:
                self.checkpoint_store.save(task_id, phase, action.value, **checkpoint)
            else:
                with self.transaction_manager.atomic():
                    self.checkpoint_store.save(
                        task_id, phase, action.value, **checkpoint
                    )

    @staticmethod
    def _coerce_wait_reason(value: WaitReason | str | None) -> WaitReason:
        try:
            if value is None:
                raise ValueError("missing")
            return WaitReason(value)
        except ValueError as error:
            raise ValueError(f"WAIT requires a valid WaitReason: {value}") from error

    @classmethod
    def _require_wait_reason(cls, result: EngineResult) -> WaitReason:
        return cls._coerce_wait_reason(result.wait_reason)

    def _persist_planner_interaction(
        self,
        task_id: int,
        attempt_id: int,
        result: EngineResult,
        cycle: int,
        retries: int,
        replans: int,
    ) -> str | None:
        if self.unit_of_work is None or self.checkpoint_store is None:
            return None
        request = result.interaction_request
        if not isinstance(request, dict):
            raise TypeError("interaction request must be a mapping")
        self.unit_of_work._validate_interaction_request(request)
        reason = self._require_wait_reason(result)
        expected = (
            WaitReason.PLAN_REVIEW
            if request["kind"] == "plan_review"
            else WaitReason.HUMAN_INPUT
        )
        if reason is not expected:
            raise ValueError("interaction kind does not match planner WAIT reason")
        plan = result.data.get("plan")
        execution = result.data.get("execution")
        if request["resume_action"] == NextAction.VALIDATE.value and (
            plan is None or execution is None
        ):
            raise ValueError("validation resume requires saved plan and execution")
        serialized_plan = self._serialize(plan) if plan is not None else None
        plan_fingerprint = fingerprint(serialized_plan) if serialized_plan else None
        checkpoint = {
            "phase": "planning",
            "next_action": NextAction.WAIT.value,
            "attempt_id": attempt_id,
            "reason": result.message or request["prompt"],
            "waiting_reason_code": reason.value,
            "plan_version": getattr(plan, "version", None),
            "plan_fingerprint": plan_fingerprint,
            "context_data": {
                "plan": serialized_plan,
                "execution": self._serialize(execution)
                if execution is not None
                else None,
                "validation_progress": {
                    "retries": retries,
                    "replans": replans,
                    "cycles": cycle,
                },
            },
        }
        return self.unit_of_work.create_human_wait(
            task_id,
            attempt_id,
            checkpoint,
            request,
            plan_version=getattr(plan, "version", None),
            plan_fingerprint=plan_fingerprint,
            events=(("task.planning.waiting", {"kind": request["kind"]}),),
        )

    def list_human_interactions(self, task_id: int) -> list[dict[str, Any]]:
        """Return pending interaction requests for an application/API adapter."""
        if self.unit_of_work is None:
            raise ConfigError("human interactions require transactional SQLite stores")
        with self.unit_of_work.phase():
            rows = self.unit_of_work.human_interaction_store.list_open(task_id)
            interactions = [
                {
                    "interaction_id": row["interaction_id"],
                    "task_id": row["task_id"],
                    "wait_token": row["wait_token"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "prompt": row["prompt"],
                    "response_schema": json.loads(row["response_schema"]),
                    "request_data": json.loads(row["request_data"]),
                    "plan_version": row["plan_version"],
                    "plan_fingerprint": row["plan_fingerprint"],
                }
                for row in rows
            ]
        for interaction in interactions:
            interaction["suggestions"] = self.suggest_human_interaction(
                task_id, interaction["prompt"]
            )
        return interactions

    def suggest_human_interaction(
        self, task_id: int, question: str
    ) -> list[dict[str, Any]]:
        """Suggest prior decisions from this task's project only."""
        vault = getattr(self, "decision_vault", None)
        if vault is None:
            return []
        task = self.task_store.get(task_id)
        if task is None or task["project_id"] is None:
            return []
        project = self.context_builder.project_store.get(task["project_id"])
        return vault.suggest(project["project_key"], question) if project else []

    def publish_vault_decisions(self, *, limit: int = 100) -> int:
        """Retry queued Vault publications (also useful after a restart)."""
        vault = getattr(self, "decision_vault", None)
        return vault.publish_pending(limit=limit) if vault else 0

    def resolve_tool_invocation(
        self,
        task_id: int,
        wait_token: str,
        invocation_id: str,
        actor: str,
        outcome: str,
        evidence_ref: str,
    ) -> bool:
        """Resolve an uncertain tool effect after independent inspection."""
        if self.unit_of_work is None:
            raise ConfigError(
                "tool reconciliation requires transactional SQLite stores"
            )
        return self.unit_of_work.resolve_tool_invocation(
            task_id, wait_token, invocation_id, actor, outcome, evidence_ref
        )

    def list_tool_reconciliations(self, task_id: int) -> list[dict[str, Any]]:
        """Expose only the current uncertain call and its wait token."""
        if self.unit_of_work is None or self.checkpoint_store is None:
            return []
        with self.unit_of_work.phase():
            task = self.task_store.get(task_id)
            checkpoint = self.checkpoint_store.get_active(task_id)
            if (
                task is None
                or task["status"] != "waiting"
                or checkpoint is None
                or checkpoint["waiting_reason_code"]
                != WaitReason.TOOL_OUTCOME_UNKNOWN.value
            ):
                return []
            payload = json.loads(checkpoint["context_data"] or "{}")
            invocation_id = payload.get("tool_invocation_id")
            row = (
                self.unit_of_work.tool_invocation_store.get(invocation_id)
                if invocation_id
                else None
            )
            if row is None or row["task_id"] != task_id or row["status"] != "started":
                return []
            return [
                {
                    "invocation_id": invocation_id,
                    "wait_token": checkpoint["wait_token"],
                    "step_id": row["step_id"],
                    "tool": row["tool_name"],
                    "plan_version": row["plan_version"],
                    "arguments_fingerprint": row["arguments_fingerprint"],
                    "started_at": row["started_at"],
                }
            ]

    def answer_human_interaction(
        self, task_id: int, interaction_id: str, actor: str, response: Any
    ) -> bool:
        """Submit one typed response; authentication belongs to the API adapter."""
        if self.unit_of_work is None:
            raise ConfigError("human interactions require transactional SQLite stores")
        answered = self.unit_of_work.answer_human_interaction(
            task_id, interaction_id, actor, response
        )
        if answered:
            self.publish_vault_decisions()
        return answered

    def resolve_external_wait(
        self, task_id: int, wait_token: str, actor: str, information_ref: str
    ) -> bool:
        """Persist a reference to information supplied for one waiting phase."""
        if self.unit_of_work is None:
            raise ConfigError(
                "external wait resolution requires transactional SQLite stores"
            )
        return self.unit_of_work.resolve_external_wait(
            task_id, wait_token, actor, information_ref
        )

    def approve_wait(
        self, task_id: int, wait_token: str, actor: str, scope: str
    ) -> bool:
        if self.unit_of_work is None:
            raise ConfigError("scoped approval requires transactional SQLite stores")
        return self.unit_of_work.decide_approval(
            task_id, wait_token, actor, scope, "approved"
        )

    def reject_wait(
        self, task_id: int, wait_token: str, actor: str, scope: str
    ) -> bool:
        if self.unit_of_work is None:
            raise ConfigError("scoped approval requires transactional SQLite stores")
        return self.unit_of_work.decide_approval(
            task_id, wait_token, actor, scope, "rejected"
        )

    def revoke_wait(
        self, task_id: int, wait_token: str, actor: str, scope: str
    ) -> bool:
        if self.unit_of_work is None:
            raise ConfigError("scoped approval requires transactional SQLite stores")
        return self.unit_of_work.decide_approval(
            task_id, wait_token, actor, scope, "revoked"
        )

    def _record_replan(
        self, task_id: int, plan: Any, attempt_id: int | None, reason_code: str
    ) -> None:
        if self.task_store is None or not hasattr(self.task_store, "record_replan"):
            return
        version = getattr(plan, "version", 1)
        if self.transaction_manager is None:
            self.task_store.record_replan(
                task_id,
                reason_code,
                version + 1,
                parent_plan_version=version,
                attempt_id=attempt_id,
            )
        else:
            with self.transaction_manager.atomic():
                self.task_store.record_replan(
                    task_id,
                    reason_code,
                    version + 1,
                    parent_plan_version=version,
                    attempt_id=attempt_id,
                )

    def resume(self, task_id: int) -> EngineResult:
        """Resume a task with a persisted waiting checkpoint."""
        terminal = self._terminal_resume_result(task_id)
        if terminal is not None:
            return terminal
        get_active = getattr(self.checkpoint_store, "get_active", None)
        checkpoint = (
            get_active(task_id)
            if get_active is not None
            else self.checkpoint_store.get(task_id)
            if self.checkpoint_store is not None
            else None
        )
        if checkpoint is None:
            return self.run(task_id)
        try:
            next_action = checkpoint["next_action"]
        except (KeyError, IndexError):
            next_action = None
        lease_token = str(uuid4())
        if self.unit_of_work is not None:
            claimed = self.unit_of_work.claim_resume_lease(
                task_id,
                lease_token,
                self.resume_lease_seconds,
                expected_revision=self._checkpoint_value(checkpoint, "revision"),
                expected_action=next_action,
                expected_attempt_id=self._checkpoint_value(checkpoint, "attempt_id"),
            )
        else:
            lease_claim = getattr(self.checkpoint_store, "claim_resume_lease", None)
            if lease_claim is not None:
                claimed = (
                    lease_claim(
                        task_id,
                        token=lease_token,
                        lease_seconds=self.resume_lease_seconds,
                    )
                    is not None
                )
            else:
                legacy_claim = getattr(self.checkpoint_store, "claim_resume", None)
                if legacy_claim is not None:
                    claimed = legacy_claim(task_id)
                else:
                    mark_resumed = getattr(self.checkpoint_store, "mark_resumed", None)
                    claimed = (
                        mark_resumed(task_id) if mark_resumed is not None else True
                    )
                    if claimed is None:
                        claimed = True
            if claimed:
                self._event(task_id, "task.resumed", {"next_action": next_action})
        if not claimed:
            terminal = self._terminal_resume_result(task_id)
            if terminal is not None:
                return terminal
            return EngineResult.waiting("Checkpoint is already being resumed.")
        checkpoint = (
            get_active(task_id)
            if get_active is not None
            else self.checkpoint_store.get(task_id)
        )
        if checkpoint is None:
            if self.unit_of_work is not None:
                self.unit_of_work.release_resume_lease(task_id, lease_token)
            return EngineResult.failure("Checkpoint was retired during resume claim.")
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
        try:
            if action is NextAction.VALIDATE:
                result = self._resume_validation(task_id, checkpoint, plan)
            else:
                result = self._resume_from_checkpoint(task_id, checkpoint, action, plan)
            return result
        except TransactionError:
            terminal = self._terminal_resume_result(task_id)
            if terminal is not None:
                return terminal
            return EngineResult.waiting("Task execution ownership was lost.")
        finally:
            if self.unit_of_work is not None:
                release = getattr(self.unit_of_work, "release_resume_lease", None)
                if release is not None:
                    release(task_id, lease_token)
            else:
                release = getattr(self.checkpoint_store, "release_resume_lease", None)
                if release is not None:
                    release(task_id, lease_token)

    def _terminal_resume_result(self, task_id: int) -> EngineResult | None:
        get_task = getattr(self.task_store, "get", None)
        task = get_task(task_id) if get_task is not None else None
        if task is None:
            return None
        status = task["status"]
        if status == "done":
            return EngineResult.success(
                "Task already completed.", already_completed=True
            )
        if status in {"failed", "cancelled"}:
            return EngineResult.failure(f"Task is already {status}.")
        return None

    def _resume_from_checkpoint(
        self,
        task_id: int,
        checkpoint: Any,
        action: NextAction,
        plan: Any | None,
    ) -> EngineResult:
        """Dispatch resume deterministically by the persisted checkpoint action."""
        progress = self._checkpoint_progress(checkpoint)
        if action is NextAction.RETRY_EXECUTION:
            return self._resume_retry_execution(task_id, plan, progress=progress)
        if action is NextAction.WAIT:
            return self._resume_wait(task_id, checkpoint, plan)
        if action is NextAction.VALIDATE:
            return self._resume_validation(task_id, checkpoint, plan, progress=progress)
        if action is NextAction.REPLAN:
            return self._resume_replan(task_id, progress=progress)
        return EngineResult.failure(f"Unsupported resume action: {action.value}")

    def _resume_retry_execution(
        self,
        task_id: int,
        plan: Any | None,
        *,
        progress: tuple[int, int, int] = (0, 0, 0),
    ) -> EngineResult:
        if plan is None:
            return self._resume_replan(task_id, progress=progress)
        return self.run(
            task_id,
            _resume_plan=plan,
            _resume_action=NextAction.RETRY_EXECUTION,
            _resume_claimed=True,
            _resume_retries=progress[0],
            _resume_replans=progress[1],
            _resume_cycles=progress[2],
        )

    def _resume_wait(
        self, task_id: int, checkpoint: Any, plan: Any | None = None
    ) -> EngineResult:
        """Choose a safe resume policy from the persisted blocker category."""
        reason = str(checkpoint["reason"] or "waiting")
        reason_code = self._checkpoint_value(checkpoint, "waiting_reason_code")
        reason_code = reason_code or self._waiting_reason_code(reason)
        task = self.task_store.get(task_id) if self.task_store is not None else None
        payload = self._checkpoint_value(checkpoint, "context_data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        execution = self._deserialize_execution(
            payload.get("execution") if isinstance(payload, dict) else None
        )
        progress = self._checkpoint_progress(checkpoint)
        interaction = None
        interaction_action = None
        if reason_code in {WaitReason.HUMAN_INPUT.value, WaitReason.PLAN_REVIEW.value}:
            token = self._checkpoint_value(checkpoint, "wait_token")
            if self.unit_of_work is not None and token:
                interaction = self.unit_of_work.human_interaction_store.for_wait(
                    task_id, token
                )
            if interaction is not None and interaction["status"] == "answered":
                if interaction["plan_fingerprint"]:
                    current_fingerprint = (
                        fingerprint(self._serialize(plan)) if plan else None
                    )
                    if current_fingerprint != interaction["plan_fingerprint"]:
                        return EngineResult.failure(
                            "Human interaction plan binding is stale."
                        )
                interaction_action = interaction["resume_action"]
                if interaction["kind"] == "plan_review":
                    response = json.loads(interaction["response_data"])
                    if response.get("decision") == "changes_requested":
                        return self._resume_replan(task_id, progress=progress)
                    interaction_action = NextAction.RETRY_EXECUTION.value
        if interaction_action is not None:
            if plan is None and isinstance(payload, dict):
                plan = self._deserialize_plan(payload.get("plan"))
            if interaction_action == NextAction.REPLAN.value:
                return self._resume_replan(task_id, progress=progress)
            if (
                interaction_action == NextAction.VALIDATE.value
                and execution is not None
            ):
                return self._resume_validation(
                    task_id, checkpoint, plan, progress=progress
                )
            if interaction_action == NextAction.RETRY_EXECUTION.value:
                return self._resume_retry_execution(task_id, plan, progress=progress)
        should_continue = (
            reason_code in {"temporary_error", "workspace_missing", "manual_replan"}
            or reason_code == "approval"
            and self._approval_checkpoint_ready(task_id, checkpoint, plan, payload)
            or reason_code == "external_information"
            and self._checkpoint_value(checkpoint, "external_resolved_at") is not None
            or reason_code == WaitReason.TOOL_OUTCOME_UNKNOWN.value
            and self.unit_of_work is not None
            and isinstance(payload, dict)
            and (
                invocation := self.unit_of_work.tool_invocation_store.get(
                    payload.get("tool_invocation_id", "")
                )
            )
            is not None
            and invocation["status"] in {"completed", "no_effect"}
        )
        if should_continue:
            if plan is None and isinstance(payload, dict):
                plan = self._deserialize_plan(payload.get("plan"))
            if plan is not None and execution is not None:
                return self.run(
                    task_id,
                    _resume_plan=plan,
                    _resume_action=NextAction.RETRY_EXECUTION,
                    _resume_claimed=True,
                    _resume_retries=progress[0],
                    _resume_replans=progress[1],
                    _resume_cycles=progress[2],
                )
            return self._resume_replan(task_id, progress=progress)
        if self.task_store is not None and (
            task is None or task["status"] != "waiting"
        ):
            self._transition(task_id, "waiting")
        return EngineResult.waiting(reason, WaitReason(reason_code))

    def _approval_checkpoint_ready(
        self, task_id: int, checkpoint: Any, plan: Any, payload: Any
    ) -> bool:
        if self.unit_of_work is None:
            return False
        if plan is None and isinstance(payload, dict):
            plan = self._deserialize_plan(payload.get("plan"))
        token = self._checkpoint_value(checkpoint, "wait_token")
        if not token or plan is None:
            return False
        request = self.unit_of_work.approval_store.get(token)
        if request is None:
            return False
        step = next(
            (item for item in plan.steps if item.id == request["step_id"]), None
        )
        if step is None:
            return False
        expected = request_for_step(plan, step, request["permission_scope"])
        with self.unit_of_work.phase():
            return self.unit_of_work.approval_store.ready(task_id, token, expected)

    @staticmethod
    def _checkpoint_value(checkpoint: Any, key: str) -> Any:
        try:
            return checkpoint[key]
        except (KeyError, IndexError, TypeError):
            return None

    @classmethod
    def _checkpoint_progress(cls, checkpoint: Any) -> tuple[int, int, int]:
        payload = cls._checkpoint_value(checkpoint, "context_data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return (0, 0, 0)
        data = (
            payload.get("validation_progress", {}) if isinstance(payload, dict) else {}
        )
        values = tuple(data.get(key, 0) for key in ("retries", "replans", "cycles"))
        return tuple(
            value if type(value) is int and value >= 0 else 0 for value in values
        )

    @staticmethod
    def _waiting_reason_code(reason: str) -> str:
        normalized = reason.casefold()
        if "approv" in normalized or "permission" in normalized:
            return "approval"
        if "workspace" in normalized or "work directory" in normalized:
            return "workspace_missing"
        if any(word in normalized for word in ("timeout", "temporar", "try again")):
            return "temporary_error"
        if any(word in normalized for word in ("replan", "plan manually")):
            return "manual_replan"
        return "external_information"

    def _resume_validation(
        self,
        task_id: int,
        checkpoint: Any,
        plan: Any | None = None,
        *,
        progress: tuple[int, int, int] | None = None,
    ) -> EngineResult:
        """Resume validation through the same state-decision path as a live run."""
        if isinstance(checkpoint, str) and plan is None:
            plan = checkpoint
            checkpoint = (
                self.checkpoint_store.get(task_id) if self.checkpoint_store else None
            )
        if checkpoint is None:
            checkpoint = (
                self.checkpoint_store.get(task_id) if self.checkpoint_store else None
            )
        if plan is None:
            checkpoint = (
                self.checkpoint_store.get(task_id)
                if self.checkpoint_store
                else checkpoint
            )
            payload = (
                self._checkpoint_value(checkpoint, "context_data")
                if checkpoint
                else None
            )
            if isinstance(payload, str):
                payload = json.loads(payload)
            plan = (
                self._deserialize_plan(payload.get("plan"))
                if isinstance(payload, dict)
                else None
            )
        else:
            payload = self._checkpoint_value(checkpoint, "context_data")
        if isinstance(payload, str):
            payload = json.loads(payload)
        execution = self._deserialize_execution(
            payload.get("execution") if isinstance(payload, dict) else None
        )
        if execution is None or self.validator is None:
            if (
                execution is None
                and plan is not None
                and self.unit_of_work is not None
                and self.unit_of_work.tool_invocation_store.has_for_plan(
                    task_id, fingerprint(self._serialize(plan))
                )
            ):
                return self._resume_retry_execution(
                    task_id,
                    plan,
                    progress=progress or self._checkpoint_progress(checkpoint),
                )
            return self._resume_replan(
                task_id, progress=progress or self._checkpoint_progress(checkpoint)
            )
        context = self.build_context(task_id)
        if self.unit_of_work is not None:
            attempt_id = self.unit_of_work.start_cycle(
                task_id, "validating", "task.validating"
            )
        else:
            self._transition(task_id, "validating")
            attempt_id = self._start_attempt(task_id)
            self._event(task_id, "task.validating")
        try:
            self._check_ownership()
            with (
                self.transaction_manager.keep_lease_alive()
                if self.transaction_manager is not None
                else nullcontext()
            ):
                validation = self.validator.validate(context, plan, execution)
            self._check_ownership()
        except TransactionError:
            raise
        except Exception as error:  # noqa: BLE001 - stage boundary
            return self._handle_stage_exception(
                task_id, attempt_id, "validation", error
            )
        progress = progress or self._checkpoint_progress(checkpoint)
        decision = self._finish_validation(
            task_id,
            attempt_id,
            plan,
            execution,
            validation,
            retries=progress[0],
            replans=progress[1],
            cycle=progress[2],
        )
        if decision.outcome == "retry":
            return self.run(
                task_id,
                _resume_plan=plan,
                _resume_action=NextAction.RETRY_EXECUTION,
                _resume_claimed=True,
                _resume_retries=decision.retries,
                _resume_replans=decision.replans,
                _resume_cycles=progress[2] + 1,
            )
        if decision.outcome == "replan":
            return self._resume_replan(
                task_id, progress=(decision.retries, decision.replans, progress[2] + 1)
            )
        return decision.result

    def _resume_replan(
        self, task_id: int, *, progress: tuple[int, int, int] = (0, 0, 0)
    ) -> EngineResult:
        return self.run(
            task_id,
            _resume_claimed=True,
            _resume_retries=progress[0],
            _resume_replans=progress[1],
            _resume_cycles=progress[2],
        )

    def cancel(self, task_id: int, reason: str = "Cancelled by user.") -> EngineResult:
        """Cancel a task and clean up its active execution state atomically."""
        if self.unit_of_work is not None:
            self.unit_of_work.cancel(task_id, reason)
        else:
            if self.task_store is not None and hasattr(self.task_store, "cancel"):
                self.task_store.cancel(task_id, reason)
            else:
                self._transition(task_id, "cancelled")
            if self.checkpoint_store is not None and hasattr(
                self.checkpoint_store, "invalidate"
            ):
                self.checkpoint_store.invalidate(task_id, reason)
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
