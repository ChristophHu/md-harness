from pathlib import Path

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
    with pytest.raises(GitError):
        repo.push()
