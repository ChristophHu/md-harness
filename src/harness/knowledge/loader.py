"""Read-only loading of local Markdown knowledge sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml


class KnowledgeError(RuntimeError):
    """Raised when a knowledge source or document is invalid."""


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    """One configured source of Markdown knowledge."""

    name: str
    path: Path
    source_type: str = "local_markdown"
    enabled: bool = True
    read_only: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())


@dataclass(slots=True)
class KnowledgeDocument:
    """Markdown content and machine-readable document metadata."""

    path: str
    content: str
    document_id: str | None = None
    document_type: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class KnowledgeLoader:
    """Load Markdown documents from enabled, read-only local sources."""

    EXCLUDED_NAMES: ClassVar[set[str]] = {".env", ".env.local"}
    EXCLUDED_PARTS: ClassVar[set[str]] = {"state", "artifacts", "logs"}

    def __init__(self, sources: list[KnowledgeSource]) -> None:
        self.sources = sources

    def load_for_task(
        self,
        task_id: int,
        *,
        task_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[KnowledgeDocument]:
        """Load documents matching optional task metadata."""
        if task_id < 1:
            raise KnowledgeError("task_id must be positive")
        return self.load(document_type=task_type, tags=tags)

    def load(
        self,
        *,
        paths: list[str] | None = None,
        document_id: str | None = None,
        document_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[KnowledgeDocument]:
        """Load and filter Markdown documents from enabled local sources."""
        documents: list[KnowledgeDocument] = []
        for source in self.sources:
            if not source.enabled:
                continue
            if source.source_type != "local_markdown":
                raise KnowledgeError(
                    f"unsupported knowledge source: {source.source_type}"
                )
            if not source.read_only:
                raise KnowledgeError(
                    f"knowledge source must be read-only: {source.name}"
                )
            documents.extend(self._load_source(source, paths))
        return [
            document
            for document in documents
            if self._matches(document, document_id, document_type, tags)
        ]

    def _load_source(
        self, source: KnowledgeSource, paths: list[str] | None
    ) -> list[KnowledgeDocument]:
        if not source.path.is_dir():
            raise KnowledgeError(f"knowledge source does not exist: {source.path}")
        candidates = (
            [source.path / path for path in paths]
            if paths
            else source.path.rglob("*.md")
        )
        documents: list[KnowledgeDocument] = []
        for candidate in sorted(candidates):
            resolved = self._resolve(source, candidate)
            if (
                resolved.is_file()
                and resolved.suffix == ".md"
                and not self._excluded(resolved, source.path)
            ):
                documents.append(self._read_document(resolved, source.path))
        return documents

    def _resolve(self, source: KnowledgeSource, candidate: Path) -> Path:
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(source.path)
        except ValueError as error:
            raise KnowledgeError(
                f"path outside knowledge source: {candidate}"
            ) from error
        return resolved

    def _read_document(self, path: Path, root: Path) -> KnowledgeDocument:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise KnowledgeError(
                f"could not read knowledge document: {path}"
            ) from error
        metadata, content = self._frontmatter(text, path)
        raw_tags = metadata.get("tags", [])
        tags = [raw_tags] if isinstance(raw_tags, str) else list(raw_tags)
        return KnowledgeDocument(
            str(path.relative_to(root)),
            content,
            metadata.get("id"),
            metadata.get("type"),
            tags,
            metadata,
        )

    @staticmethod
    def _frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text
        end = text.find("\n---", 4)
        if end == -1:
            raise KnowledgeError(f"invalid frontmatter: {path}")
        try:
            metadata = yaml.safe_load(text[4:end]) or {}
        except yaml.YAMLError as error:
            raise KnowledgeError(f"invalid frontmatter: {path}") from error
        if not isinstance(metadata, dict):
            raise KnowledgeError(f"frontmatter must be a mapping: {path}")
        return metadata, text[end + 4 :].lstrip("\n")

    @classmethod
    def _excluded(cls, path: Path, root: Path) -> bool:
        relative = path.relative_to(root)
        return path.name in cls.EXCLUDED_NAMES or any(
            part in cls.EXCLUDED_PARTS for part in relative.parts
        )

    @staticmethod
    def _matches(
        document: KnowledgeDocument,
        document_id: str | None,
        document_type: str | None,
        tags: list[str] | None,
    ) -> bool:
        return (
            (document_id is None or document.document_id == document_id)
            and (document_type is None or document.document_type == document_type)
            and (not tags or set(tags).issubset(document.tags))
        )
