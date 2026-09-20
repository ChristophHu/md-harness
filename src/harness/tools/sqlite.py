"""Controlled SQLite operations for the harness."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator, Mapping, Sequence

from harness.storage.database import SCHEMA


class SQLiteError(RuntimeError):
    """Raised when a SQLite operation fails."""


@dataclass(frozen=True)
class SQLiteTool:
    """Provide parameterized SQLite operations for one database file."""

    path: Path
    timeout: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def connect(self) -> sqlite3.Connection:
        """Open a configured row-based connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, schema: str = SCHEMA) -> None:
        """Create the database and apply an idempotent schema script."""
        try:
            with self.connect() as connection:
                connection.executescript(schema)
        except sqlite3.Error as error:
            raise SQLiteError("could not initialize database") from error

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and commit or roll back as one transaction."""
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteError("transaction failed") from error
        finally:
            connection.close()

    def execute(
        self,
        sql: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> int:
        """Execute one parameterized statement and return its row id."""
        try:
            with self.connect() as connection:
                cursor = connection.execute(sql, parameters)
                return cursor.lastrowid or 0
        except sqlite3.Error as error:
            raise SQLiteError("statement failed") from error

    def executemany(
        self,
        sql: str,
        parameters: Sequence[Sequence[object]],
    ) -> int:
        """Execute one statement for multiple parameter sets."""
        try:
            with self.connect() as connection:
                cursor = connection.executemany(sql, parameters)
                return cursor.rowcount
        except sqlite3.Error as error:
            raise SQLiteError("batch statement failed") from error

    def fetch_one(
        self,
        sql: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> sqlite3.Row | None:
        """Return the first matching row, or None."""
        try:
            with self.connect() as connection:
                return connection.execute(sql, parameters).fetchone()
        except sqlite3.Error as error:
            raise SQLiteError("query failed") from error

    def fetch_all(
        self,
        sql: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> list[sqlite3.Row]:
        """Return all matching rows."""
        try:
            with self.connect() as connection:
                return connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise SQLiteError("query failed") from error

    def table_exists(self, table: str) -> bool:
        """Check whether a table exists without interpolating user SQL."""
        row = self.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return row is not None

    def vacuum(self) -> None:
        """Compact the database file."""
        try:
            with self.connect() as connection:
                connection.execute("VACUUM")
        except sqlite3.Error as error:
            raise SQLiteError("vacuum failed") from error

    def backup(self, destination: str | Path) -> Path:
        """Create a SQLite backup using SQLite's online backup API."""
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as source, sqlite3.connect(target) as destination_db:
                source.backup(destination_db)
        except sqlite3.Error as error:
            raise SQLiteError("backup failed") from error
        return target
