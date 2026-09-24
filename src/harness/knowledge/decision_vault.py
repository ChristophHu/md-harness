"""Project-scoped HITL decision projection and read-only suggestions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


class UnsafeDecision(ValueError):
    """A decision cannot safely be published to Markdown."""


_SECRET = re.compile(
    r"(?i)(password|secret|api[_ -]?key|access[_ -]?token|private[_ -]?key|"
    r"bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{12,})"
)
_WORDS = re.compile(r"[\w-]{4,}", re.UNICODE)


class DecisionVault:
    """Project-only Vault adapter; SQLite owns the publication queue."""

    def __init__(self, connection: sqlite3.Connection, root: str | Path) -> None:
        self.connection = connection
        self.root = Path(root).expanduser().resolve()

    def publish_pending(self, *, limit: int = 100) -> int:
        """Retry pending/expired items, recording failures without losing answers."""
        published = 0
        seen: set[str] = set()
        for _ in range(limit):
            claim = self._claim(seen)
            if claim is None:
                break
            interaction_id, token = claim
            seen.add(interaction_id)
            try:
                row = self._source(interaction_id)
                if row is None:
                    raise UnsafeDecision("source interaction is missing")
                self._write(row)
            except UnsafeDecision as error:
                self._finish(interaction_id, token, "blocked", str(error))
            except (OSError, ValueError, yaml.YAMLError) as error:
                self._finish(interaction_id, token, "pending", str(error))
            else:
                if self._finish(interaction_id, token, "published", None):
                    published += 1
        return published

    def _claim(self, seen: set[str]) -> tuple[str, str] | None:
        now = datetime.now(UTC)
        current = now.strftime("%Y-%m-%d %H:%M:%S")
        expiry = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """SELECT interaction_id FROM vault_decision_outbox
                   WHERE status = 'pending' OR
                   (status = 'publishing' AND claim_expires_at < ?)
                   ORDER BY created_at, interaction_id""",
                (current,),
            ).fetchall()
            row = next((r for r in rows if r["interaction_id"] not in seen), None)
            if row is None:
                self.connection.commit()
                return None
            token = str(uuid.uuid4())
            self.connection.execute(
                """UPDATE vault_decision_outbox SET status = 'publishing',
                   claim_token = ?, claim_expires_at = ?, attempts = attempts + 1,
                   last_error = NULL WHERE interaction_id = ?""",
                (token, expiry, row["interaction_id"]),
            )
            self.connection.commit()
            return row["interaction_id"], token
        except Exception:
            self.connection.rollback()
            raise

    def _finish(
        self, interaction_id: str, token: str, status: str, error: str | None
    ) -> bool:
        cursor = self.connection.execute(
            """UPDATE vault_decision_outbox SET status = ?, last_error = ?,
               claim_token = NULL, claim_expires_at = NULL,
               published_at = CASE WHEN ? = 'published' THEN CURRENT_TIMESTAMP ELSE published_at END
               WHERE interaction_id = ? AND claim_token = ?""",
            (status, error, status, interaction_id, token),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def _source(self, interaction_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT i.*, p.project_key, p.name AS project_name
               FROM vault_decision_outbox o
               JOIN task_human_interactions i ON i.interaction_id = o.interaction_id
               JOIN tasks t ON t.id = i.task_id
               JOIN projects p ON p.id = t.project_id AND p.project_key = o.project_key
               WHERE o.interaction_id = ? AND i.status = 'answered'""",
            (interaction_id,),
        ).fetchone()

    def _project_dir(self, project_key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", project_key):
            raise UnsafeDecision("invalid project key")
        base = self.root / "decisions" / "projects"
        target = base / project_key
        if not target.resolve().is_relative_to(self.root):
            raise UnsafeDecision("project path escapes Vault")
        return target

    @staticmethod
    def _safe_text(value: Any) -> str:
        rendered = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if _SECRET.search(rendered):
            raise UnsafeDecision("potential secret in decision; manual review required")
        return rendered

    @staticmethod
    def _create_once(path: Path, content: str) -> None:
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(content)
        except FileExistsError:
            pass

    def _write(self, row: sqlite3.Row) -> None:
        prompt = self._safe_text(row["prompt"])
        request = json.loads(row["request_data"])
        response = json.loads(row["response_data"])
        decision = self._safe_text(response)
        context = self._safe_text(request.get("context", ""))
        rationale = self._safe_text(
            response.get("rationale", "") if isinstance(response, dict) else ""
        )
        scope = self._safe_text(request.get("scope", "project"))
        target = self._project_dir(row["project_key"])
        target.mkdir(parents=True, exist_ok=True)
        self._create_once(target.parent / "projects.md", "# Projektentscheidungen\n")
        self._create_once(
            target / f"{row['project_key']}.md",
            f"# Entscheidungen: {self._safe_text(row['project_name'])}\n",
        )
        metadata = {
            "id": f"decision-{row['interaction_id']}",
            "type": "decision",
            "scope": "project",
            "status": "active",
            "version": 1,
            "project_key": row["project_key"],
            "project_id": self.connection.execute(
                "SELECT project_id FROM tasks WHERE id = ?", (row["task_id"],)
            ).fetchone()[0],
            "source_task_id": row["task_id"],
            "source_interaction_id": row["interaction_id"],
            "kind": row["kind"],
            "reusable": row["kind"] != "plan_review",
            "decided_by": self._safe_text(row["decided_by"]),
            "decided_at": row["decided_at"],
        }
        content = (
            "---\n"
            + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            + "---\n\n## Frage und Kontext\n\n"
            + prompt
            + ("\n\n" + context if context else "")
            + "\n\n## Entscheidung\n\n"
            + decision
            + "\n\n## Begründung und Alternativen\n\n"
            + (rationale or "Nicht angegeben.")
            + "\n\n## Geltungsbereich\n\n"
            + scope
            + "\n\n## Gültigkeit\n\nBis zur ausdrücklichen Ablösung.\n"
        )
        destination = target / f"{row['interaction_id']}.md"
        if destination.is_symlink():
            raise UnsafeDecision("decision path is a symlink")
        if destination.exists():
            existing = destination.read_text(encoding="utf-8")
            if existing != content:
                raise UnsafeDecision(
                    "existing decision differs; manual review required"
                )
            return
        temporary = target / f".{row['interaction_id']}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                file.write(content)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_text(encoding="utf-8") != content:
                    raise UnsafeDecision(
                        "concurrent decision differs; manual review required"
                    )
        finally:
            temporary.unlink(missing_ok=True)

    def project_documents(self, project_key: str) -> list[dict[str, Any]]:
        """Load only active decisions from one validated project directory."""
        directory = self._project_dir(project_key)
        if not directory.is_dir():
            return []
        documents = []
        for path in sorted(directory.glob("*.md")):
            if path.is_symlink() or path.stem == project_key:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not content.startswith("---\n"):
                continue
            end = content.find("\n---\n", 4)
            if end < 0:
                continue
            try:
                metadata = yaml.safe_load(content[4:end])
            except yaml.YAMLError:
                continue
            if (
                not isinstance(metadata, dict)
                or metadata.get("project_key") != project_key
            ):
                continue
            if metadata.get("type") != "decision" or metadata.get("status") != "active":
                continue
            if metadata.get("reusable", True) is not True:
                continue
            valid_until = metadata.get("valid_until")
            if valid_until is not None:
                try:
                    deadline = datetime.fromisoformat(str(valid_until)).date()
                except ValueError:
                    continue
                if deadline < datetime.now(UTC).date():
                    continue
            documents.append(
                {"path": str(path), "content": content[end + 5 :], "metadata": metadata}
            )
        return documents

    def suggest(
        self, project_key: str, question: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Return source-linked suggestions; never authorize a current WAIT."""
        words = set(_WORDS.findall(question.casefold()))
        if not words:
            return []
        matches = []
        for document in self.project_documents(project_key):
            text = document["content"]
            overlap = words & set(_WORDS.findall(text.casefold()))
            score = len(overlap) / len(words)
            if score >= 0.3:
                matches.append(
                    {
                        "source": document["path"],
                        "decision_id": document["metadata"]["id"],
                        "excerpt": text[:500],
                        "score": round(score, 3),
                        "requires_confirmation": True,
                    }
                )
        return sorted(matches, key=lambda item: -item["score"])[:limit]

    def for_task(self, task_id: int) -> list[str]:
        """Provide current-project decisions to the planning context."""
        row = self.connection.execute(
            """SELECT p.project_key FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id = ?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return []
        return [
            f"Source: {item['path']}\n{item['content']}"
            for item in self.project_documents(row[0])
        ]
