import sqlite3

import pytest

from harness.tools.sqlite import SQLiteError, SQLiteTool


def test_tool_validates_timeout_and_connects(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        SQLiteTool(tmp_path / "db.sqlite", timeout=0)
    tool = SQLiteTool(tmp_path / "nested" / "db.sqlite")
    with tool.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert isinstance(connection, sqlite3.Connection)


def test_initialize_and_table_exists(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    tool.initialize("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)")
    assert tool.table_exists("sample")
    assert not tool.table_exists("missing")


def test_execute_fetch_and_parameterized_queries(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, kind TEXT)")
    item_id = tool.execute("INSERT INTO items (name, kind) VALUES (?, ?)", ("one", "a"))
    assert item_id == 1
    tool.execute("INSERT INTO items (name, kind) VALUES (:name, :kind)", {"name": "two", "kind": "b"})
    assert tool.fetch_one("SELECT name FROM items WHERE id = ?", (1,))["name"] == "one"
    assert tool.fetch_one("SELECT * FROM items WHERE id = ?", (99,)) is None
    assert [row["name"] for row in tool.fetch_all("SELECT * FROM items ORDER BY id")] == ["one", "two"]


def test_executemany_inserts_batch(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    assert tool.executemany("INSERT INTO items (name) VALUES (?)", [("one",), ("two",)]) == 2
    assert len(tool.fetch_all("SELECT * FROM items")) == 2


def test_transaction_commits_and_rolls_back(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    with tool.transaction() as connection:
        connection.execute("INSERT INTO items (name) VALUES (?)", ("committed",))
    assert tool.fetch_one("SELECT * FROM items")["name"] == "committed"
    with pytest.raises(SQLiteError, match="transaction failed"):
        with tool.transaction() as connection:
            connection.execute("INSERT INTO items (name) VALUES (?)", ("rolled back",))
            connection.execute("INSERT INTO missing (name) VALUES (?)", ("error",))
    assert len(tool.fetch_all("SELECT * FROM items")) == 1


def test_invalid_operations_are_wrapped(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    with pytest.raises(SQLiteError, match="statement failed"):
        tool.execute("INSERT INTO missing VALUES (1)")
    with pytest.raises(SQLiteError, match="batch statement failed"):
        tool.executemany("INSERT INTO missing VALUES (?)", [(1,)])
    with pytest.raises(SQLiteError, match="query failed"):
        tool.fetch_one("SELECT * FROM missing")
    with pytest.raises(SQLiteError, match="query failed"):
        tool.fetch_all("SELECT * FROM missing")
    with pytest.raises(SQLiteError, match="could not initialize"):
        tool.initialize("not valid sql")


def test_backup_and_vacuum(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    tool.execute("INSERT INTO items (name) VALUES (?)", ("backup",))
    backup = tool.backup(tmp_path / "backups" / "copy.sqlite")
    assert backup.exists()
    copied = SQLiteTool(backup)
    assert copied.fetch_one("SELECT name FROM items")["name"] == "backup"
    tool.vacuum()


def test_backup_and_vacuum_wrap_sqlite_errors(tmp_path):
    tool = SQLiteTool(tmp_path / "db.sqlite")
    tool.initialize("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    invalid_destination = tmp_path / "existing-directory"
    invalid_destination.mkdir()
    with pytest.raises(SQLiteError, match="backup failed"):
        tool.backup(invalid_destination)
    invalid_database = tmp_path / "database-directory"
    invalid_database.mkdir()
    with pytest.raises(SQLiteError, match="vacuum failed"):
        SQLiteTool(invalid_database).vacuum()


def test_default_harness_schema_initializes(tmp_path):
    tool = SQLiteTool(tmp_path / "harness.sqlite")
    tool.initialize()
    assert tool.table_exists("tasks")
