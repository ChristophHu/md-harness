import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from time import perf_counter

from harness.alerting import (
    AlertError,
    AlertPolicy,
    JsonAlertState,
    SlackWebhookNotifier,
    evaluate_alerts,
)
from harness.application import build_orchestrator
from harness.config import ConfigError, load_config
from harness.operations_service import ServiceError, manage_launch_agents
from harness.security.secrets import SecretError, default_secret_provider
from harness.storage.offsite_backup import (
    BackupError,
    SSHStore,
    create_offsite_backup,
    restore_drill,
)
from harness.storage.operations import (
    JsonLogFormatter,
    OperationsThresholds,
    create_verified_backup,
    database_path_from_config,
    inspect_database,
    process_lock,
    restore_verified_backup,
    stale_task_ids,
    verify_backup,
)

_logger = logging.getLogger("harness.operations")


def _configure_logging() -> None:
    if not _logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonLogFormatter())
        _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _thresholds(config: dict) -> OperationsThresholds:
    settings = config.get("operations", {})
    offsite = settings.get("offsite", {})
    return OperationsThresholds(
        stale_task_minutes=settings.get("stale_task_minutes", 30),
        outbox_pending_minutes=settings.get("outbox_pending_minutes", 15),
        backup_receipt=_operation_path(
            config, offsite.get("receipt_file", "../state/offsite-backup.json")
        )
        if offsite.get("provider") == "ssh"
        else None,
        drill_receipt=_operation_path(
            config, offsite.get("drill_file", "../state/restore-drill.json")
        )
        if offsite.get("provider") == "ssh"
        else None,
        backup_max_age_minutes=offsite.get("backup_max_age_minutes", 120),
        drill_max_age_days=offsite.get("drill_max_age_days", 8),
    )


def _operation_path(config: dict, value: str) -> Path:
    base = (
        Path(config.get("_config_path", "config/config.yaml"))
        .expanduser()
        .resolve()
        .parent
    )
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).absolute()


def _offsite(config: dict) -> tuple[SSHStore, str, Path, Path, dict[str, Path]] | None:
    settings = config.get("operations", {}).get("offsite", {})
    provider = settings.get("provider", "none")
    if provider == "none":
        return None
    if provider != "ssh":
        raise BackupError("operations.offsite.provider must be none or ssh")
    store = SSHStore(settings.get("target", ""), settings.get("directory", ""))
    secret = config["secrets"]
    key = default_secret_provider(
        service_prefix=secret.service_prefix,
        account=secret.account,
        names=secret.names,
    ).require(settings.get("key_secret", "backup_encryption_key"))
    includes = settings.get("includes", {})
    if not isinstance(includes, dict) or any(
        not isinstance(label, str) or not isinstance(path, str)
        for label, path in includes.items()
    ):
        raise BackupError("operations.offsite.includes must map labels to paths")
    return (
        store,
        key,
        _operation_path(
            config, settings.get("receipt_file", "../state/offsite-backup.json")
        ),
        _operation_path(
            config, settings.get("drill_file", "../state/restore-drill.json")
        ),
        {label: _operation_path(config, path) for label, path in includes.items()},
    )


def _backup_directory(config_path: str, config: dict) -> str:
    configured = config.get("operations", {}).get("backup_directory", "backups")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(config_path).expanduser().resolve().parent / path
    return str(path.resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="MD Harness")
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.add_argument("--config", default="config/config.yaml")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("run", "resume", "cancel"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("task_id", type=int)
    subparsers.add_parser("doctor", help="Check database and operational queues")
    backup_parser = subparsers.add_parser(
        "backup", help="Create and restore-test a database backup"
    )
    backup_parser.add_argument("--keep", type=int)
    verify_parser = subparsers.add_parser(
        "verify-backup", help="Restore-test a database backup"
    )
    verify_parser.add_argument("backup_path")
    restore_parser = subparsers.add_parser(
        "restore-backup", help="Restore a verified backup to a new database path"
    )
    restore_parser.add_argument("backup_path")
    restore_parser.add_argument("destination")
    subparsers.add_parser(
        "restore-drill",
        help="Download and verify the latest offsite backup in isolation",
    )
    offsite_restore_parser = subparsers.add_parser(
        "restore-offsite", help="Restore a verified offsite bundle into a new directory"
    )
    offsite_restore_parser.add_argument("destination")
    maintenance_parser = subparsers.add_parser(
        "maintenance", help="Retry Vault publication and recover abandoned runs"
    )
    maintenance_parser.add_argument("--limit", type=int)
    subparsers.add_parser("alert", help="Check operational health and deliver alerts")
    api_parser = subparsers.add_parser(
        "serve", help="Start the HTTP API and Swagger UI"
    )
    api_parser.add_argument("--host")
    api_parser.add_argument("--port", type=int)
    service_parser = subparsers.add_parser(
        "service", help="Manage the macOS per-user launchd agents"
    )
    service_parser.add_argument("action", choices=("install", "status", "uninstall"))
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if args.command == "serve":
        _serve_api(parser, args)
        return
    if args.command in {
        "doctor",
        "backup",
        "verify-backup",
        "restore-backup",
        "restore-drill",
        "restore-offsite",
        "maintenance",
        "alert",
        "service",
    }:
        _run_operations_command(parser, args)
        return
    try:
        orchestrator, connection = build_orchestrator(args.config)
        try:
            started = perf_counter()
            if args.command == "run":
                result = orchestrator.run(args.task_id)
            elif args.command == "resume":
                result = orchestrator.resume(args.task_id)
            else:
                result = orchestrator.cancel(args.task_id)
        finally:
            connection.close()
    except ConfigError as error:
        parser.error(str(error))
    _configure_logging()
    _logger.info(
        "task.command.completed",
        extra={
            "fields": {
                "command": args.command,
                "task_id": args.task_id,
                "status": result.status.value,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    print(result.message or result.status.value)
    if result.status.value == "failed":
        raise SystemExit(1)


def _serve_api(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        import uvicorn

        from harness.api import create_app

        api_config = load_config(args.config)["api"]
        host = args.host or api_config.host
        port = args.port if args.port is not None else api_config.port
        app = create_app(args.config, host=host)
    except (ImportError, RuntimeError, ValueError, ConfigError) as error:
        parser.error(str(error))
    uvicorn.run(app, host=host, port=port)


def _run_operations_command(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    try:
        config = load_config(args.config)
        config["_config_path"] = args.config
        database = database_path_from_config(args.config)
        if args.command in {"restore-drill", "restore-offsite"}:
            offsite = _offsite(config)
            if offsite is None:
                raise BackupError("offsite backup is not configured")
            store, key, receipt, drill, _ = offsite
            with process_lock(database, "backup") as acquired:
                if not acquired:
                    _emit_json(
                        {"ok": True, "skipped": True, "reason": "already_running"}
                    )
                    return
                destination = (
                    args.destination if args.command == "restore-offsite" else None
                )
                _emit_json(restore_drill(store, key, receipt, drill, destination))
            return
        if args.command == "doctor":
            report = inspect_database(database, _thresholds(config))
            _emit_json(report)
            _configure_logging()
            _logger.info(
                "operations.doctor.completed",
                extra={"fields": {"status": report["status"], **report["metrics"]}},
            )
            if report["status"] != "ok":
                raise SystemExit(1)
            return
        if args.command == "verify-backup":
            report = verify_backup(args.backup_path)
            _emit_json(report)
            _configure_logging()
            _logger.info(
                "operations.backup.verify.completed",
                extra={
                    "fields": {"ok": report["ok"], "backup_path": report["backup_path"]}
                },
            )
            if not report["ok"]:
                raise SystemExit(1)
            return
        if args.command == "restore-backup":
            report = restore_verified_backup(args.backup_path, args.destination)
            _emit_json(report)
            _configure_logging()
            _logger.info(
                "operations.backup.restored",
                extra={
                    "fields": {
                        "backup_path": report["backup_path"],
                        "restored_path": report["restored_path"],
                    }
                },
            )
            return
        backup_settings = config.get("operations", {})
        if args.command == "maintenance":
            _configure_logging()
            with process_lock(database, "maintenance") as acquired:
                if not acquired:
                    _emit_json({"status": "skipped", "reason": "already_running"})
                    return
                _run_maintenance(args, config, database)
            return
        if args.command == "alert":
            with process_lock(database, "alert") as acquired:
                if not acquired:
                    _emit_json({"status": "skipped", "reason": "already_running"})
                    return
                _run_alert(args, config, database)
            return
        if args.command == "service":
            if args.action == "install":
                health = inspect_database(database, _thresholds(config))
                if health["status"] == "critical":
                    raise ValueError(
                        "refusing to install LaunchAgents while doctor reports critical; "
                        "resolve database issues first"
                    )
            report = manage_launch_agents(args.action, args.config, backup_settings)
            _emit_json(report)
            return
        keep = (
            args.keep
            if args.keep is not None
            else backup_settings.get("backup_keep", 7)
        )
        with process_lock(database, "backup") as acquired:
            if not acquired:
                _emit_json({"ok": True, "skipped": True, "reason": "already_running"})
                return
            report = create_verified_backup(
                database,
                _backup_directory(args.config, config),
                keep=keep,
            )
            offsite = _offsite(config)
            if offsite is not None:
                store, key, receipt, _, includes = offsite
                report["offsite"] = create_offsite_backup(
                    report["backup_path"], includes, store, key, receipt
                )
        _emit_json(report)
        _configure_logging()
        _logger.info(
            "operations.backup.created",
            extra={
                "fields": {
                    "backup_path": report["backup_path"],
                    "removed_backups": len(report["removed_backups"]),
                }
            },
        )
    except ConfigError as error:
        parser.error(str(error))
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        ServiceError,
        AlertError,
        SecretError,
        BackupError,
    ) as error:
        _emit_json({"ok": False, "error": str(error)})
        raise SystemExit(1) from error


def _run_maintenance(args: argparse.Namespace, config: dict, database: Path) -> None:
    settings = config.get("operations", {})
    limit = args.limit if args.limit is not None else settings.get("recovery_limit", 25)
    stale_minutes = settings.get("stale_task_minutes", 30)
    if type(limit) is not int or limit < 1:
        raise ValueError("maintenance limit must be positive")
    orchestrator, connection = build_orchestrator(args.config)
    recovered: list[dict[str, object]] = []
    dispatched: list[dict[str, object]] = []
    try:
        candidates = stale_task_ids(
            database, older_than_minutes=stale_minutes, limit=limit
        )
        for task_id in candidates:
            try:
                result = orchestrator.resume(task_id)
                recovered.append({"task_id": task_id, "status": result.status.value})
            except Exception as error:  # noqa: BLE001 - isolate one recovery attempt
                recovered.append(
                    {
                        "task_id": task_id,
                        "status": "failed",
                        "error_type": type(error).__name__,
                    }
                )
            _logger.info(
                "maintenance.task_recovery.completed",
                extra={"fields": recovered[-1]},
            )
        if settings.get("auto_dispatch", False):
            ready_tasks = [
                task
                for task in orchestrator.task_store.get_executable_tasks()
                if task["status"] == "ready"
            ][:limit]
            for task in ready_tasks:
                task_id = int(task["id"])
                try:
                    result = orchestrator.run(task_id)
                    dispatched.append(
                        {"task_id": task_id, "status": result.status.value}
                    )
                except Exception as error:  # noqa: BLE001 - isolate one dispatch
                    dispatched.append(
                        {
                            "task_id": task_id,
                            "status": "failed",
                            "error_type": type(error).__name__,
                        }
                    )
                _logger.info(
                    "maintenance.task_dispatch.completed",
                    extra={"fields": dispatched[-1]},
                )
        diagnostics = inspect_database(database, _thresholds(config))
    finally:
        connection.close()
    failed = any(item["status"] == "failed" for item in [*recovered, *dispatched])
    status = "critical" if failed else diagnostics["status"]
    report = {
        "status": status,
        "recovered_tasks": recovered,
        "dispatched_tasks": dispatched,
        "diagnostics": diagnostics,
    }
    _emit_json(report)
    _configure_logging()
    _logger.info(
        "operations.maintenance.completed",
        extra={
            "fields": {
                "status": status,
                "recovered_count": len(recovered),
                "dispatched_count": len(dispatched),
                "diagnostic_status": diagnostics["status"],
            }
        },
    )
    if status != "ok":
        raise SystemExit(1)


def _run_alert(args: argparse.Namespace, config: dict, database: Path) -> None:
    settings = config.get("operations", {}).get("alerts", {})
    if not isinstance(settings, dict):
        raise TypeError("operations.alerts must be a mapping")
    provider_kind = settings.get("provider", "none")
    if not isinstance(provider_kind, str) or provider_kind not in {"none", "slack"}:
        raise ValueError("operations.alerts.provider must be none or slack")
    policy = AlertPolicy(
        cooldown_minutes=settings.get("cooldown_minutes", 60),
        escalation_minutes=settings.get("escalation_minutes", 30),
        repeat_minutes=settings.get("repeat_minutes", 240),
    )
    state_file = settings.get("state_file", "../state/alerts.json")
    if not isinstance(state_file, str) or not state_file.strip():
        raise ValueError("operations.alerts.state_file must be a non-empty path")
    state_config = Path(state_file).expanduser()
    if not state_config.is_absolute():
        state_config = Path(args.config).expanduser().resolve().parent / state_config
    notifier = None
    if provider_kind == "slack":
        secret_name = settings.get("webhook_secret", "operations_slack_webhook")
        if not isinstance(secret_name, str) or not secret_name.strip():
            raise ValueError(
                "operations.alerts.webhook_secret must be a non-empty name"
            )
        secret_config = config["secrets"]
        secrets = default_secret_provider(
            service_prefix=secret_config.service_prefix,
            account=secret_config.account,
            names=secret_config.names,
        )
        webhook = secrets.require(secret_name)
        notifier = SlackWebhookNotifier(webhook)
    report = inspect_database(database, _thresholds(config))
    result = evaluate_alerts(
        report,
        JsonAlertState(state_config),
        policy,
        notifier,
    )
    _emit_json(result)
    _configure_logging()
    _logger.info(
        "operations.alert.completed",
        extra={
            "fields": {
                "status": result["status"],
                "active_count": len(result["active_alerts"]),
                "notification_count": len(result["notifications"]),
                "delivered": result["delivered"],
            }
        },
    )
    if result["status"] != "ok":
        raise SystemExit(1)
