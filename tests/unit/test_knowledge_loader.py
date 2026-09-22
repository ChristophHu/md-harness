"""Tests for read-only Markdown knowledge loading."""

from pathlib import Path

import pytest

from harness.knowledge.loader import KnowledgeError, KnowledgeLoader, KnowledgeSource


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def loader(tmp_path):
    write(
        tmp_path / "workflow.md",
        "---\nid: feature\ntype: workflow\ntags: [python, testing]\n---\n# Workflow\n",
    )
    write(tmp_path / "plain.md", "# Plain\n")
    write(tmp_path / "state" / "ignored.md", "# Ignored\n")
    return KnowledgeLoader([KnowledgeSource("vault", tmp_path)])


def test_loader_reads_frontmatter_and_plain_documents(tmp_path):
    documents = loader(tmp_path).load()

    assert [document.path for document in documents] == ["plain.md", "workflow.md"]
    workflow = documents[1]
    assert workflow.document_id == "feature"
    assert workflow.document_type == "workflow"
    assert workflow.tags == ["python", "testing"]
    assert workflow.content == "# Workflow\n"


def test_loader_filters_and_loads_for_task(tmp_path):
    knowledge = loader(tmp_path)

    assert [d.document_id for d in knowledge.load(document_id="feature")] == ["feature"]
    assert [
        d.document_id
        for d in knowledge.load(document_type="workflow", tags=["testing"])
    ] == ["feature"]
    assert [
        d.document_id for d in knowledge.load_for_task(1, task_type="workflow")
    ] == ["feature"]


def test_loader_rejects_invalid_task_id_and_path_escape(tmp_path):
    knowledge = loader(tmp_path)
    with pytest.raises(KnowledgeError, match="task_id"):
        knowledge.load_for_task(0)
    with pytest.raises(KnowledgeError, match="outside"):
        knowledge.load(paths=["../outside.md"])


def test_loader_rejects_invalid_source_configuration(tmp_path):
    with pytest.raises(KnowledgeError, match="does not exist"):
        KnowledgeLoader([KnowledgeSource("missing", tmp_path / "missing")]).load()
    with pytest.raises(KnowledgeError, match="unsupported"):
        KnowledgeLoader([KnowledgeSource("remote", tmp_path, "remote")]).load()
    with pytest.raises(KnowledgeError, match="read-only"):
        KnowledgeLoader([KnowledgeSource("writable", tmp_path, read_only=False)]).load()


def test_loader_ignores_disabled_sources(tmp_path):
    documents = KnowledgeLoader(
        [KnowledgeSource("disabled", tmp_path / "missing", enabled=False)]
    ).load()

    assert documents == []


def test_loader_rejects_invalid_frontmatter(tmp_path):
    write(tmp_path / "broken.md", "---\ninvalid: [\n---\ncontent")
    with pytest.raises(KnowledgeError, match="invalid frontmatter"):
        loader(tmp_path).load()

    write(tmp_path / "unclosed.md", "---\nid: missing")
    with pytest.raises(KnowledgeError, match="invalid frontmatter"):
        KnowledgeLoader([KnowledgeSource("vault", tmp_path)]).load(
            paths=["unclosed.md"]
        )

    write(tmp_path / "scalar.md", "---\n- item\n---\ncontent")
    with pytest.raises(KnowledgeError, match="mapping"):
        KnowledgeLoader([KnowledgeSource("vault", tmp_path)]).load(paths=["scalar.md"])


def test_loader_reports_unreadable_document(tmp_path):
    (tmp_path / "broken.md").mkdir()
    knowledge = KnowledgeLoader([KnowledgeSource("vault", tmp_path)])

    with pytest.raises(KnowledgeError, match="could not read"):
        knowledge._read_document(tmp_path / "broken.md", tmp_path)
