"""Central Gitflow validation rules for harness-controlled Git operations."""

from __future__ import annotations

from dataclasses import dataclass


class GitflowPolicyError(RuntimeError):
    """Raised when a Git operation violates the configured Gitflow."""


@dataclass(frozen=True)
class GitflowPolicy:
    """Validate branch names and allowed Gitflow transitions."""

    stable_branch: str = "main"
    development_branch: str = "develop"

    def validate_branch(self, branch: str, *, base: str | None = None) -> None:
        if not branch.strip():
            raise GitflowPolicyError("branch must not be empty")
        if branch == self.development_branch:
            if base is not None and base not in {self.stable_branch, "master"}:
                raise GitflowPolicyError(
                    f"{self.development_branch} must start from {self.stable_branch}"
                )
            return
        if branch in {self.stable_branch, "master"}:
            if base is not None:
                raise GitflowPolicyError(
                    "stable branches cannot be created from a feature flow"
                )
            return
        if branch.startswith(("feature/", "release/")):
            self._require_base(branch, base, self.development_branch)
            return
        if branch.startswith("hotfix/"):
            self._require_base(branch, base, self.stable_branch)
            return
        raise GitflowPolicyError(f"unsupported Gitflow branch: {branch}")

    def validate_commit(self, branch: str, *, initial_commit: bool = False) -> None:
        if initial_commit:
            return
        if branch in {self.stable_branch, "master", self.development_branch}:
            raise GitflowPolicyError(
                f"direct commits are not allowed on protected branch: {branch}"
            )
        self.validate_branch(branch)

    def validate_push(self, branch: str) -> None:
        if branch in {self.stable_branch, "master", self.development_branch}:
            raise GitflowPolicyError(
                f"direct pushes are not allowed on protected branch: {branch}"
            )
        self.validate_branch(branch)

    def validate_merge(self, source: str, target: str) -> None:
        valid = (
            (source.startswith("feature/") and target == self.development_branch)
            or (
                source.startswith("release/")
                and target in {self.development_branch, self.stable_branch, "master"}
            )
            or (
                source.startswith("hotfix/")
                and target in {self.development_branch, self.stable_branch, "master"}
            )
        )
        if not valid:
            raise GitflowPolicyError(
                f"Gitflow does not allow merging {source} into {target}"
            )

    @staticmethod
    def _require_base(branch: str, base: str | None, expected: str) -> None:
        if base is not None and base != expected:
            raise GitflowPolicyError(f"{branch} must start from {expected}")
