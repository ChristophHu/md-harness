"""Controlled Git repository operations for the harness."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


@dataclass(frozen=True)
class GitRepository:
    """Repository adapter used by the harness."""

    path: Path
    name: str

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise GitError(message or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def status(self) -> str:
        """Return the porcelain status of the working tree."""
        return self._run("status", "--short")

    def stage_all(self) -> None:
        """Stage tracked and untracked changes in this repository."""
        self._run("add", "--all")

    def commit(self, message: str) -> str:
        """Create a commit after staging changes."""
        if not message.strip():
            raise ValueError("commit message must not be empty")
        self.stage_all()
        return self._run("commit", "-m", message)
