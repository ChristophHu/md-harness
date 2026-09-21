from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.tools.git import GitError, GitRepository


def test_init_add_commit_status_branch_and_log(tmp_path):
    repo = GitRepository.init(tmp_path / "repo", "test-repo")
    file_path = repo.path / "README.md"
    file_path.write_text("# Test\n", encoding="utf-8")

    assert repo.name == "test-repo"
    assert "README.md" in repo.status()
    repo.add("README.md")
    repo.commit_staged("initial commit")

    assert repo.status() == ""
    assert repo.current_branch() in {"main", "master"}
    assert "initial commit" in repo.log()


def test_add_selected_files_and_commit(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    (repo.path / "one.txt").write_text("one", encoding="utf-8")
    (repo.path / "two.txt").write_text("two", encoding="utf-8")
    repo.add(Path("one.txt"))
    repo.commit_staged("add one")

    assert "two.txt" in repo.status()
    assert "one.txt" not in repo.status()


def test_branch_checkout_and_remote_listing(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    (repo.path / "file.txt").write_text("content", encoding="utf-8")
    repo.commit("initial")
    repo.checkout("develop", create=True)
    repo.checkout("feature/test", create=True)
    assert repo.current_branch() == "feature/test"
    assert repo.remotes() == ""


def test_clone_local_repository(tmp_path):
    source = GitRepository.init(tmp_path / "source")
    (source.path / "file.txt").write_text("content", encoding="utf-8")
    source.commit("initial")
    clone = GitRepository.clone(str(source.path), tmp_path / "clone")
    assert (clone.path / "file.txt").read_text(encoding="utf-8") == "content"


def test_invalid_operations_raise_errors(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with pytest.raises(ValueError):
        repo.add([])
    with pytest.raises(ValueError):
        repo.commit("")
    with pytest.raises(ValueError):
        repo.checkout("")
    with pytest.raises(ValueError):
        repo.add_remote("", "https://example.test/repo.git")


def test_push_without_branch_is_rejected_for_detached_head(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with patch.object(GitRepository, "current_branch", return_value=""):
        with pytest.raises(GitError):
            repo.push()


def test_push_rejects_missing_current_branch_explicitly(tmp_path):
    repo = GitRepository(tmp_path, "repo")
    with patch.object(GitRepository, "current_branch", return_value=""):
        with pytest.raises(GitError, match="current branch"):
            repo.push()


def test_run_reports_stderr_stdout_and_fallback_errors(tmp_path):
    repo = GitRepository(tmp_path, "repo")
    for stderr, stdout, expected in [
        ("fatal error\n", "", "fatal error"),
        ("", "output error\n", "output error"),
        ("", "", "git status --short failed"),
    ]:
        result = SimpleNamespace(returncode=1, stderr=stderr, stdout=stdout)
        with patch("harness.tools.git.subprocess.run", return_value=result):
            with pytest.raises(GitError, match=expected):
                repo.status()


def test_init_reports_git_failure(tmp_path):
    result = SimpleNamespace(returncode=1, stderr="init failed\n", stdout="")
    with patch("harness.tools.git.subprocess.run", return_value=result):
        with pytest.raises(GitError, match="init failed"):
            GitRepository.init(tmp_path / "repo")


def test_clone_reports_git_failure(tmp_path):
    result = SimpleNamespace(returncode=1, stderr="clone failed\n", stdout="")
    with patch("harness.tools.git.subprocess.run", return_value=result):
        with pytest.raises(GitError, match="clone failed"):
            GitRepository.clone("https://example.test/repo.git", tmp_path / "repo")


def test_log_rejects_non_positive_limit(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with pytest.raises(ValueError, match="positive"):
        repo.log(0)


def test_pull_supports_default_branch_and_rebase(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with patch.object(GitRepository, "_run", return_value="pulled") as run:
        assert repo.pull() == "pulled"
        run.assert_called_once_with("pull", "origin")
    with patch.object(GitRepository, "_run", return_value="pulled") as run:
        repo.pull("upstream", "develop", rebase=True)
        run.assert_called_once_with("pull", "--rebase", "upstream", "develop")


def test_push_supports_explicit_branch_and_upstream(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with patch.object(GitRepository, "_run", return_value="pushed") as run:
        assert repo.push("origin", "feature/test", set_upstream=True) == "pushed"
        run.assert_called_once_with("push", "-u", "origin", "feature/test")


def test_commit_staged_and_remote_operations_validate_and_delegate(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with pytest.raises(ValueError, match="empty"):
        repo.commit_staged(" ")
    with patch.object(GitRepository, "_run", return_value="added") as run:
        assert repo.add_remote("origin", "https://example.test/repo.git") == "added"
        run.assert_called_once_with(
            "remote", "add", "origin", "https://example.test/repo.git"
        )


def test_gitflow_branches_merge_rebase_fetch_and_protection(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with (
        patch.object(GitRepository, "_run", return_value="ok") as run,
        patch.object(GitRepository, "current_branch", return_value="develop"),
    ):
        assert repo.create_branch("feature/task", "develop") == "ok"
        run.assert_called_with("switch", "-c", "feature/task", "develop")
        repo.fetch("upstream", "develop")
        run.assert_called_with("fetch", "upstream", "develop")
        repo.merge("feature/task", no_ff=False)
        run.assert_called_with("merge", "feature/task")
        repo.merge("feature/task")
        run.assert_called_with("merge", "--no-ff", "feature/task")
        repo.rebase("develop")
        run.assert_called_with("rebase", "develop")
        repo.delete_branch("feature/task", remote=True)
        run.assert_called_with("push", "origin", "--delete", "feature/task")
    with pytest.raises(GitError, match="protected"):
        repo.delete_branch("main")
    with pytest.raises(ValueError, match="invalid"):
        repo.delete_branch("bad..branch")
    with pytest.raises(ValueError):
        repo.create_branch("bad branch")
    with pytest.raises(ValueError, match="branch"):
        repo.create_branch("")


def test_review_release_and_branch_helpers(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with patch.object(
        GitRepository,
        "_run",
        side_effect=[
            "a.py\nb.py",
            "stat",
            "show",
            "v1",
            "deleted",
            "v1\nv2",
            "main\nfeature/x",
        ],
    ):
        assert repo.changed_files() == ["a.py", "b.py"]
        assert repo.diff_stat() == "stat"
        assert repo.show() == "show"
        assert repo.tag("v1", "release") == "v1"
        assert repo.delete_tag("v1") == "deleted"
        assert repo.list_tags() == ["v1", "v2"]
        assert repo.list_branches() == ["main", "feature/x"]
    with patch.object(GitRepository, "_run", return_value=""):
        assert repo.check_clean() is True
    with patch.object(GitRepository, "_run", return_value="last"):
        assert repo.last_commit() == "last"
    with patch.object(GitRepository, "_run", return_value="diff") as run:
        assert repo.diff() == "diff"
        run.assert_called_with("diff")
    with patch.object(GitRepository, "_run", return_value="cached") as run:
        assert repo.diff(staged=True) == "cached"
        run.assert_called_with("diff", "--cached")
    with patch.object(GitRepository, "_run", return_value="tagged") as run:
        assert repo.tag("v2") == "tagged"
        run.assert_called_with("tag", "v2")
    with pytest.raises(ValueError):
        repo.show(" ")
    with pytest.raises(ValueError):
        repo.tag("")
    with pytest.raises(ValueError):
        repo.delete_tag("")


def test_execute_dispatches_extended_operations(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with patch.object(GitRepository, "status", return_value="clean"):
        assert repo.execute(operation="status") == "clean"
    with patch.object(GitRepository, "create_branch", return_value="created"):
        assert repo.execute(operation="create_branch", branch="feature/x") == "created"
    with pytest.raises(GitError, match="unsupported"):
        repo.execute(operation="unknown")


def test_execute_dispatches_every_supported_operation(tmp_path, monkeypatch):
    repo = GitRepository.init(tmp_path / "repo")
    methods = {
        "init": "init",
        "add": "add",
        "commit": "commit",
        "commit_staged": "commit_staged",
        "pull": "pull",
        "push": "push",
        "fetch": "fetch",
        "log": "log",
        "current_branch": "branch",
        "checkout": "checkout",
        "create_branch": "create_branch",
        "delete_branch": "delete_branch",
        "merge": "merge",
        "rebase": "rebase",
        "diff": "diff",
        "diff_stat": "diff_stat",
        "changed_files": "changed_files",
        "check_clean": "check_clean",
        "last_commit": "last_commit",
        "show": "show",
        "tag": "tag",
        "delete_tag": "delete_tag",
        "list_tags": "list_tags",
        "list_branches": "list_branches",
        "add_remote": "add_remote",
        "remotes": "remotes",
    }
    for method, result in methods.items():
        monkeypatch.setattr(GitRepository, method, MagicMock(return_value=result))

    cases = [
        ("init", {}),
        ("add", {"paths": ["file"]}),
        ("commit", {"message": "x"}),
        ("commit_staged", {"message": "x"}),
        ("pull", {}),
        ("push", {}),
        ("fetch", {}),
        ("log", {}),
        ("branch", {}),
        ("checkout", {"branch": "feature/x"}),
        ("create_branch", {"branch": "feature/x"}),
        ("delete_branch", {"branch": "feature/x"}),
        ("merge", {"branch": "feature/x"}),
        ("rebase", {"branch": "develop"}),
        ("diff", {}),
        ("diff_stat", {}),
        ("changed_files", {}),
        ("check_clean", {}),
        ("last_commit", {}),
        ("show", {}),
        ("tag", {"name": "v1"}),
        ("delete_tag", {"name": "v1"}),
        ("list_tags", {}),
        ("list_branches", {}),
        ("add_remote", {"remote": "origin", "url": "https://example.test/repo.git"}),
        ("remotes", {}),
    ]
    for operation, arguments in cases:
        assert (
            repo.execute(operation=operation, **arguments)
            == methods["current_branch" if operation == "branch" else operation]
        )


def test_repository_policy_rejects_commit_on_main_after_initial_commit(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    (repo.path / "file.txt").write_text("initial", encoding="utf-8")
    repo.commit("initial")
    (repo.path / "file.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(GitError, match="direct commits"):
        repo.commit("blocked")
