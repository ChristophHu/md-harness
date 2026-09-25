"""Read-only operational diagnostics and verified SQLite backup routines."""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.storage.database import (
    CURRENT_SCHEMA_VERSION,
    backup_database,
    connect,
    restore_database,
)


@contextmanager
def process_lock(database_path: str | Path, job: str):
    """Acquire a crash-released per-database lock for one scheduled job."""
    if job not in {"maintenance", "backup", "alert"}:
        raise ValueError("job must be maintenance, backup or alert")
    database = Path(database_path).expanduser().resolve()
    lock_directory = database.parent / ".harness-locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    handle = (lock_directory / f"{database.name}.{job}.lock").open("a")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@dataclass(frozen=True, slots=True)
class OperationsThresholds:
    """Age thresholds after which operational queues should be reviewed."""

    stale_task_minutes: int = 30
    outbox_pending_minutes: int = 15

    def __post_init__(self) -> None:
        if type(self.stale_task_minutes) is not int or self.stale_task_minutes < 1:
            raise ValueError("stale_task_minutes must be positive")
        if (
            type(self.outbox_pending_minutes) is not int
            or self.outbox_pending_minutes < 1
        ):
            raise ValueError("outbox_pending_minutes must be positive")


def database_path_from_config(config_path: str | Path) -> Path:
    """Resolve the configured database path without initializing the app."""
    from harness.config import load_config

    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    configured = Path(config.get("storage", {}).get("database", "state/harness.sqlite"))
    if not configured.is_absolute():
        configured = path.parent / configured
    return configured.expanduser().resolve()


def inspect_database(
    database_path: str | Path,
    thresholds: OperationsThresholds | None = None,
) -> dict[str, Any]:
    """Return a read-only health snapshot suitable for CLI and monitoring."""
    thresholds = thresholds or OperationsThresholds()
    path = Path(database_path).expanduser().resolve()
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    try:
        if not path.is_file():
            raise sqlite3.OperationalError("database file does not exist")
        with connect(path) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            checks.append(
                _check(
                    "database_integrity",
                    integrity == "ok",
                    integrity,
                    severity="critical",
                )
            )
            checks.append(
                _check(
                    "schema_version",
                    version == CURRENT_SCHEMA_VERSION,
                    f"{version}/{CURRENT_SCHEMA_VERSION}",
                    severity="critical",
                )
            )
            active_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM tasks
                   WHERE status IN ('planning', 'executing', 'validating')
                   GROUP BY status"""
            ).fetchall()
            metrics["active_tasks"] = {
                row["status"]: row["count"] for row in active_rows
            }
            metrics["waiting_tasks"] = _count(
                connection, "SELECT COUNT(*) FROM tasks WHERE status = 'waiting'"
            )
            metrics["ready_approved_tasks"] = _count(
                connection,
                "SELECT COUNT(*) FROM tasks WHERE status = 'ready' AND approval_status = 'approved'",
            )
            stale_tasks = _count(
                connection,
                """SELECT COUNT(*) FROM tasks WHERE status IN
                   ('planning', 'executing', 'validating')
                   AND updated_at < datetime('now', ?)
                   AND (claim_token IS NULL OR claim_expires_at IS NULL
                        OR claim_expires_at < CURRENT_TIMESTAMP)""",
                (f"-{thresholds.stale_task_minutes} minutes",),
            )
            metrics["stale_active_tasks"] = stale_tasks
            expired_task_claims = _count(
                connection,
                """SELECT COUNT(*) FROM tasks WHERE claim_token IS NOT NULL
                   AND claim_expires_at < CURRENT_TIMESTAMP
                   AND status NOT IN ('done', 'failed', 'cancelled')""",
            )
            metrics["expired_task_claims"] = expired_task_claims
            checks.append(_check("stale_active_tasks", stale_tasks == 0, stale_tasks))
            checks.append(
                _check(
                    "expired_task_claims", expired_task_claims == 0, expired_task_claims
                )
            )
            metrics["expired_resume_claims"] = _count(
                connection,
                """SELECT COUNT(*) FROM task_checkpoints
                   WHERE resume_claim_token IS NOT NULL
                   AND resume_claim_expires_at < CURRENT_TIMESTAMP
                   AND invalidated_at IS NULL""",
            )
            checks.append(
                _check(
                    "expired_resume_claims",
                    metrics["expired_resume_claims"] == 0,
                    metrics["expired_resume_claims"],
                )
            )
            metrics.update(_outbox_metrics(connection, thresholds))
            checks.extend(_outbox_checks(metrics))
            metrics["unresolved_tool_invocations"] = _count(
                connection,
                """SELECT COUNT(*) FROM task_tool_invocations i
                   JOIN tasks t ON t.id = i.task_id
                   WHERE i.status = 'started'
                   AND (t.status = 'waiting' OR t.claim_token IS NULL
                        OR t.claim_expires_at < CURRENT_TIMESTAMP)
                   AND i.started_at < datetime('now', ?)""",
                (f"-{thresholds.stale_task_minutes} minutes",),
            )
            checks.append(
                _check(
                    "unresolved_tool_invocations",
                    metrics["unresolved_tool_invocations"] == 0,
                    metrics["unresolved_tool_invocations"],
                )
            )
            metrics["schema_version"] = version
            metrics["database_path"] = str(path)
    except (sqlite3.Error, OSError) as error:
        checks.append(_check("database_access", False, str(error), severity="critical"))
        metrics["database_path"] = str(path)

    status = (
        "critical"
        if any(check["severity"] == "critical" and not check["ok"] for check in checks)
        else "warning"
        if any(not check["ok"] for check in checks)
        else "ok"
    )
    return {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "metrics": metrics,
    }


def stale_task_ids(
    database_path: str | Path,
    *,
    older_than_minutes: int = 30,
    limit: int = 25,
) -> list[int]:
    """List abandoned active tasks eligible for fenced resume takeover."""
    if type(older_than_minutes) is not int or older_than_minutes < 1:
        raise ValueError("older_than_minutes must be positive")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be positive")
    path = Path(database_path).expanduser().resolve()
    with connect(path) as connection:
        rows = connection.execute(
            """SELECT id FROM tasks WHERE status IN ('planning', 'executing', 'validating')
               AND updated_at < datetime('now', ?)
               AND (claim_token IS NULL OR claim_expires_at IS NULL
                    OR claim_expires_at < CURRENT_TIMESTAMP)
               ORDER BY updated_at, id LIMIT ?""",
            (f"-{older_than_minutes} minutes", limit),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _outbox_metrics(
    connection: sqlite3.Connection, thresholds: OperationsThresholds
) -> dict[str, int]:
    statuses = connection.execute(
        "SELECT status, COUNT(*) AS count FROM vault_decision_outbox GROUP BY status"
    ).fetchall()
    result = {f"outbox_{status}": count for status, count in statuses}
    result["outbox_pending"] = result.get("outbox_pending", 0)
    result["outbox_publishing"] = result.get("outbox_publishing", 0)
    result["outbox_published"] = result.get("outbox_published", 0)
    result["outbox_blocked"] = result.get("outbox_blocked", 0)
    result["outbox_old_pending"] = _count(
        connection,
        """SELECT COUNT(*) FROM vault_decision_outbox
           WHERE status = 'pending' AND created_at < datetime('now', ?)""",
        (f"-{thresholds.outbox_pending_minutes} minutes",),
    )
    result["outbox_expired_claims"] = _count(
        connection,
        """SELECT COUNT(*) FROM vault_decision_outbox
           WHERE status = 'publishing' AND claim_expires_at < CURRENT_TIMESTAMP""",
    )
    result["outbox_publish_failures"] = _count(
        connection,
        """SELECT COUNT(*) FROM vault_decision_outbox
           WHERE status = 'pending' AND last_error IS NOT NULL""",
    )
    return result


def _outbox_checks(metrics: dict[str, int]) -> list[dict[str, Any]]:
    return [
        _check(
            "outbox_old_pending",
            metrics["outbox_old_pending"] == 0,
            metrics["outbox_old_pending"],
        ),
        _check(
            "outbox_expired_claims",
            metrics["outbox_expired_claims"] == 0,
            metrics["outbox_expired_claims"],
        ),
        _check(
            "outbox_blocked", metrics["outbox_blocked"] == 0, metrics["outbox_blocked"]
        ),
        _check(
            "outbox_publish_failures",
            metrics["outbox_publish_failures"] == 0,
            metrics["outbox_publish_failures"],
        ),
    ]


def _count(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> int:
    return int(connection.execute(query, parameters).fetchone()[0])


def _check(
    name: str, ok: bool, detail: Any, *, severity: str = "warning"
) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "severity": "ok" if ok else severity,
        "detail": detail,
    }


def verify_backup(backup_path: str | Path) -> dict[str, Any]:
    """Restore a backup into a temporary file and validate it end-to-end."""
    source = Path(backup_path).expanduser().resolve()
    if not source.is_file():
        return {
            "ok": False,
            "backup_path": str(source),
            "error": "backup does not exist",
        }
    try:
        with tempfile.TemporaryDirectory(prefix="harness-restore-check-") as directory:
            restored_path = restore_database(
                source, Path(directory) / "restored.sqlite"
            )
            with connect(restored_path) as restored:
                check = restored.execute("PRAGMA quick_check").fetchone()[0]
                version = int(restored.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    row[0]
                    for row in restored.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            required = {
                "tasks",
                "task_events",
                "task_checkpoints",
                "vault_decision_outbox",
            }
            missing = sorted(required - tables)
            ok = check == "ok" and version == CURRENT_SCHEMA_VERSION and not missing
            return {
                "ok": ok,
                "backup_path": str(source),
                "integrity": check,
                "schema_version": version,
                "missing_tables": missing,
            }
    except (sqlite3.Error, OSError) as error:
        return {"ok": False, "backup_path": str(source), "error": str(error)}


def create_verified_backup(
    database_path: str | Path,
    backup_directory: str | Path,
    *,
    keep: int = 7,
) -> dict[str, Any]:
    """Create, restore-test and rotate timestamped backups in one directory."""
    if keep < 1:
        raise ValueError("keep must be positive")
    source = Path(database_path).expanduser().resolve()
    directory = Path(backup_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"harness-{stamp}.sqlite"
    try:
        backup_database(source, destination)
        verification = verify_backup(destination)
        if not verification["ok"]:
            raise sqlite3.DatabaseError(f"backup verification failed: {verification}")
    except (sqlite3.Error, OSError, ValueError):
        destination.unlink(missing_ok=True)
        raise
    backups = sorted(directory.glob("harness-*.sqlite"), key=lambda item: item.name)
    removed = backups[:-keep]
    for old_backup in removed:
        old_backup.unlink()
    return {
        "ok": True,
        "backup_path": str(destination),
        "verified": verification,
        "removed_backups": [str(item) for item in removed],
    }


def restore_verified_backup(
    backup_path: str | Path, destination: str | Path
) -> dict[str, Any]:
    """Restore a verified backup to a new path without replacing existing data."""
    source = Path(backup_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"restore destination already exists: {target}")
    verification = verify_backup(source)
    if not verification["ok"]:
        raise sqlite3.DatabaseError(f"backup verification failed: {verification}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        restore_database(source, target)
        restored_verification = verify_backup(target)
        if not restored_verification["ok"]:
            raise sqlite3.DatabaseError(
                f"restored database verification failed: {restored_verification}"
            )
    except (sqlite3.Error, OSError, ValueError):
        target.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "backup_path": str(source),
        "restored_path": str(target),
        "verified": restored_verification,
    }


class JsonLogFormatter(logging.Formatter):
    """Small JSON formatter that keeps operational metadata structured."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
