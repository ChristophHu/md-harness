"""Validate the current Git branch for a commit or push hook."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness.security.gitflow_policy import GitflowPolicy, GitflowPolicyError


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "commit"
    if action not in {"commit", "push"}:
        raise SystemExit("usage: validate_gitflow.py [commit|push]")
    root = Path(__file__).resolve().parents[1]
    branch = _git(root, "branch", "--show-current").stdout.strip()
    if not branch:
        raise SystemExit("Gitflow validation requires an active branch")
    policy = GitflowPolicy()
    try:
        if action == "commit":
            initial_commit = _git(root, "rev-parse", "--verify", "HEAD").returncode != 0
            policy.validate_commit(branch, initial_commit=initial_commit)
        else:
            policy.validate_push(branch)
    except GitflowPolicyError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
