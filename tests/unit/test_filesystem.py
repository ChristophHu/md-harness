from pathlib import Path
from unittest.mock import patch

import pytest

from harness.tools.filesystem import FilesystemError, FilesystemTool


def test_workspace_is_created_and_paths_are_resolved(tmp_path):
    root = tmp_path / "workspace"
    fs = FilesystemTool(root)
    assert root.is_dir()
    assert fs.resolve("file.txt") == root / "file.txt"
    assert fs.resolve(".") == root


def test_paths_outside_workspace_are_rejected(tmp_path):
    fs = FilesystemTool(tmp_path / "workspace")
    with pytest.raises(FilesystemError, match="outside"):
        fs.resolve("../outside.txt")


def test_file_and_directory_queries(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("a/file.txt", "hello")
    fs.mkdir("empty")
    assert fs.exists("a/file.txt")
    assert fs.is_file("a/file.txt")
    assert fs.is_dir("a")
    assert not fs.exists("missing.txt")


def test_read_and_write_text_with_encoding_and_overwrite(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("note.txt", "Grüße", encoding="utf-8")
    assert fs.read_text("note.txt") == "Grüße"
    with pytest.raises(FilesystemError, match="already exists"):
        fs.write_text("note.txt", "new", overwrite=False)
    with pytest.raises(FilesystemError, match="not a file"):
        fs.read_text("missing.txt")


def test_mkdir_without_parents_rejects_missing_parent(tmp_path):
    fs = FilesystemTool(tmp_path)
    with pytest.raises(FileNotFoundError):
        fs.mkdir("missing/child", parents=False)


def test_list_non_recursive_and_recursive(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("one.txt", "1")
    fs.write_text("nested/two.txt", "2")
    assert fs.list() == [Path("nested"), Path("one.txt")]
    assert fs.list(recursive=True) == [
        Path("nested"),
        Path("nested/two.txt"),
        Path("one.txt"),
    ]
    with pytest.raises(FilesystemError, match="not a directory"):
        fs.list("one.txt")


def test_copy_files_and_directories(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("source/file.txt", "content")
    copied_file = fs.copy("source/file.txt", "copies/file.txt")
    copied_dir = fs.copy("source", "copies/source")
    assert copied_file.read_text() == "content"
    assert (copied_dir / "file.txt").read_text() == "content"
    with pytest.raises(FilesystemError, match="does not exist"):
        fs.copy("missing", "destination")


def test_move_files(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("before.txt", "content")
    moved = fs.move("before.txt", "after.txt")
    assert moved == tmp_path / "after.txt"
    assert fs.read_text("after.txt") == "content"
    with pytest.raises(FilesystemError, match="does not exist"):
        fs.move("missing", "target")


def test_delete_requires_permission_and_supports_files_and_directories(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("file.txt", "content")
    with pytest.raises(FilesystemError, match="disabled"):
        fs.delete("file.txt")
    enabled = FilesystemTool(tmp_path, allow_delete=True)
    enabled.delete("file.txt")
    enabled.write_text("directory/file.txt", "content")
    with pytest.raises(FilesystemError, match="could not delete"):
        enabled.delete("directory")
    enabled.delete("directory", recursive=True)
    assert not enabled.exists("directory")


def test_delete_rejects_missing_path(tmp_path):
    fs = FilesystemTool(tmp_path, allow_delete=True)
    with pytest.raises(FilesystemError, match="does not exist"):
        fs.delete("missing")


def test_copy_and_move_reject_escape_paths(tmp_path):
    fs = FilesystemTool(tmp_path)
    with pytest.raises(FilesystemError, match="outside"):
        fs.copy("../source", "target")
    with pytest.raises(FilesystemError, match="outside"):
        fs.move("source", "../target")


def test_read_write_copy_and_move_wrap_os_errors(tmp_path):
    fs = FilesystemTool(tmp_path)
    fs.write_text("file.txt", "content")
    with patch.object(Path, "read_text", side_effect=OSError("read error")):
        with pytest.raises(FilesystemError, match="could not read"):
            fs.read_text("file.txt")
    with patch.object(Path, "write_text", side_effect=OSError("write error")):
        with pytest.raises(FilesystemError, match="could not write"):
            fs.write_text("other.txt", "content")
    with patch(
        "harness.tools.filesystem.shutil.copy2", side_effect=OSError("copy error")
    ):
        with pytest.raises(FilesystemError, match="could not copy"):
            fs.copy("file.txt", "copy.txt")
    with patch(
        "harness.tools.filesystem.shutil.move", side_effect=OSError("move error")
    ):
        with pytest.raises(FilesystemError, match="could not move"):
            fs.move("file.txt", "moved.txt")
