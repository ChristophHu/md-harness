"""SQLite database lifecycle, migrations and backup helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CURRENT_SCHEMA_VERSION = 17
_STORAGE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = _STORAGE_DIR / "schema.sql"
MIGRATIONS_PATH = _STORAGE_DIR / "migrations"


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS_PATH.glob("[0-9][0-9][0-9]_*.sql"):
        files.append((int(path.name[:3]), path))
    return sorted(files)


def connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def apply_migrations(
    connection: sqlite3.Connection, target: int = CURRENT_SCHEMA_VERSION
) -> int:
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    for version, path in _migration_files():
        if current < version <= target:
            connection.executescript(path.read_text(encoding="utf-8"))
            connection.execute(f"PRAGMA user_version = {version}")
            current = version
    return current


def initialize_database(database_path: str | Path) -> Path:
    path = Path(database_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        apply_migrations(connection)
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    return path


@contextmanager
def transaction(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        yield connection
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    finally:
        connection.close()


def healthcheck(database_path: str | Path) -> bool:
    try:
        with connect(database_path) as connection:
            return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def schema_version(database_path: str | Path) -> int:
    with connect(database_path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def backup_database(database_path: str | Path, destination: str | Path) -> Path:
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as source, sqlite3.connect(target) as backup:
        source.backup(backup)
    return target


def restore_database(backup_path: str | Path, database_path: str | Path) -> Path:
    target = Path(database_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup_path) as source, sqlite3.connect(target) as restored:
        source.backup(restored)
    return target
