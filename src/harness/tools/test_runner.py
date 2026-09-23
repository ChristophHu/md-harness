"""Safe, allowlisted test-command execution inside a workspace."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.tools.base import (
    PermissionLevel,
    ResourceLimits,
    Tool,
    ToolDefinition,
    ToolError,
    ToolParameter,
)


@dataclass(frozen=True)
class TestRunner(Tool):
    __test__ = False
    root: Path
    allowed_prefixes: tuple[tuple[str, ...], ...] = (
        ("python", "-m", "pytest"),
        ("uv", "run", "pytest"),
    )
    timeout: float = 120.0
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    definition = ToolDefinition(
        "test_runner",
        "Run an allowlisted test command in the workspace.",
        PermissionLevel.READ,
        (
            ToolParameter("command", "list"),
            ToolParameter("timeout", "number", required=False),
            ToolParameter("working_directory", "path", required=False),
        ),
    )

    def __post_init__(self) -> None:
        Tool.__init__(self)
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    def execute(
        self,
        command: list[str],
        timeout: float | None = None,
        working_directory: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        argv = [str(part) for part in command]
        if not argv or not any(
            tuple(argv[: len(prefix)]) == prefix for prefix in self.allowed_prefixes
        ):
            raise ToolError("test command is not allowlisted")
        if any(token in "|;&`$()<>" for token in argv):
            raise ToolError("shell syntax is not allowed")
        limit = self.timeout if timeout is None else float(timeout)
        if limit <= 0:
            raise ToolError("test timeout must be positive")
        cwd = self.root
        if working_directory is not None:
            relative = Path(working_directory)
            if relative.is_absolute() or ".." in relative.parts:
                raise ToolError("test working directory is outside the workspace")
            cwd = (self.root / relative).resolve()
            if self.root not in cwd.parents and cwd != self.root:
                raise ToolError("test working directory is outside the workspace")
            if not cwd.is_dir():
                raise ToolError("test working directory does not exist")
        started = time.monotonic()
        before = self._files()
        try:
            environment = None
            if argv[:2] == ["uv", "run"]:
                if not (cwd / "pyproject.toml").exists() and argv[2:3] == ["pytest"]:
                    argv = [sys.executable, "-m", *argv[2:]]
                environment = dict(os.environ)
                environment.setdefault(
                    "UV_CACHE_DIR",
                    str(Path(tempfile.gettempdir()) / "md-harness-uv-cache"),
                )
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
            return {
                "command": argv,
                "exit_code": result.returncode,
                "passed": result.returncode == 0,
                "stdout": result.stdout[-self.limits.max_output_bytes :],
                "stderr": result.stderr[-self.limits.max_output_bytes :],
                "timed_out": False,
                "duration_seconds": time.monotonic() - started,
                "changed_files": self._changed_files(before),
            }
        except subprocess.TimeoutExpired as error:
            return {
                "command": argv,
                "exit_code": None,
                "passed": False,
                "stdout": str(error.stdout or "")[-self.limits.max_output_bytes :],
                "stderr": str(error.stderr or "")[-self.limits.max_output_bytes :],
                "timed_out": True,
                "duration_seconds": time.monotonic() - started,
                "changed_files": self._changed_files(before),
            }
        except FileNotFoundError as error:
            raise ToolError(f"test program not found: {argv[0]}") from error

    def _files(self) -> set[str]:
        return {
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

    def _changed_files(self, before: set[str]) -> list[str]:
        changed = sorted(self._files() - before)
        if len(changed) > self.limits.max_changed_files:
            raise ToolError("maximum changed file limit exceeded")
        return changed
