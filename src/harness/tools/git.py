"""Controlled Git repository operations for the harness."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.security.gitflow_policy import GitflowPolicy, GitflowPolicyError
from harness.tools.base import PermissionLevel, Tool, ToolDefinition, ToolParameter


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


@dataclass(frozen=True)
class GitRepository(Tool):
    """Repository adapter used by the harness."""

    path: Path
    name: str
    policy: GitflowPolicy = field(default_factory=GitflowPolicy)

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
        if operation == "init":
            return self.init(self.path, self.name)
        if operation == "status":
            return self.status()
        if operation == "add":
            return self.add(arguments.get("paths"))
        if operation == "commit":
            return self.commit(arguments.pop("message"))
        if operation == "commit_staged":
            return self.commit_staged(arguments.pop("message"))
        if operation == "pull":
            return self.pull(arguments.get("remote", "origin"), arguments.get("branch"))
        if operation == "push":
            return self.push(arguments.get("remote", "origin"), arguments.get("branch"))
        if operation == "fetch":
            return self.fetch(
                arguments.get("remote", "origin"), arguments.get("branch")
            )
        if operation == "log":
            return self.log()
        if operation == "branch":
            return self.current_branch()
        if operation == "checkout":
            return self.checkout(
                arguments.pop("branch"), create=arguments.get("create", False)
            )
        if operation == "create_branch":
            return self.create_branch(arguments.pop("branch"), arguments.get("base"))
        if operation == "delete_branch":
            return self.delete_branch(
                arguments.pop("branch"), remote=arguments.get("remote_delete", False)
            )
        if operation == "merge":
            return self.merge(
                arguments.pop("branch"), no_ff=arguments.get("no_ff", True)
            )
        if operation == "rebase":
            return self.rebase(arguments.pop("branch"))
        if operation == "diff":
            return self.diff(staged=arguments.get("staged", False))
        if operation == "diff_stat":
            return self.diff_stat(staged=arguments.get("staged", False))
        if operation == "changed_files":
            return self.changed_files(staged=arguments.get("staged", False))
        if operation == "check_clean":
            return self.check_clean()
        if operation == "last_commit":
            return self.last_commit()
        if operation == "show":
            return self.show(arguments.pop("commit", "HEAD"))
        if operation == "tag":
            return self.tag(arguments.pop("name"), arguments.get("message"))
        if operation == "delete_tag":
            return self.delete_tag(arguments.pop("name"))
        if operation == "list_tags":
            return self.list_tags()
        if operation == "list_branches":
            return self.list_branches(remote=arguments.get("remote_branches", False))
        if operation == "add_remote":
            return self.add_remote(arguments.pop("remote"), arguments.pop("url"))
        if operation == "remotes":
            return self.remotes()
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
    def init(cls, path: str | Path, name: str | None = None) -> GitRepository:
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
    def clone(
        cls, url: str, path: str | Path, name: str | None = None
    ) -> GitRepository:
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
        self._validate_commit_policy()
        self.stage_all()
        return self._run("commit", "-m", message)

    def commit_staged(self, message: str) -> str:
        """Commit already staged changes without staging additional files."""
        if not message.strip():
            raise ValueError("commit message must not be empty")
        self._validate_commit_policy()
        return self._run("commit", "-m", message)

    def pull(
        self, remote: str = "origin", branch: str | None = None, *, rebase: bool = False
    ) -> str:
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
        self._validate_policy(self.policy.validate_push, target)
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
            self._validate_policy(
                self.policy.validate_branch, branch, base=self.current_branch()
            )
            args.append("-c")
        args.append(branch)
        return self._run(*args)

    def fetch(self, remote: str = "origin", branch: str | None = None) -> str:
        args = ["fetch", remote]
        if branch:
            args.append(branch)
        return self._run(*args)

    def create_branch(self, branch: str, base: str | None = None) -> str:
        self._validate_branch(branch)
        self._validate_policy(
            self.policy.validate_branch, branch, base=base or self.current_branch()
        )
        args = ["switch", "-c", branch]
        if base:
            args.append(base)
        return self._run(*args)

    def delete_branch(self, branch: str, *, remote: bool = False) -> str:
        self._validate_branch(branch)
        if branch in {"main", "master", "develop"}:
            raise GitError(f"protected branch cannot be deleted: {branch}")
        return (
            self._run("push", "origin", "--delete", branch)
            if remote
            else self._run("branch", "-d", branch)
        )

    def merge(self, branch: str, *, no_ff: bool = True) -> str:
        self._validate_policy(self.policy.validate_merge, branch, self.current_branch())
        args = ["merge"]
        if no_ff:
            args.append("--no-ff")
        args.append(branch)
        return self._run(*args)

    def rebase(self, branch: str) -> str:
        return self._run("rebase", branch)

    def diff(self, *, staged: bool = False) -> str:
        return self._run("diff", "--cached") if staged else self._run("diff")

    def diff_stat(self, *, staged: bool = False) -> str:
        return (
            self._run("diff", "--stat", "--cached")
            if staged
            else self._run("diff", "--stat")
        )

    def changed_files(self, *, staged: bool = False) -> list[str]:
        output = (
            self._run("diff", "--cached", "--name-only")
            if staged
            else self._run("diff", "--name-only")
        )
        return [line for line in output.splitlines() if line]

    def check_clean(self) -> bool:
        return self.status() == ""

    def last_commit(self) -> str:
        return self._run("log", "-1", "--oneline")

    def show(self, commit: str = "HEAD") -> str:
        if not commit.strip():
            raise ValueError("commit must not be empty")
        return self._run("show", "--stat", "--oneline", commit)

    def tag(self, name: str, message: str | None = None) -> str:
        if not name.strip():
            raise ValueError("tag name must not be empty")
        args = ["tag"]
        if message:
            args.extend(["-a", name, "-m", message])
        else:
            args.append(name)
        return self._run(*args)

    def delete_tag(self, name: str) -> str:
        if not name.strip():
            raise ValueError("tag name must not be empty")
        return self._run("tag", "-d", name)

    def list_tags(self) -> list[str]:
        output = self._run("tag", "--list")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def list_branches(self, *, remote: bool = False) -> list[str]:
        output = self._run("branch", "-a" if remote else "--format=%(refname:short)")
        return [
            line.strip().lstrip("*").strip()
            for line in output.splitlines()
            if line.strip()
        ]

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if not branch.strip():
            raise ValueError("branch must not be empty")
        if branch.startswith(("/", "-")) or ".." in branch or " " in branch:
            raise ValueError("invalid branch name")

    def _validate_commit_policy(self) -> None:
        self._validate_policy(
            self.policy.validate_commit,
            self.current_branch(),
            initial_commit=self._is_initial_commit(),
        )

    def _is_initial_commit(self) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode != 0

    @staticmethod
    def _validate_policy(callback: Any, *args: Any, **kwargs: Any) -> None:
        try:
            callback(*args, **kwargs)
        except GitflowPolicyError as error:
            raise GitError(str(error)) from error
