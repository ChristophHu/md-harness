"""Controlled filesystem operations for the harness."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


class FilesystemError(RuntimeError):
    """Raised when a filesystem operation is invalid or fails."""


@dataclass(frozen=True)
class FilesystemTool:
    """Perform filesystem operations below one explicitly allowed root."""

    root: Path
    allow_delete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path = ".") -> Path:
        """Resolve a path and reject paths outside the configured root."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise FilesystemError(f"path outside workspace: {path}") from error
        return resolved

    def exists(self, path: str | Path) -> bool:
        return self.resolve(path).exists()

    def is_file(self, path: str | Path) -> bool:
        return self.resolve(path).is_file()

    def is_dir(self, path: str | Path) -> bool:
        return self.resolve(path).is_dir()

    def mkdir(self, path: str | Path, *, parents: bool = True) -> Path:
        directory = self.resolve(path)
        directory.mkdir(parents=parents, exist_ok=True)
        return directory

    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        file_path = self.resolve(path)
        if not file_path.is_file():
            raise FilesystemError(f"not a file: {path}")
        try:
            return file_path.read_text(encoding=encoding)
        except OSError as error:
            raise FilesystemError(f"could not read file: {path}") from error

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> Path:
        file_path = self.resolve(path)
        if file_path.exists() and not overwrite:
            raise FilesystemError(f"file already exists: {path}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_path.write_text(content, encoding=encoding)
        except OSError as error:
            raise FilesystemError(f"could not write file: {path}") from error
        return file_path

    def list(self, path: str | Path = ".", *, recursive: bool = False) -> list[Path]:
        directory = self.resolve(path)
        if not directory.is_dir():
            raise FilesystemError(f"not a directory: {path}")
        entries = directory.rglob("*") if recursive else directory.iterdir()
        return sorted((entry.relative_to(self.root) for entry in entries), key=str)

    def copy(self, source: str | Path, destination: str | Path) -> Path:
        source_path = self.resolve(source)
        destination_path = self.resolve(destination)
        if not source_path.exists():
            raise FilesystemError(f"source does not exist: {source}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source_path.is_dir():
                shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
            else:
                shutil.copy2(source_path, destination_path)
        except OSError as error:
            raise FilesystemError(f"could not copy {source} to {destination}") from error
        return destination_path

    def move(self, source: str | Path, destination: str | Path) -> Path:
        source_path = self.resolve(source)
        destination_path = self.resolve(destination)
        if not source_path.exists():
            raise FilesystemError(f"source does not exist: {source}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return Path(shutil.move(str(source_path), str(destination_path)))
        except OSError as error:
            raise FilesystemError(f"could not move {source} to {destination}") from error

    def delete(self, path: str | Path, *, recursive: bool = False) -> None:
        if not self.allow_delete:
            raise FilesystemError("delete operations are disabled")
        target = self.resolve(path)
        if not target.exists():
            raise FilesystemError(f"path does not exist: {path}")
        try:
            if target.is_dir():
                if not recursive:
                    target.rmdir()
                else:
                    shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as error:
            raise FilesystemError(f"could not delete: {path}") from error

