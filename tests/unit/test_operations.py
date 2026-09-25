import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from harness.storage.database import connect, initialize_database
from harness.storage.operations import (
    JsonLogFormatter,
    OperationsThresholds,
    create_verified_backup,
    database_path_from_config,
    inspect_database,
    restore_verified_backup,
    stale_task_ids,
    verify_backup,
)


def test_thresholds_require_positive_values():
    with pytest.raises(ValueError, match="stale_task_minutes"):
        OperationsThresholds(stale_task_minutes=0)
    with pytest.raises(ValueError, match="outbox_pending_minutes"):
        OperationsThresholds(outbox_pending_minutes=-1)
    with pytest.raises(ValueError, match="stale_task_minutes"):
        OperationsThresholds(stale_task_minutes="30")
    with pytest.raises(ValueError, match="backup_max_age_minutes"):
        OperationsThresholds(backup_max_age_minutes=0)
    with pytest.raises(ValueError, match="drill_max_age_days"):
        OperationsThresholds(drill_max_age_days=0)


def test_offsite_receipts_are_monitored_even_if_database_is_unavailable(tmp_path):
    receipt = tmp_path / "backup.json"
    drill = tmp_path / "drill.json"
    thresholds = OperationsThresholds(backup_receipt=receipt, drill_receipt=drill)
    report = inspect_database(tmp_path / "missing.sqlite", thresholds)
    assert {item["name"] for item in report["checks"]} >= {
        "database_access",
        "offsite_backup_fresh",
        "restore_drill_fresh",
    }
    path = initialize_database(tmp_path / "db.sqlite")
    now = datetime.now(UTC)
    receipt.write_text(json.dumps({"created_at": now.isoformat()}))
    drill.write_text(json.dumps({"completed_at": now.isoformat()}))
    assert inspect_database(path, thresholds)["status"] == "ok"
    receipt.write_text(
        json.dumps({"created_at": (now - timedelta(hours=3)).isoformat()})
    )
    assert inspect_database(path, thresholds)["status"] == "critical"
    receipt.write_text(json.dumps({"created_at": "2020-01-01T00:00:00"}))
    assert (
        next(
            item
            for item in inspect_database(path, thresholds)["checks"]
            if item["name"] == "offsite_backup_fresh"
        )["detail"]
        == "receipt missing or invalid"
    )
    receipt.write_text("not json")
    assert inspect_database(path, thresholds)["status"] == "critical"


def test_database_path_resolves_relative_and_absolute_config(tmp_path):
    relative_config = tmp_path / "config.yaml"
    relative_config.write_text("storage:\n  database: state/harness.sqlite\n")
    assert database_path_from_config(relative_config) == (
        tmp_path / "state" / "harness.sqlite"
    )
    absolute_config = tmp_path / "absolute.yaml"
    absolute_config.write_text(f"storage:\n  database: {tmp_path}/db.sqlite\n")
    assert database_path_from_config(absolute_config) == tmp_path / "db.sqlite"


def test_database_path_rejects_invalid_configuration(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text("storage: []\n")
    with pytest.raises(AttributeError):
        database_path_from_config(config)


def test_inspect_database_reports_healthy_counts_read_only(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO tasks (title, status) VALUES ('waiting', 'waiting')"
        )
        connection.execute("INSERT INTO tasks (title, status) VALUES ('done', 'done')")
    report = inspect_database(path)
    assert report["status"] == "ok"
    assert report["metrics"]["waiting_tasks"] == 1
    assert report["metrics"]["active_tasks"] == {}
    assert report["metrics"]["outbox_pending"] == 0
    assert report["metrics"]["unresolved_tool_invocations"] == 0
    assert all(check["ok"] for check in report["checks"])


def test_inspect_database_reports_stale_and_unresolved_work(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO tasks (title, status, claim_token, claim_expires_at, updated_at)
               VALUES ('stuck', 'executing', 'expired', '2000-01-01 00:00:00',
                       '2000-01-01 00:00:00')"""
        )
        connection.execute(
            """INSERT INTO task_checkpoints
               (task_id, phase, next_action, resume_claim_token, resume_claim_expires_at)
               VALUES (1, 'execution', 'wait', 'expired', '2000-01-01 00:00:00')"""
        )
        connection.execute(
            """INSERT INTO task_human_interactions
               (interaction_id, task_id, wait_token, kind, prompt, response_schema,
                resume_action) VALUES ('interaction', 1, 'wait', 'information_request',
                'question', '{}', 'retry_execution')"""
        )
        connection.execute(
            """INSERT INTO vault_decision_outbox
               (interaction_id, project_key, status, created_at, last_error)
               VALUES ('interaction', 'abc', 'pending', '2000-01-01 00:00:00', 'disk error')"""
        )
        connection.execute(
            """INSERT INTO task_human_interactions
               (interaction_id, task_id, wait_token, kind, prompt, response_schema,
                resume_action) VALUES ('interaction-2', 1, 'wait-2', 'information_request',
                'question', '{}', 'retry_execution')"""
        )
        connection.execute(
            """INSERT INTO task_human_interactions
               (interaction_id, task_id, wait_token, kind, prompt, response_schema,
                resume_action) VALUES ('interaction-3', 1, 'wait-3', 'information_request',
                'question', '{}', 'retry_execution')"""
        )
        connection.execute(
            """INSERT INTO vault_decision_outbox
               (interaction_id, project_key, status, claim_expires_at)
               VALUES ('interaction-2', 'abc', 'blocked', NULL)"""
        )
        connection.execute(
            """INSERT INTO vault_decision_outbox
               (interaction_id, project_key, status, claim_expires_at)
               VALUES ('interaction-3', 'abc', 'publishing', '2000-01-01 00:00:00')"""
        )
        connection.execute(
            """INSERT INTO task_tool_invocations
                (invocation_id, task_id, plan_fingerprint, plan_version, step_id,
                tool_name, arguments_fingerprint, permission_scope, status, started_at)
               VALUES ('invoke', 1, 'plan', 1, 'step', 'filesystem', 'args', 'write',
                       'started', '2000-01-01 00:00:00')"""
        )
    report = inspect_database(path)
    assert report["status"] == "warning"
    metrics = report["metrics"]
    assert metrics["active_tasks"] == {"executing": 1}
    assert metrics["expired_task_claims"] == 1
    assert metrics["expired_resume_claims"] == 1
    assert metrics["outbox_old_pending"] == 1
    assert metrics["outbox_blocked"] == 1
    assert metrics["outbox_expired_claims"] == 1
    assert metrics["outbox_publish_failures"] == 1
    assert metrics["unresolved_tool_invocations"] == 1
    assert not any(check["name"] == "waiting_tasks" for check in report["checks"])


def test_inspect_database_reports_critical_for_schema_and_missing_file(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")
    assert inspect_database(path)["status"] == "critical"
    missing = inspect_database(tmp_path / "missing.sqlite")
    assert missing["status"] == "critical"
    assert missing["checks"][-1]["name"] == "database_access"


def test_inspect_database_handles_corrupt_sqlite_file(tmp_path):
    path = tmp_path / "bad.sqlite"
    path.write_text("not a database")
    report = inspect_database(path)
    assert report["status"] == "critical"
    assert report["checks"][-1]["severity"] == "critical"


def test_stale_task_ids_only_selects_old_unclaimed_active_tasks(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO tasks (title, status, updated_at, claim_token, claim_expires_at)
               VALUES ('orphan', 'executing', '2000-01-01', 'old', '2000-01-01')"""
        )
        connection.execute(
            """INSERT INTO tasks (title, status, updated_at, claim_token, claim_expires_at)
               VALUES ('healthy', 'executing', '2000-01-01', 'live', '2999-01-01')"""
        )
        connection.execute(
            "INSERT INTO tasks (title, status, updated_at) VALUES ('waiting', 'waiting', '2000-01-01')"
        )
    assert stale_task_ids(path, older_than_minutes=10) == [1]
    assert stale_task_ids(path, limit=1) == [1]
    with pytest.raises(ValueError, match="older_than_minutes"):
        stale_task_ids(path, older_than_minutes=0)
    with pytest.raises(ValueError, match="limit"):
        stale_task_ids(path, limit=0)


def test_inspect_does_not_alert_for_a_long_run_with_a_valid_claim(tmp_path):
    path = initialize_database(tmp_path / "harness.sqlite")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO tasks (title, status, updated_at, claim_token, claim_expires_at)
               VALUES ('long run', 'executing', '2000-01-01', 'live', '2999-01-01')"""
        )
    report = inspect_database(path)
    assert report["metrics"]["stale_active_tasks"] == 0
    assert report["status"] == "ok"


def test_verify_backup_restores_database_and_rejects_missing_or_invalid(tmp_path):
    database = initialize_database(tmp_path / "source.sqlite")
    backup = tmp_path / "backup.sqlite"
    with connect(database) as connection:
        with sqlite3.connect(backup) as destination:
            connection.backup(destination)
    verified = verify_backup(backup)
    assert verified["ok"] is True
    assert verified["schema_version"] == 18
    assert verify_backup(tmp_path / "absent.sqlite")["error"] == "backup does not exist"
    broken = tmp_path / "broken.sqlite"
    broken.write_text("invalid")
    assert verify_backup(broken)["ok"] is False


def test_verify_backup_rejects_old_schema(tmp_path):
    backup = tmp_path / "old.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA user_version = 1")
    report = verify_backup(backup)
    assert report["ok"] is False
    assert report["missing_tables"]


def test_create_verified_backup_restores_and_rotates_only_matching_files(tmp_path):
    database = initialize_database(tmp_path / "source.sqlite")
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / "harness-20200101.sqlite").touch()
    (directory / "keep-me.txt").touch()
    result = create_verified_backup(database, directory, keep=1)
    assert result["ok"] is True
    assert result["verified"]["ok"] is True
    assert len(list(directory.glob("harness-*.sqlite"))) == 1
    assert (directory / "keep-me.txt").exists()
    with pytest.raises(ValueError, match="keep"):
        create_verified_backup(database, directory, keep=0)


def test_create_verified_backup_removes_failed_backup(tmp_path, monkeypatch):
    source = initialize_database(tmp_path / "source.sqlite")
    monkeypatch.setattr(
        "harness.storage.operations.verify_backup", lambda _path: {"ok": False}
    )
    with pytest.raises(sqlite3.DatabaseError, match="verification failed"):
        create_verified_backup(source, tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


def test_restore_verified_backup_is_non_overwriting_and_reverified(tmp_path):
    database = initialize_database(tmp_path / "source.sqlite")
    backup = tmp_path / "backup.sqlite"
    with connect(database) as connection, sqlite3.connect(backup) as destination:
        connection.backup(destination)
    target = tmp_path / "restore" / "restored.sqlite"
    result = restore_verified_backup(backup, target)
    assert result["ok"] is True
    assert result["verified"]["ok"] is True
    with pytest.raises(FileExistsError, match="already exists"):
        restore_verified_backup(backup, target)
    with pytest.raises(sqlite3.DatabaseError, match="verification failed"):
        restore_verified_backup(tmp_path / "missing.sqlite", tmp_path / "other.sqlite")


def test_restore_verified_backup_cleans_partial_target_on_restore_failure(
    tmp_path, monkeypatch
):
    database = initialize_database(tmp_path / "source.sqlite")
    backup = tmp_path / "backup.sqlite"
    with connect(database) as connection, sqlite3.connect(backup) as destination:
        connection.backup(destination)
    target = tmp_path / "restored.sqlite"

    def fail_restore(_source, path):
        path.touch()
        raise sqlite3.DatabaseError("restore failed")

    monkeypatch.setattr("harness.storage.operations.restore_database", fail_restore)
    with pytest.raises(sqlite3.DatabaseError, match="restore failed"):
        restore_verified_backup(backup, target)
    assert not target.exists()


def test_restore_verified_backup_cleans_target_if_restored_copy_fails_verification(
    tmp_path, monkeypatch
):
    database = initialize_database(tmp_path / "source.sqlite")
    backup = tmp_path / "backup.sqlite"
    with connect(database) as connection, sqlite3.connect(backup) as destination:
        connection.backup(destination)
    responses = iter(({"ok": True}, {"ok": False}))
    monkeypatch.setattr(
        "harness.storage.operations.verify_backup", lambda _path: next(responses)
    )
    target = tmp_path / "restored.sqlite"
    with pytest.raises(
        sqlite3.DatabaseError, match="restored database verification failed"
    ):
        restore_verified_backup(backup, target)
    assert not target.exists()


def test_json_log_formatter_includes_context_and_exception():
    record = logging.LogRecord(
        "harness.test", logging.INFO, "test.py", 12, "task.finished", (), None
    )
    record.fields = {"task_id": 8, "duration_ms": 4.5}
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["event"] == "task.finished"
    assert payload["task_id"] == 8
    assert payload["level"] == "info"

    try:
        raise RuntimeError("expected")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
    payload = json.loads(JsonLogFormatter().format(record))
    assert "RuntimeError: expected" in payload["exception"]


def test_json_log_formatter_omits_non_mapping_fields():
    record = logging.LogRecord(
        "harness.test", logging.WARNING, "test.py", 1, "warning", (), None
    )
    record.fields = ["not", "mapping"]
    payload = json.loads(JsonLogFormatter().format(record))
    assert "fields" not in payload
