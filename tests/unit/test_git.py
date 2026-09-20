from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    repo.checkout("feature", create=True)
    assert repo.current_branch() == "feature"
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
        assert repo.push("origin", "develop", set_upstream=True) == "pushed"
        run.assert_called_once_with("push", "-u", "origin", "develop")


def test_commit_staged_and_remote_operations_validate_and_delegate(tmp_path):
    repo = GitRepository.init(tmp_path / "repo")
    with pytest.raises(ValueError, match="empty"):
        repo.commit_staged(" ")
    with patch.object(GitRepository, "_run", return_value="added") as run:
        assert repo.add_remote("origin", "https://example.test/repo.git") == "added"
        run.assert_called_once_with("remote", "add", "origin", "https://example.test/repo.git")
