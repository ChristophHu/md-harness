"""Transaction coordination for the SQLite-backed engine stores."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

from harness.security.approval import fingerprint, request_for_step
from harness.storage.approval_store import ApprovalStore
from harness.storage.human_interaction_store import HumanInteractionStore
from harness.storage.tool_invocation_store import ToolInvocationStore


class TransactionError(RuntimeError):
    """Raised when stores cannot participate in one transaction."""


_UNSET = object()


@dataclass(frozen=True, slots=True)
class ExecutionOwner:
    """Fencing identity for one task execution or resume."""

    task_id: int
    token: str
    epoch: int
    lease_seconds: int = 300
    resume_token: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationWrite:
    """Complete, precomputed persistence decision for one validator result."""

    task_id: int
    attempt_id: int | None
    validation: Any
    outcome: str
    message: str
    checkpoint: dict[str, Any] | None = None
    event_type: str | None = None
    event_payload: Any = None
    replan: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class PhaseDecision:
    """All persistence effects of one planner or executor decision."""

    task_id: int
    attempt_id: int | None
    expected_status: str
    events: tuple[tuple[str, Any], ...]
    checkpoint: dict[str, Any] | None = None
    attempt_status: str | None = None
    attempt_error: str | None = None
    task_status: str | None = None
    artifacts: tuple[str, ...] = ()
    replan: tuple[str, int, int] | None = None
    approval_request: dict[str, Any] | None = None
    invalidate_checkpoint: bool = False


class TransactionManager:
    """Coordinate atomic persistence blocks on one SQLite connection."""

    def __init__(self, connection: sqlite3.Connection, *stores: Any) -> None:
        self.connection = connection
        self._owner: ContextVar[ExecutionOwner | None] = ContextVar(
            "execution_owner", default=None
        )
        self._validate_connections(stores)

    @property
    def owner(self) -> ExecutionOwner | None:
        return self._owner.get()

    @owner.setter
    def owner(self, value: ExecutionOwner | None) -> None:
        self._owner.set(value)

    def assert_owner(self) -> None:
        """Renew the lease and reject a superseded or cancelled worker."""
        owner = self.owner
        if owner is None:
            return
        now = datetime.now(UTC)
        current = now.strftime("%Y-%m-%d %H:%M:%S")
        expires = (now + timedelta(seconds=owner.lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = self.connection.execute(
            """UPDATE tasks SET claim_expires_at = ?
               WHERE id = ? AND claim_token = ? AND execution_epoch = ?
               AND status NOT IN ('done', 'failed', 'cancelled')
               AND claim_expires_at >= ?""",
            (expires, owner.task_id, owner.token, owner.epoch, current),
        )
        if cursor.rowcount != 1:
            raise TransactionError("task execution claim was lost")
        if owner.resume_token is not None:
            cursor = self.connection.execute(
                """UPDATE task_checkpoints SET resume_claim_expires_at = ?
                   WHERE task_id = ? AND resume_claim_token = ?
                   AND invalidated_at IS NULL AND resume_claim_expires_at >= ?""",
                (expires, owner.task_id, owner.resume_token, current),
            )
            if cursor.rowcount != 1:
                raise TransactionError("resume checkpoint claim was lost")

    @contextmanager
    def keep_lease_alive(self) -> Iterator[None]:
        """Renew a file-backed lease while a stage can block outside SQLite."""
        owner = self.owner
        if owner is None:
            yield
            return
        database_path = self.connection.execute("PRAGMA database_list").fetchone()[2]
        if not database_path:
            yield
            return
        stop = Event()
        lost = Event()

        def renew() -> None:
            while not stop.wait(max(owner.lease_seconds / 3, 0.1)):
                connection: sqlite3.Connection | None = None
                try:
                    connection = sqlite3.connect(database_path, timeout=5)
                    now = datetime.now(UTC)
                    current = now.strftime("%Y-%m-%d %H:%M:%S")
                    expires = (now + timedelta(seconds=owner.lease_seconds)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    cursor = connection.execute(
                        """UPDATE tasks SET claim_expires_at = ? WHERE id = ?
                           AND claim_token = ? AND execution_epoch = ?
                           AND status NOT IN ('done', 'failed', 'cancelled')
                           AND claim_expires_at >= ?""",
                        (expires, owner.task_id, owner.token, owner.epoch, current),
                    )
                    if cursor.rowcount != 1:
                        lost.set()
                        return
                    if owner.resume_token is not None:
                        cursor = connection.execute(
                            """UPDATE task_checkpoints SET resume_claim_expires_at = ?
                               WHERE task_id = ? AND resume_claim_token = ?
                               AND invalidated_at IS NULL
                               AND resume_claim_expires_at >= ?""",
                            (expires, owner.task_id, owner.resume_token, current),
                        )
                        if cursor.rowcount != 1:
                            lost.set()
                            return
                    connection.commit()
                except sqlite3.Error:
                    lost.set()
                    return
                finally:
                    if connection is not None:
                        connection.close()

        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
        if lost.is_set():
            raise TransactionError("task execution lease renewal failed")

    def _validate_connections(self, stores: tuple[Any, ...]) -> None:
        for store in stores:
            if store is None:
                continue
            store_connection = getattr(store, "connection", None)
            if store_connection is not self.connection:
                raise TransactionError(
                    "all stores must share the transaction connection"
                )

    @contextmanager
    def atomic(self, *, fenced: bool = True) -> Iterator[sqlite3.Connection]:
        """Commit the block or roll it back when an exception escapes."""
        try:
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            if fenced:
                self.assert_owner()
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def record_failure(
        self, task_store: Any, event_store: Any, task_id: int, message: str
    ) -> None:
        """Persist a failure in a fresh transaction after a failed block."""
        self._validate_connections((task_store, event_store))
        try:
            with self.atomic():
                task_store.transition(task_id, "failed")
                event_store.record(
                    task_id, "task.persistence.failed", {"error": message}
                )
        except Exception as error:
            raise TransactionError("could not persist transaction failure") from error


class EngineUnitOfWork:
    """Group one engine phase into a single persistence transaction."""

    def __init__(
        self,
        manager: TransactionManager,
        task_store: Any,
        event_store: Any,
        artifact_store: Any,
        checkpoint_store: Any | None = None,
    ) -> None:
        self.manager = manager
        self.task_store = task_store
        self.event_store = event_store
        self.artifact_store = artifact_store
        self.checkpoint_store = checkpoint_store
        self.approval_store = ApprovalStore(manager.connection)
        self.human_interaction_store = HumanInteractionStore(manager.connection)
        self.tool_invocation_store = ToolInvocationStore(manager.connection)
        manager._validate_connections(
            (task_store, event_store, artifact_store, checkpoint_store)
        )

    def resume(self, task_id: int, next_action: str) -> bool:
        if self.checkpoint_store is None:
            return False
        with self.phase():
            claim = getattr(self.checkpoint_store, "claim_resume", None)
            resumed = (
                claim(task_id)
                if claim is not None
                else self.checkpoint_store.mark_resumed(task_id)
            )
            if resumed:
                self.event_store.record(
                    task_id, "task.resumed", {"next_action": next_action}
                )
            return resumed

    def claim_resume_lease(
        self,
        task_id: int,
        token: str,
        lease_seconds: int = 300,
        *,
        expected_revision: int | None = None,
        expected_action: str | None = None,
        expected_attempt_id: int | None | object = _UNSET,
    ) -> bool:
        """Acquire a resumable lease and record the resume event atomically."""
        if self.checkpoint_store is None:
            return False
        owner: ExecutionOwner | None = None
        with self.phase():
            get_task = getattr(self.task_store, "get", None)
            task = get_task(task_id) if get_task is not None else None
            if task is not None and task["status"] in {"done", "failed", "cancelled"}:
                return False
            get_active = getattr(self.checkpoint_store, "get_active", None)
            checkpoint = get_active(task_id) if get_active is not None else None
            if get_active is not None and checkpoint is None:
                return False
            if (
                checkpoint is not None
                and expected_action is not None
                and checkpoint["next_action"] != expected_action
            ):
                return False
            if (
                checkpoint is not None
                and expected_revision is not None
                and checkpoint["revision"] != expected_revision
            ):
                return False
            if (
                checkpoint is not None
                and expected_attempt_id is not _UNSET
                and checkpoint["attempt_id"] != expected_attempt_id
            ):
                return False
            if task is not None and "execution_epoch" in set(task.keys()):
                if checkpoint is None:
                    return False
                allowed = {
                    "wait": {"waiting", "executing", "validating"},
                    "validate": {"executing", "validating", "waiting"},
                    "retry_execution": {"executing", "validating"},
                    "replan": {"planning", "executing", "validating"},
                }
                if task["status"] not in allowed.get(checkpoint["next_action"], set()):
                    return False
                attempt_id = checkpoint["attempt_id"]
                if attempt_id is not None:
                    attempt = self.manager.connection.execute(
                        "SELECT task_id, status FROM task_attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                    if attempt is None or attempt["task_id"] != task_id:
                        return False
                    if (
                        checkpoint["next_action"] == "wait"
                        and attempt["status"] == "running"
                    ):
                        return False
                now = datetime.now(UTC)
                current = now.strftime("%Y-%m-%d %H:%M:%S")
                if (
                    task["status"] != "waiting"
                    and task["claim_token"] is not None
                    and task["claim_expires_at"] is not None
                    and task["claim_expires_at"] >= current
                ):
                    return False
            claim = getattr(self.checkpoint_store, "claim_resume_lease", None)
            if claim is None:
                legacy_claim = getattr(self.checkpoint_store, "claim_resume", None)
                claimed = (
                    legacy_claim(task_id)
                    if legacy_claim is not None
                    else self.checkpoint_store.mark_resumed(task_id)
                )
                if claimed:
                    self.event_store.record(
                        task_id, "task.resumed", {"claim_token": token}
                    )
                return claimed
            claimed = claim(task_id, token=token, lease_seconds=lease_seconds)
            if claimed is None:
                return False
            if task is not None and "execution_epoch" in set(task.keys()):
                if task["status"] != "waiting":
                    self.manager.connection.execute(
                        """UPDATE task_attempts SET status = 'failed',
                           completed_at = CURRENT_TIMESTAMP,
                           error_message = 'resume lease takeover'
                           WHERE task_id = ? AND status = 'running'
                           AND completed_at IS NULL""",
                        (task_id,),
                    )
                expires = (
                    datetime.now(UTC) + timedelta(seconds=lease_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
                cursor = self.manager.connection.execute(
                    """UPDATE tasks SET claim_token = ?, claimed_at = CURRENT_TIMESTAMP,
                       claim_expires_at = ?, execution_epoch = execution_epoch + 1
                       WHERE id = ? AND execution_epoch = ?""",
                    (token, expires, task_id, task["execution_epoch"]),
                )
                if cursor.rowcount != 1:
                    raise TransactionError("task changed during resume claim")
                owner = ExecutionOwner(
                    task_id, token, task["execution_epoch"] + 1, lease_seconds, token
                )
            self.event_store.record(task_id, "task.resumed", {"claim_token": token})
        self.manager.owner = owner
        return True

    def claim_orphaned_run(
        self, task_id: int, token: str, lease_seconds: int = 300
    ) -> bool:
        """Take over an expired active run that has no checkpoint or tool effects."""
        if self.checkpoint_store is None or lease_seconds < 1:
            return False
        owner: ExecutionOwner | None = None
        with self.manager.atomic(fenced=False):
            task = self.task_store.get(task_id)
            if task is None or task["status"] not in {
                "planning",
                "executing",
                "validating",
            }:
                return False
            get_active = getattr(self.checkpoint_store, "get_active", None)
            if get_active is None or get_active(task_id) is not None:
                return False
            # Without the persisted plan, a tool journal cannot prove safe progress.
            if self.tool_invocation_store.has_for_task(task_id):
                return False
            now = datetime.now(UTC)
            current = now.strftime("%Y-%m-%d %H:%M:%S")
            if (
                task["claim_token"] is not None
                and task["claim_expires_at"] is not None
                and task["claim_expires_at"] >= current
            ):
                return False
            expires = (now + timedelta(seconds=lease_seconds)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            columns = set(task.keys())
            if "execution_epoch" not in columns:
                return False
            epoch = int(task["execution_epoch"])
            cursor = self.manager.connection.execute(
                """UPDATE tasks SET claim_token = ?, claimed_at = CURRENT_TIMESTAMP,
                   claim_expires_at = ?, execution_epoch = execution_epoch + 1
                   WHERE id = ? AND execution_epoch = ?
                   AND status IN ('planning', 'executing', 'validating')
                   AND (claim_token IS NULL OR claim_expires_at IS NULL
                        OR claim_expires_at < ?)""",
                (token, expires, task_id, epoch, current),
            )
            if cursor.rowcount != 1:
                return False
            interrupted = self.manager.connection.execute(
                """UPDATE task_attempts SET status = 'failed',
                   completed_at = CURRENT_TIMESTAMP,
                   error_message = 'process interrupted before checkpoint'
                   WHERE task_id = ? AND status = 'running'
                   AND completed_at IS NULL""",
                (task_id,),
            ).rowcount
            self.event_store.record(
                task_id,
                "task.recovery.claimed",
                {
                    "previous_status": task["status"],
                    "interrupted_attempts": interrupted,
                },
            )
            owner = ExecutionOwner(task_id, token, epoch + 1, lease_seconds)
        self.manager.owner = owner
        return True

    def release_resume_lease(self, task_id: int, token: str) -> bool:
        """Release a lease after the resumed workflow returns."""
        if self.checkpoint_store is None:
            return False
        owner = self.manager.owner
        if (
            owner is not None
            and owner.task_id == task_id
            and owner.resume_token == token
        ):
            self.manager.owner = None
        with self.phase():
            released = self.checkpoint_store.release_resume_lease(task_id, token)
            if owner is not None and owner.task_id == task_id and owner.token == token:
                release_claim = getattr(self.task_store, "release_claim", None)
                if release_claim is not None:
                    release_claim(task_id, token)
            return released

    def resolve_external_wait(
        self, task_id: int, wait_token: str, actor: str, information_ref: str
    ) -> bool:
        """Resolve one external wait and write its audit event in one transaction.

        Returns False for an idempotent repeat with the same reference.
        """
        if not actor.strip() or not information_ref.strip() or not wait_token.strip():
            raise ValueError("wait_token, actor and information_ref must be non-empty")
        if self.checkpoint_store is None:
            raise TransactionError("checkpoint store is required")
        with self.phase():
            if self.checkpoint_store.resolve_external_wait(
                task_id, wait_token, actor, information_ref
            ):
                self.event_store.record(
                    task_id,
                    "task.external_wait.resolved",
                    {
                        "wait_token": wait_token,
                        "actor": actor,
                        "information_ref": information_ref,
                    },
                )
                return True
            task = self.task_store.get(task_id)
            checkpoint = self.checkpoint_store.get_active(task_id)
            if task is None or task["status"] != "waiting" or checkpoint is None:
                raise ValueError("task has no active waiting checkpoint")
            if checkpoint["wait_token"] != wait_token:
                raise ValueError("stale wait token")
            if checkpoint["waiting_reason_code"] != "external_information":
                raise ValueError("checkpoint is not waiting for external information")
            if checkpoint["external_resolved_at"] is not None:
                if checkpoint["information_ref"] == information_ref:
                    return False
                raise ValueError("external wait was resolved with another reference")
            raise ValueError("external wait could not be resolved")

    def execution_wait(
        self,
        task_id: int,
        attempt_id: int,
        checkpoint: dict[str, Any],
        execution: Any,
        next_action: str,
        approval_request: dict[str, Any] | None = None,
    ) -> None:
        """Persist an execution wait checkpoint and waiting decision atomically."""
        with self.phase():
            self.checkpoint_store.save(task_id, **checkpoint)
            if approval_request is not None:
                current = self.checkpoint_store.get_active(task_id)
                self.approval_store.create(
                    task_id, current["wait_token"], approval_request
                )
                self.event_store.record(
                    task_id, "task.approval.requested", approval_request
                )
            self.event_store.record(
                task_id,
                "task.execution.next_action",
                {"next_action": next_action},
            )
            self.task_store.complete_attempt(
                attempt_id, "waiting", checkpoint["reason"]
            )
            self.task_store.transition(task_id, "waiting")
            self.event_store.record(
                task_id, "task.waiting", {"reason": checkpoint["reason"]}
            )

    def commit_decision(self, decision: PhaseDecision) -> None:
        """Persist an entire planner/executor decision or nothing at all."""
        if decision.checkpoint is not None and self.checkpoint_store is None:
            raise TransactionError("decision requires checkpoint store")
        with self.phase() as connection:
            task = self.task_store.get(decision.task_id)
            if task is None or task["status"] != decision.expected_status:
                raise TransactionError("decision task state changed")
            if decision.attempt_id is not None:
                attempt = connection.execute(
                    "SELECT task_id, status FROM task_attempts WHERE id = ?",
                    (decision.attempt_id,),
                ).fetchone()
                if (
                    attempt is None
                    or attempt["task_id"] != decision.task_id
                    or attempt["status"] != "running"
                ):
                    raise TransactionError("decision attempt is not active")
            if decision.checkpoint is not None:
                self.checkpoint_store.save(decision.task_id, **decision.checkpoint)
                if decision.approval_request is not None:
                    checkpoint = self.checkpoint_store.get_active(decision.task_id)
                    self.approval_store.create(
                        decision.task_id,
                        checkpoint["wait_token"],
                        decision.approval_request,
                    )
            for path in decision.artifacts:
                self.artifact_store.register(
                    decision.task_id, path, "execution-artifact"
                )
            if decision.attempt_status is not None and decision.attempt_id is not None:
                self.task_store.complete_attempt(
                    decision.attempt_id,
                    decision.attempt_status,
                    decision.attempt_error,
                )
            if decision.replan is not None:
                reason, parent_version, version = decision.replan
                self.task_store.record_replan(
                    decision.task_id,
                    reason,
                    version,
                    parent_plan_version=parent_version,
                    attempt_id=decision.attempt_id,
                )
            if decision.task_status is not None:
                self.task_store.transition(decision.task_id, decision.task_status)
            for event_type, payload in decision.events:
                self.event_store.record(decision.task_id, event_type, payload)
            if decision.invalidate_checkpoint and self.checkpoint_store is not None:
                self.checkpoint_store.invalidate(decision.task_id)

    def decide_approval(
        self, task_id: int, wait_token: str, actor: str, scope: str, status: str
    ) -> bool:
        """Apply a scoped human decision and its audit event atomically."""
        with self.manager.atomic(fenced=False):
            changed = self.approval_store.decide(
                task_id, wait_token, actor, scope, status
            )
            if changed:
                self.event_store.record(
                    task_id,
                    f"task.approval.{status}",
                    {"wait_token": wait_token, "actor": actor, "scope": scope},
                )
        return changed

    def create_human_wait(
        self,
        task_id: int,
        attempt_id: int,
        checkpoint: dict[str, Any],
        request: dict[str, Any],
        *,
        plan_version: int | None = None,
        plan_fingerprint: str | None = None,
        events: tuple[tuple[str, Any], ...] = (),
        artifacts: tuple[str, ...] = (),
    ) -> str:
        """Atomically create an interaction and move its task into waiting."""
        self._validate_interaction_request(request)
        with self.phase():
            task = self.task_store.get(task_id)
            if task is None or task["status"] not in {
                "planning",
                "executing",
                "validating",
            }:
                raise TransactionError("task is not in an interactable state")
            attempt = self.manager.connection.execute(
                "SELECT task_id, status FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if (
                attempt is None
                or attempt["task_id"] != task_id
                or attempt["status"] != "running"
            ):
                raise TransactionError("interaction attempt is not active")
            self.checkpoint_store.save(task_id, **checkpoint)
            active = self.checkpoint_store.get_active(task_id)
            self.manager.connection.execute(
                """UPDATE task_human_interactions SET status = 'superseded',
                   decided_at = CURRENT_TIMESTAMP
                   WHERE task_id = ? AND status = 'pending' AND wait_token != ?""",
                (task_id, active["wait_token"]),
            )
            interaction_id = self.human_interaction_store.create(
                task_id,
                active["wait_token"],
                request,
                plan_version=plan_version,
                plan_fingerprint=plan_fingerprint,
            )
            for path in artifacts:
                self.artifact_store.register(task_id, path, "execution-artifact")
            for event_type, payload in events:
                self.event_store.record(task_id, event_type, payload)
            self.task_store.complete_attempt(
                attempt_id, "waiting", checkpoint["reason"]
            )
            self.task_store.transition(task_id, "waiting")
            self.event_store.record(
                task_id,
                "task.interaction.requested",
                {
                    "interaction_id": interaction_id,
                    "wait_token": active["wait_token"],
                    "kind": request["kind"],
                    "prompt": request["prompt"],
                },
            )
            self.event_store.record(
                task_id, "task.waiting", {"reason": checkpoint["reason"]}
            )
            return interaction_id

    def answer_human_interaction(
        self, task_id: int, interaction_id: str, actor: str, response: Any
    ) -> bool:
        """Validate and persist one answer against the current waiting token."""
        if not actor.strip():
            raise ValueError("actor is required")
        with self.phase():
            task = self.task_store.get(task_id)
            row = self.human_interaction_store.get(interaction_id)
            checkpoint = self.checkpoint_store.get_active(task_id)
            if row is None or row["task_id"] != task_id:
                raise ValueError("human interaction not found for task")
            if task is None or task["status"] != "waiting" or checkpoint is None:
                raise ValueError("task has no active waiting interaction")
            if checkpoint["wait_token"] != row["wait_token"]:
                raise ValueError("stale human interaction")
            expected_reason = (
                "plan_review" if row["kind"] == "plan_review" else "human_input"
            )
            if checkpoint["waiting_reason_code"] != expected_reason:
                raise ValueError("interaction does not match the active wait reason")
            encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
            if row["status"] == "answered":
                if row["response_data"] == encoded:
                    return False
                raise ValueError("interaction already has a different answer")
            if row["status"] != "pending":
                raise ValueError("interaction is no longer pending")
            schema = json.loads(row["response_schema"])
            self.human_interaction_store.validate_response(schema, response)
            cursor = self.manager.connection.execute(
                """UPDATE task_human_interactions SET status = 'answered', response_data = ?,
                   decided_by = ?, decided_at = CURRENT_TIMESTAMP
                   WHERE interaction_id = ? AND status = 'pending'""",
                (encoded, actor, interaction_id),
            )
            if cursor.rowcount != 1:
                raise TransactionError("human interaction answer lost a race")
            self.event_store.record(
                task_id,
                "task.interaction.answered",
                {
                    "interaction_id": interaction_id,
                    "wait_token": row["wait_token"],
                    "kind": row["kind"],
                    "actor": actor,
                },
            )
            if row["kind"] in {"human_decision", "plan_review"} or (
                row["kind"] == "information_request"
                and json.loads(row["request_data"]).get("reusable") is True
            ):
                self.manager.connection.execute(
                    """INSERT INTO vault_decision_outbox (interaction_id, project_key)
                       SELECT ?, p.project_key FROM tasks t
                       JOIN projects p ON p.id = t.project_id
                       WHERE t.id = ? AND p.project_key IS NOT NULL""",
                    (interaction_id, task_id),
                )
            return True

    def begin_tool_invocation(
        self, task_id: int, plan: Any, step: Any, permission_scope: str
    ) -> tuple[str, Any]:
        """Durably reserve an effectful step before its external call."""
        binding = request_for_step(plan, step, permission_scope)
        binding["tool_name"] = binding.pop("tool")
        with self.phase():
            task = self.task_store.get(task_id)
            if task is None or task["status"] != "executing":
                raise TransactionError("tool invocation task is not executing")
            state, value = self.tool_invocation_store.reserve(task_id, binding)
            if state == "new":
                self.event_store.record(
                    task_id,
                    "task.tool.started",
                    {"invocation_id": value, "step_id": step.id, "tool": step.tool},
                )
            return state, value

    def complete_tool_invocation(
        self, task_id: int, invocation_id: str, step: Any
    ) -> None:
        """Persist the verified step result before execution can move on."""
        result = {
            "step_id": step.step_id,
            "status": step.status.value,
            "tool": step.tool,
            "artifacts": list(step.artifacts),
            "changed_files": list(step.changed_files),
            "acceptance_criteria": list(step.acceptance_criteria),
            "test_criteria": list(step.test_criteria),
        }
        with self.phase():
            if not self.tool_invocation_store.complete(task_id, invocation_id, result):
                raise TransactionError("tool invocation completion lost ownership")
            self.event_store.record(
                task_id,
                "task.tool.completed",
                {"invocation_id": invocation_id, "step_id": step.step_id},
            )

    def resolve_tool_invocation(
        self,
        task_id: int,
        wait_token: str,
        invocation_id: str,
        actor: str,
        outcome: str,
        evidence_ref: str,
    ) -> bool:
        """Audit a human-verified effect or a verified absence of effect."""
        if not wait_token.strip() or not actor.strip() or not evidence_ref.strip():
            raise ValueError("wait_token, actor and evidence_ref are required")
        if outcome not in {"completed", "no_effect"}:
            raise ValueError("outcome must be completed or no_effect")
        with self.manager.atomic(fenced=False):
            task = self.task_store.get(task_id)
            checkpoint = self.checkpoint_store.get_active(task_id)
            if task is None or task["status"] != "waiting" or checkpoint is None:
                raise ValueError("task is not waiting for tool reconciliation")
            if checkpoint["waiting_reason_code"] != "tool_outcome_unknown":
                raise ValueError("checkpoint is not a tool reconciliation wait")
            if checkpoint["wait_token"] != wait_token:
                raise ValueError("stale tool reconciliation token")
            payload = json.loads(checkpoint["context_data"] or "{}")
            if payload.get("tool_invocation_id") != invocation_id:
                raise ValueError("stale tool invocation")
            row = self.tool_invocation_store.get(invocation_id)
            if row is None or row["task_id"] != task_id:
                raise ValueError("tool invocation not found for task")
            if row["status"] != "started":
                if (
                    row["status"] == outcome
                    and row["resolved_by"] == actor
                    and row["evidence_ref"] == evidence_ref
                ):
                    return False
                raise ValueError("tool invocation already has another resolution")
            plan = payload.get("plan")
            if (
                not isinstance(plan, dict)
                or fingerprint(plan) != row["plan_fingerprint"]
            ):
                raise ValueError("tool invocation plan binding is stale")
            step = next(
                (
                    item
                    for item in plan.get("steps", [])
                    if item.get("id") == row["step_id"]
                ),
                None,
            )
            if step is None or step.get("tool") != row["tool_name"]:
                raise ValueError("tool invocation step binding is stale")
            result = (
                {
                    "step_id": row["step_id"],
                    "status": "success",
                    "tool": row["tool_name"],
                    "artifacts": [],
                    "changed_files": [],
                    "acceptance_criteria": step.get("acceptance_criteria", []),
                    "test_criteria": step.get("test_criteria", []),
                }
                if outcome == "completed"
                else None
            )
            if not self.tool_invocation_store.resolve(
                task_id, invocation_id, actor, outcome, evidence_ref, result
            ):
                raise TransactionError("tool invocation resolution lost a race")
            self.event_store.record(
                task_id,
                "task.tool.reconciled",
                {
                    "invocation_id": invocation_id,
                    "outcome": outcome,
                    "actor": actor,
                    "evidence_ref": evidence_ref,
                },
            )
            return True

    @staticmethod
    def _validate_interaction_request(request: dict[str, Any]) -> None:
        if not isinstance(request, dict):
            raise TypeError("interaction request must be a mapping")
        if request.get("kind") not in {
            "information_request",
            "human_decision",
            "plan_review",
        }:
            raise ValueError("unsupported human interaction kind")
        if not isinstance(request.get("prompt"), str) or not request["prompt"].strip():
            raise ValueError("interaction prompt is required")
        if request.get("resume_action") not in {
            "retry_execution",
            "replan",
            "validate",
        }:
            raise ValueError("invalid interaction resume_action")
        schema = request.get("response_schema")
        if not isinstance(schema, dict) or schema.get("type") not in {
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        }:
            raise ValueError(
                "interaction response_schema must declare a supported type"
            )
        HumanInteractionStore.validate_schema(schema)
        if not isinstance(request.get("request_data", {}), dict):
            raise TypeError("interaction request_data must be an object")

    @contextmanager
    def phase(self) -> Iterator[sqlite3.Connection]:
        """Commit all writes in the phase or roll them back together."""
        with self.manager.atomic() as connection:
            yield connection

    def cycle_start(self, task_id: int, status: str, event_type: str) -> None:
        with self.phase():
            self.task_store.transition(task_id, status)
            self.task_store.record_attempt(task_id, "running")
            self.event_store.record(task_id, event_type)

    def start_cycle(self, task_id: int, status: str, event_type: str) -> int:
        """Atomically create the attempt and persist the cycle start."""
        with self.phase():
            self.task_store.transition(task_id, status)
            attempt_id = self.task_store.record_attempt(task_id, "running")
            self.event_store.record(task_id, event_type)
            return attempt_id

    def execution_result(self, task_id: int, attempt_id: int, result: Any) -> None:
        with self.phase():
            self.event_store.record(task_id, "task.execution.completed", result)
            for path in sorted(set(result.artifacts)):
                self.artifact_store.register(task_id, path, "execution-artifact")
            self.task_store.complete_attempt(attempt_id, "completed")

    def decision(
        self, task_id: int, status: str, event_type: str, payload: Any = None
    ) -> None:
        with self.phase():
            self.task_store.transition(task_id, status)
            self.event_store.record(task_id, event_type, payload)

    def finish_decision(
        self,
        task_id: int,
        attempt_id: int,
        attempt_status: str,
        status: str,
        event_type: str,
        payload: Any = None,
    ) -> None:
        """Atomically finish an attempt, transition the task and record an event."""
        with self.phase():
            self.task_store.complete_attempt(attempt_id, attempt_status)
            self.task_store.transition(task_id, status)
            self.event_store.record(task_id, event_type, payload)

    def complete_done(self, task_id: int, attempt_id: int | None) -> None:
        """Complete a successful task and retire its checkpoint atomically."""
        with self.phase():
            if attempt_id is not None:
                self.task_store.complete_attempt(attempt_id, "completed")
            self.task_store.transition(task_id, "done")
            self.event_store.record(task_id, "task.done")
            if self.checkpoint_store is not None:
                self.checkpoint_store.invalidate(task_id)

    def validation_decision(self, write: ValidationWrite) -> None:
        """Commit the validator result and its complete workflow decision."""
        if write.checkpoint is not None and self.checkpoint_store is None:
            raise TransactionError(
                "resumable validation decision requires checkpoint store"
            )
        with self.phase() as connection:
            task = self.task_store.get(write.task_id)
            if task is None or task["status"] != "validating":
                raise TransactionError("validation task is not validating")
            if write.attempt_id is not None:
                attempt = connection.execute(
                    "SELECT task_id, status FROM task_attempts WHERE id = ?",
                    (write.attempt_id,),
                ).fetchone()
                if (
                    attempt is None
                    or attempt["task_id"] != write.task_id
                    or attempt["status"] != "running"
                ):
                    raise TransactionError("validation attempt is not active")
            self.apply_validation_decision(
                self.task_store, self.event_store, self.checkpoint_store, write
            )

    @staticmethod
    def apply_validation_decision(
        task_store: Any, event_store: Any, checkpoint_store: Any, write: ValidationWrite
    ) -> None:
        """Write one decision; the caller supplies the transaction boundary."""
        task_id = write.task_id
        record = getattr(event_store, "record", lambda *_args: None)
        transition = getattr(task_store, "transition", lambda *_args: None)
        complete_attempt = getattr(task_store, "complete_attempt", lambda *_args: None)
        record(task_id, "task.validation.completed", write.validation)
        if write.checkpoint is not None and checkpoint_store is not None:
            checkpoint_store.save(task_id, **write.checkpoint)
        if write.attempt_id is not None:
            attempt_status = (
                "completed"
                if write.outcome == "done"
                else "waiting"
                if write.outcome == "wait"
                else "failed"
            )
            complete_attempt(
                write.attempt_id,
                attempt_status,
                write.message if attempt_status != "completed" else None,
            )
        if write.outcome == "done":
            transition(task_id, "done")
            record(task_id, "task.done")
        elif write.outcome == "wait":
            transition(task_id, "waiting")
            record(task_id, "task.waiting", {"reason": write.message})
        else:
            record(
                task_id,
                "task.validation.failed",
                {"error": write.message},
            )
            if write.outcome == "failed":
                transition(task_id, "failed")
                if write.event_type and write.event_type != "task.validation.failed":
                    record(task_id, write.event_type, write.event_payload)
                record(task_id, "task.failed", {"error": write.message})
            else:
                if write.replan is not None:
                    parent_version, version = write.replan
                    record_replan = getattr(task_store, "record_replan", None)
                    if record_replan is not None:
                        record_replan(
                            task_id,
                            "validation_failed",
                            version,
                            parent_plan_version=parent_version,
                            attempt_id=write.attempt_id,
                        )
                record(task_id, write.event_type, write.event_payload)
        if write.outcome in {"done", "failed"} and hasattr(
            checkpoint_store, "invalidate"
        ):
            checkpoint_store.invalidate(task_id)

    def cancel(self, task_id: int, reason: str) -> None:
        """Atomically cancel the task and clean up all resumable state."""
        with self.manager.atomic(fenced=False):
            self.task_store.cancel(task_id, reason)
            if self.checkpoint_store is not None:
                self.checkpoint_store.invalidate(task_id, reason)
            self.manager.connection.execute(
                """UPDATE task_human_interactions SET status = 'cancelled',
                   decided_at = CURRENT_TIMESTAMP WHERE task_id = ? AND status = 'pending'""",
                (task_id,),
            )
            self.event_store.record(task_id, "task.cancelled", {"reason": reason})
