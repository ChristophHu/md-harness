from pathlib import Path

import pytest

from harness.tools.base import (
    PermissionLevel,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolParameter,
    ToolRegistry,
)
from harness.tools.filesystem import FilesystemError, FilesystemTool
from harness.tools.git import GitError, GitRepository
from harness.tools.sqlite import SQLiteError, SQLiteTool


class EchoTool(Tool):
    definition = ToolDefinition(
        name="echo",
        description="Returns a message.",
        permission=PermissionLevel.READ,
        parameters=(ToolParameter("message", "string"),),
    )

    def execute(self, **arguments):
        return arguments["message"]


def test_tool_context_and_metadata():
    context = ToolContext(workspace="/tmp", dry_run=True, metadata={"run": 1})
    tool = EchoTool(context=context)
    assert tool.tool_name == "echo"
    assert tool.context == context
    assert tool.definition.permission == PermissionLevel.READ


def test_tool_validation_and_registry():
    registry = ToolRegistry()
    tool = EchoTool()
    registry.register(tool)
    assert registry.get("echo") is tool
    assert registry.list()[0].name == "echo"
    assert registry.execute("echo", message="hello") == "hello"
    with pytest.raises(ToolError, match="already registered"):
        registry.register(tool)
    with pytest.raises(ToolError, match="unknown tool"):
        registry.get("missing")
    with pytest.raises(ToolError, match="unknown arguments"):
        registry.execute("echo", message="hello", extra=True)
    with pytest.raises(ToolError, match="missing arguments"):
        registry.execute("echo")


def test_filesystem_tool_dispatches_operations(tmp_path):
    tool = FilesystemTool(tmp_path, allow_delete=True)
    assert tool.tool_name == "filesystem"
    assert tool.execute(operation="write", path="file.txt", content="hello")
    assert tool.execute(operation="read", path="file.txt") == "hello"
    assert tool.execute(operation="exists", path="file.txt") is True
    assert tool.execute(operation="mkdir", path="folder").is_dir()
    assert tool.execute(operation="list") == [Path("file.txt"), Path("folder")]
    tool.execute(operation="copy", path="file.txt", destination="copy.txt")
    tool.execute(operation="move", path="copy.txt", destination="moved.txt")
    tool.execute(operation="delete", path="moved.txt")
    with pytest.raises(FilesystemError, match="unsupported"):
        tool.execute(operation="invalid", path="file.txt")


def test_git_tool_dispatches_operations(tmp_path):
    tool = GitRepository.init(tmp_path / "repo")
    tool.path.joinpath("file.txt").write_text("content")
    assert tool.execute(operation="status")
    tool.execute(operation="add", paths=["file.txt"])
    tool.execute(operation="commit", message="initial")
    assert "initial" in tool.execute(operation="log")
    with pytest.raises(GitError, match="unsupported"):
        tool.execute(operation="invalid")


def test_sqlite_tool_dispatches_operations(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.execute(operation="initialize", sql="ignored")
    tool.execute(
        operation="execute",
        sql="CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)",
    )
    tool.execute(
        operation="execute",
        sql="INSERT INTO items (name) VALUES (?)",
        parameters=("one",),
    )
    assert (
        tool.execute(operation="fetch_one", sql="SELECT name FROM items")["name"]
        == "one"
    )
    assert tool.execute(operation="fetch_all", sql="SELECT * FROM items")
    assert tool.execute(operation="table_exists", table="items") is True
    tool.execute(operation="vacuum")
    with pytest.raises(SQLiteError, match="sql is required"):
        tool.execute(operation="execute")
    with pytest.raises(SQLiteError, match="unsupported"):
        tool.execute(operation="invalid", sql="ignored")


def test_base_abstract_execute_body_is_not_implemented():
    with pytest.raises(NotImplementedError):
        Tool.execute(EchoTool())
