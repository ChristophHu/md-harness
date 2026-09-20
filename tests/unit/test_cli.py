import pytest
import runpy

from harness.cli import main


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
