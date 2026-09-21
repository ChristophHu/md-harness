import pytest

from harness.security.gitflow_policy import GitflowPolicy, GitflowPolicyError


def test_policy_allows_valid_branch_origins():
    policy = GitflowPolicy()
    policy.validate_branch("develop", base="main")
    policy.validate_branch("feature/task", base="develop")
    policy.validate_branch("release/v1.0.0", base="develop")
    policy.validate_branch("hotfix/security", base="main")
    policy.validate_branch("main")


def test_policy_rejects_invalid_branch_names_and_origins():
    policy = GitflowPolicy()
    for branch, base in [
        ("feature/task", "main"),
        ("release/v1", "main"),
        ("hotfix/fix", "develop"),
        ("experiment/test", "develop"),
        ("develop", "feature/task"),
    ]:
        with pytest.raises(GitflowPolicyError):
            policy.validate_branch(branch, base=base)
    with pytest.raises(GitflowPolicyError, match="empty"):
        policy.validate_branch("")
    with pytest.raises(GitflowPolicyError, match="stable"):
        policy.validate_branch("main", base="develop")


def test_policy_protects_direct_commits_and_pushes():
    policy = GitflowPolicy()
    policy.validate_commit("main", initial_commit=True)
    policy.validate_commit("feature/task")
    policy.validate_push("feature/task")
    for branch in ("main", "master", "develop"):
        with pytest.raises(GitflowPolicyError):
            policy.validate_commit(branch)
        with pytest.raises(GitflowPolicyError):
            policy.validate_push(branch)


def test_policy_only_allows_gitflow_merges():
    policy = GitflowPolicy()
    policy.validate_merge("feature/task", "develop")
    policy.validate_merge("release/v1", "main")
    policy.validate_merge("release/v1", "develop")
    policy.validate_merge("hotfix/fix", "main")
    policy.validate_merge("hotfix/fix", "develop")
    with pytest.raises(GitflowPolicyError):
        policy.validate_merge("feature/task", "main")
