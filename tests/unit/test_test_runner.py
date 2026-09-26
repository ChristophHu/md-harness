import pytest

from harness.tools.base import ResourceLimits, ToolError
from harness.tools.test_runner import TestRunner


def test_test_runner_executes_allowlisted_command(tmp_path):
    result = TestRunner(tmp_path).execute(["uv", "run", "pytest", "--version"])
    assert result["passed"] is True
    assert result["timed_out"] is False
    assert result["exit_code"] == 0


def test_test_runner_rejects_commands_and_shell_syntax(tmp_path):
    runner = TestRunner(tmp_path)
    with pytest.raises(ToolError):
        runner.execute(["sh", "-c", "echo nope"])
    with pytest.raises(ToolError):
        runner.execute(["python", "-m", "pytest", "|", "cat"])
    with pytest.raises(ToolError):
        runner.execute(["python", "-m", "pytest"], timeout=0)


def test_test_runner_reports_failed_command(tmp_path):
    result = TestRunner(tmp_path).execute(["uv", "run", "pytest", "missing_tests"])
    assert result["passed"] is False
    assert result["exit_code"] != 0


def test_test_runner_reports_timeout(tmp_path, monkeypatch):
    import subprocess

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["uv"], 1, output="partial", stderr="error")

    monkeypatch.setattr(subprocess, "run", timeout)
    result = TestRunner(tmp_path).execute(["uv", "run", "pytest"])
    assert result["timed_out"] is True
    assert result["passed"] is False


def test_test_runner_reports_missing_program(tmp_path, monkeypatch):
    import subprocess

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ToolError, match="not found"):
        TestRunner(tmp_path).execute(["uv", "run", "pytest"])


def test_test_runner_supports_workspace_working_directory(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    captured = {}

    def run(*_args, **kwargs):
        captured["cwd"] = kwargs["cwd"]
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("subprocess.run", run)
    result = TestRunner(tmp_path).execute(
        ["uv", "run", "pytest"], working_directory="tests"
    )
    assert result["passed"] is True
    assert captured["cwd"] == tmp_path / "tests"


def test_test_runner_rejects_invalid_working_directory(tmp_path):
    runner = TestRunner(tmp_path)
    with pytest.raises(ToolError, match="outside"):
        runner.execute(["uv", "run", "pytest"], working_directory="../")
    with pytest.raises(ToolError, match="does not exist"):
        runner.execute(["uv", "run", "pytest"], working_directory="missing")


def test_test_runner_rejects_working_directory_symlink_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside-test-runner"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="outside"):
        TestRunner(tmp_path).execute(
            ["uv", "run", "pytest"], working_directory="linked"
        )


def test_test_runner_enforces_changed_file_limit(tmp_path, monkeypatch):
    runner = TestRunner(tmp_path, limits=ResourceLimits(max_changed_files=1))
    states = iter([set(), {"a", "b"}])
    monkeypatch.setattr(TestRunner, "_files", lambda _runner: next(states))
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    with pytest.raises(ToolError, match="changed file limit"):
        runner.execute(["uv", "run", "pytest"])
