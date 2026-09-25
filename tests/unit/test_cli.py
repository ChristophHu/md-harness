import json
import runpy
from contextlib import nullcontext

import pytest

from harness.cli import main
from harness.config import ConfigError
from harness.storage.database import connect, initialize_database


def test_cli_prints_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["harness"])
    main()
    output = capsys.readouterr().out
    assert "MD Harness" in output
    assert "--version" in output


def test_cli_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["harness", "--version"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_cli_rejects_unknown_argument(monkeypatch):
    monkeypatch.setattr("sys.argv", ["harness", "--unknown"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_module_entrypoint_calls_main(monkeypatch):
    called = False

    def fake_main():
        nonlocal called
        called = True

    monkeypatch.setattr("harness.cli.main", fake_main)
    runpy.run_module("harness.__main__", run_name="__main__")
    assert called is True


def test_cli_without_command_prints_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["harness", "--config", "custom.yaml"])
    main()
    assert "MD Harness" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["run", "resume", "cancel"])
def test_cli_dispatches_engine_command(monkeypatch, capsys, command):
    class Result:
        status = type("Status", (), {"value": "success"})()
        message = "done"

    class Orchestrator:
        def __init__(self):
            self.called = None

        def run(self, task_id):
            self.called = ("run", task_id)
            return Result()

        def resume(self, task_id):
            self.called = ("resume", task_id)
            return Result()

        def cancel(self, task_id):
            self.called = ("cancel", task_id)
            return Result()

    orchestrator = Orchestrator()
    monkeypatch.setattr(
        "harness.cli.build_orchestrator",
        lambda _: (orchestrator, type("C", (), {"close": lambda self: None})()),
    )
    monkeypatch.setattr("sys.argv", ["harness", command, "12"])
    main()
    assert orchestrator.called == (command, 12)
    assert capsys.readouterr().out.strip() == "done"


def test_cli_reports_config_error(monkeypatch):
    monkeypatch.setattr(
        "harness.cli.build_orchestrator",
        lambda _: (_ for _ in ()).throw(ConfigError("bad config")),
    )
    monkeypatch.setattr("sys.argv", ["harness", "run", "1"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_returns_failure_exit_code(monkeypatch, capsys):
    class Result:
        status = type("Status", (), {"value": "failed"})()
        message = "failed"

    class Orchestrator:
        def run(self, _task_id):
            return Result()

    monkeypatch.setattr(
        "harness.cli.build_orchestrator",
        lambda _: (Orchestrator(), type("C", (), {"close": lambda self: None})()),
    )
    monkeypatch.setattr("sys.argv", ["harness", "run", "1"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert capsys.readouterr().out.strip() == "failed"


def test_cli_doctor_reports_healthy_database(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(f"storage:\n  database: {database}\n")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "doctor"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"


def test_cli_doctor_returns_alert_exit_code(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  database: missing.sqlite\n")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "doctor"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "critical"


def test_cli_backup_and_verify_backup(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n  backup_directory: archive\n"
    )
    monkeypatch.setattr(
        "sys.argv", ["harness", "--config", str(config), "backup", "--keep", "2"]
    )
    main()
    backup_report = json.loads(capsys.readouterr().out)
    assert backup_report["ok"] is True
    monkeypatch.setattr(
        "sys.argv",
        [
            "harness",
            "--config",
            str(config),
            "verify-backup",
            backup_report["backup_path"],
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_restores_backup_to_a_new_path(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(f"storage:\n  database: {database}\n")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "backup"])
    main()
    backup = json.loads(capsys.readouterr().out)["backup_path"]
    target = tmp_path / "restored.sqlite"
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "--config", str(config), "restore-backup", backup, str(target)],
    )
    main()
    assert json.loads(capsys.readouterr().out)["restored_path"] == str(target)
    assert target.exists()


def test_cli_rejects_invalid_operations_yaml(tmp_path, monkeypatch):
    config = tmp_path / "invalid.yaml"
    config.write_text("[")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "doctor"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_verify_backup_failure_returns_nonzero(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  database: unused.sqlite\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "harness",
            "--config",
            str(config),
            "verify-backup",
            str(tmp_path / "missing"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_invalid_operations_config_returns_json_failure(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "storage:\n  database: missing.sqlite\noperations:\n  stale_task_minutes: 0\n"
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "doctor"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "stale_task_minutes" in json.loads(capsys.readouterr().out)["error"]


def test_cli_backup_database_failure_is_reported(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  database: missing.sqlite\n")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "backup"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize(
    "fails,dispatch,dispatch_fails",
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (False, True, True),
    ],
)
def test_cli_maintenance_recovers_only_stale_active_runs(
    tmp_path, monkeypatch, capsys, fails, dispatch, dispatch_fails
):
    database = initialize_database(tmp_path / "state.sqlite")
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO tasks (title, status, claim_token, claim_expires_at, updated_at)
               VALUES ('orphan', 'executing', 'expired', '2000-01-01', '2000-01-01')"""
        )
        if dispatch:
            connection.execute(
                """INSERT INTO tasks (title, status, approval_status)
                   VALUES ('queued', 'ready', 'approved')"""
            )
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n  stale_task_minutes: 1\n  auto_dispatch: {str(dispatch).lower()}\n"
    )
    connection = connect(database)

    class Status:
        value = "failed" if fails else "waiting"

    class Result:
        status = Status()

    class Orchestrator:
        task_store = type(
            "TaskStoreStub",
            (),
            {
                "get_executable_tasks": lambda _self: connection.execute(
                    "SELECT id, status FROM tasks WHERE status = 'ready' AND approval_status = 'approved' ORDER BY id"
                ).fetchall()
            },
        )()

        def resume(self, task_id):
            if fails:
                raise RuntimeError("recovery failed")
            connection.execute(
                "UPDATE tasks SET status = 'waiting', claim_token = NULL, claim_expires_at = NULL WHERE id = ?",
                (task_id,),
            )
            connection.commit()
            return Result()

        def run(self, task_id):
            if dispatch_fails:
                raise RuntimeError("dispatch failed")
            connection.execute(
                "UPDATE tasks SET status = 'waiting' WHERE id = ?", (task_id,)
            )
            connection.commit()
            return Result()

    monkeypatch.setattr(
        "harness.cli.build_orchestrator", lambda _path: (Orchestrator(), connection)
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "maintenance"])
    if fails or dispatch_fails:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "critical"
        if fails:
            assert output["recovered_tasks"][0]["error_type"] == "RuntimeError"
        if dispatch_fails:
            assert output["dispatched_tasks"][0]["error_type"] == "RuntimeError"
    else:
        main()
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["recovered_tasks"] == [{"task_id": 1, "status": "waiting"}]
        if dispatch:
            assert output["dispatched_tasks"] == [{"task_id": 2, "status": "waiting"}]
        else:
            assert output["dispatched_tasks"] == []


def test_cli_maintenance_rejects_nonpositive_limit(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  database: unused.sqlite\n")
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "--config", str(config), "maintenance", "--limit", "0"],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "limit must be positive" in json.loads(capsys.readouterr().out)["error"]


def test_cli_service_reports_launch_agent_state(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        "storage:\n  database: state.sqlite\noperations:\n  auto_dispatch: false\n"
    )
    monkeypatch.setattr(
        "harness.cli.manage_launch_agents",
        lambda action, config_path, settings: {
            "action": action,
            "auto_dispatch": settings["auto_dispatch"],
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["harness", "--config", str(config), "service", "status"]
    )
    main()
    assert json.loads(capsys.readouterr().out) == {
        "action": "status",
        "auto_dispatch": False,
    }


def test_cli_service_install_requires_noncritical_doctor(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  database: missing.sqlite\n")
    calls = []
    monkeypatch.setattr(
        "harness.cli.inspect_database", lambda *_args: {"status": "critical"}
    )
    monkeypatch.setattr(
        "harness.cli.manage_launch_agents",
        lambda *_args: calls.append("installed") or {"action": "install"},
    )
    monkeypatch.setattr(
        "sys.argv", ["harness", "--config", str(config), "service", "install"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert calls == []
    assert "doctor reports critical" in json.loads(capsys.readouterr().out)["error"]


def test_cli_service_install_does_not_enable_auto_dispatch(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "storage:\n  database: state.sqlite\noperations:\n  auto_dispatch: false\n"
    )
    monkeypatch.setattr("harness.cli.inspect_database", lambda *_args: {"status": "ok"})
    monkeypatch.setattr(
        "harness.cli.manage_launch_agents",
        lambda action, _config, settings: {
            "action": action,
            "auto_dispatch": settings["auto_dispatch"],
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["harness", "--config", str(config), "service", "install"]
    )
    main()
    assert json.loads(capsys.readouterr().out) == {
        "action": "install",
        "auto_dispatch": False,
    }


@pytest.mark.parametrize("command", ["maintenance", "backup", "alert"])
def test_cli_skips_job_when_process_lock_is_already_held(
    tmp_path, monkeypatch, capsys, command
):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(f"storage:\n  database: {database}\n")
    monkeypatch.setattr("harness.cli.process_lock", lambda *_args: nullcontext(False))
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), command])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "already_running"
    assert report.get("status", report.get("ok")) in {"skipped", True}


def test_cli_alert_writes_local_state_without_configured_sink(
    tmp_path, monkeypatch, capsys
):
    database = initialize_database(tmp_path / "state.sqlite")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    config.write_text(f"storage:\n  database: {database}\n")
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "alert"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["delivered"] is False
    assert (tmp_path / "state/alerts.json").exists()


def test_cli_alert_sends_to_slack_without_leaking_secret(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n"
        "  alerts:\n    provider: slack\n    webhook_secret: alert_hook\n"
    )
    calls = []

    class Secrets:
        def require(self, name):
            calls.append(name)
            return "https://hooks.slack.com/services/private-secret"

    class Notifier:
        def __init__(self, url):
            calls.append(url)

        def send(self, message):
            calls.append(message["name"])

    monkeypatch.setattr(
        "harness.cli.default_secret_provider", lambda **_kwargs: Secrets()
    )
    monkeypatch.setattr("harness.cli.SlackWebhookNotifier", Notifier)
    monkeypatch.setattr(
        "harness.cli.inspect_database",
        lambda *_args: {
            "status": "warning",
            "checks": [{"name": "stale_active_tasks", "ok": False, "detail": 1}],
        },
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "alert"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "private-secret" not in output
    assert calls[0] == "alert_hook"
    assert calls[-1] == "stale_active_tasks"


def test_cli_alert_fails_if_slack_secret_is_unavailable(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n"
        "  alerts:\n    provider: slack\n"
    )

    class Secrets:
        def require(self, _name):
            from harness.security.secrets import SecretError

            raise SecretError("Missing secret: operations_slack_webhook")

    monkeypatch.setattr(
        "harness.cli.default_secret_provider", lambda **_kwargs: Secrets()
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "alert"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "Missing secret" in json.loads(capsys.readouterr().out)["error"]


def test_cli_alert_rejects_unknown_provider(tmp_path, monkeypatch, capsys):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n"
        "  alerts:\n    provider: pager\n"
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "alert"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert (
        "provider must be none or slack" in json.loads(capsys.readouterr().out)["error"]
    )


@pytest.mark.parametrize(
    "alerts_yaml,message",
    [
        ("alerts: invalid", "alerts must be a mapping"),
        ("alerts:\n    provider: []", "provider must be none or slack"),
        ("alerts:\n    state_file: ' '", "state_file must be a non-empty path"),
        (
            "alerts:\n    provider: slack\n    webhook_secret: ' '",
            "webhook_secret must be a non-empty name",
        ),
    ],
)
def test_cli_alert_validates_nested_configuration(
    tmp_path, monkeypatch, capsys, alerts_yaml, message
):
    database = initialize_database(tmp_path / "state.sqlite")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"storage:\n  database: {database}\noperations:\n  {alerts_yaml}\n"
    )
    monkeypatch.setattr("sys.argv", ["harness", "--config", str(config), "alert"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert message in json.loads(capsys.readouterr().out)["error"]
