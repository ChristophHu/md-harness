import runpy

import pytest

from harness.cli import main
from harness.config import ConfigError


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
