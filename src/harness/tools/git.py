"""Controlled Git repository operations for the harness."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from harness.tools.base import PermissionLevel, Tool, ToolDefinition, ToolParameter


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


@dataclass(frozen=True)
class GitRepository(Tool):
    """Repository adapter used by the harness."""

    path: Path
    name: str

    definition = ToolDefinition(
        name="git",
        description="Controlled Git repository operations.",
        permission=PermissionLevel.NETWORK,
        parameters=(
            ToolParameter("operation", "string"),
            ToolParameter("message", "string", required=False),
            ToolParameter("paths", "list[path]", required=False),
            ToolParameter("remote", "string", required=False),
            ToolParameter("branch", "string", required=False),
        ),
    )

    def __post_init__(self) -> None:
        Tool.__init__(self)
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())

    def execute(self, **arguments: Any) -> Any:
        operation = arguments.pop("operation")
        if operation == "status": return self.status()
        if operation == "add": return self.add(arguments.get("paths"))
        if operation == "commit": return self.commit(arguments.pop("message"))
        if operation == "pull": return self.pull(arguments.get("remote", "origin"), arguments.get("branch"))
        if operation == "push": return self.push(arguments.get("remote", "origin"), arguments.get("branch"))
        if operation == "log": return self.log()
        raise GitError(f"unsupported operation: {operation}")

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

    @classmethod
    def init(cls, path: str | Path, name: str | None = None) -> "GitRepository":
        """Create a local directory and initialize it as a Git repository."""
        repository_path = Path(path).expanduser().resolve()
        repository_path.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "init", str(repository_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git init failed")
        return cls(repository_path, name or repository_path.name)

    @classmethod
    def clone(cls, url: str, path: str | Path, name: str | None = None) -> "GitRepository":
        """Clone a remote repository into a local path."""
        repository_path = Path(path).expanduser().resolve()
        result = subprocess.run(
            ["git", "clone", url, str(repository_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git clone failed")
        return cls(repository_path, name or repository_path.name)

    def status(self) -> str:
        """Return the porcelain status of the working tree."""
        return self._run("status", "--short")

    def current_branch(self) -> str:
        """Return the current branch name."""
        return self._run("branch", "--show-current")

    def log(self, limit: int = 10) -> str:
        """Return recent commits in a compact format."""
        if limit < 1:
            raise ValueError("limit must be positive")
        return self._run("log", f"-{limit}", "--oneline")

    def add(self, paths: str | Path | Iterable[str | Path] | None = None) -> None:
        """Stage all changes or only the supplied files and directories."""
        if paths is None:
            self._run("add", "--all")
            return
        if isinstance(paths, (str, Path)):
            paths = [paths]
        normalized = [str(Path(path)) for path in paths]
        if not normalized:
            raise ValueError("at least one path is required")
        self._run("add", "--", *normalized)

    def stage_all(self) -> None:
        """Stage tracked and untracked changes in this repository."""
        self.add()

    def commit(self, message: str) -> str:
        """Create a commit after staging all changes."""
        if not message.strip():
            raise ValueError("commit message must not be empty")
        self.stage_all()
        return self._run("commit", "-m", message)

    def commit_staged(self, message: str) -> str:
        """Commit already staged changes without staging additional files."""
        if not message.strip():
            raise ValueError("commit message must not be empty")
        return self._run("commit", "-m", message)

    def pull(self, remote: str = "origin", branch: str | None = None, *, rebase: bool = False) -> str:
        """Fetch and integrate changes from a remote repository."""
        args = ["pull"]
        if rebase:
            args.append("--rebase")
        args.append(remote)
        if branch:
            args.append(branch)
        return self._run(*args)

    def push(
        self,
        remote: str = "origin",
        branch: str | None = None,
        *,
        set_upstream: bool = False,
    ) -> str:
        """Push commits to a remote repository."""
        target = branch or self.current_branch()
        if not target:
            raise GitError("cannot push without a current branch")
        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.extend([remote, target])
        return self._run(*args)

    def add_remote(self, name: str, url: str) -> str:
        """Add a remote URL."""
        if not name.strip() or not url.strip():
            raise ValueError("remote name and URL must not be empty")
        return self._run("remote", "add", name, url)

    def remotes(self) -> str:
        """List configured remotes."""
        return self._run("remote", "-v")

    def checkout(self, branch: str, *, create: bool = False) -> str:
        """Switch to an existing branch or create a new local branch."""
        if not branch.strip():
            raise ValueError("branch must not be empty")
        args = ["switch"]
        if create:
            args.append("-c")
        args.append(branch)
        return self._run(*args)
