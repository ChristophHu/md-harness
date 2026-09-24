"""Real SQLite and filesystem coverage for project decision projection."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.config import ExecutionConfig
from harness.engine.context_builder import ContextBuilder
from harness.engine.orchestrator import Orchestrator
from harness.engine.result import EngineResult, ResultStatus, WaitReason
from harness.engine.validator import Validator
from harness.knowledge.decision_vault import DecisionVault
from harness.storage.database import connect, initialize_database
from harness.storage.factory import StoreFactory
from harness.storage.project_store import ProjectStore


class DecisionPlanner:
    def plan(self, _context):
        return EngineResult.waiting(
            "Which deployment environment should we use?",
            WaitReason.HUMAN_INPUT,
            interaction_request={
                "kind": "human_decision",
                "prompt": "Which deployment environment should we use?",
                "response_schema": {"type": "string"},
                "request_data": {"scope": "deployment"},
                "resume_action": "replan",
                "required": True,
            },
        )


class UnusedExecutor:
    def execute(self, _context, _plan):
        raise AssertionError("execution is not expected")


def _setup(tmp_path: Path, vault_path: Path, *, db: Path | None = None, planner=None):
    database = db or tmp_path / "harness.sqlite"
    if not database.exists():
        initialize_database(database)
    connection = connect(database)
    stores = StoreFactory.create(connection, tmp_path)
    projects = ProjectStore(connection)
    vault = DecisionVault(connection, vault_path)
    builder = ContextBuilder.from_stores(
        stores, projects, knowledge_loader=vault.for_task
    )
    orchestrator = Orchestrator(
        builder,
        planner=planner or DecisionPlanner(),
        executor=UnusedExecutor(),
        validator=Validator(),
        stores=stores,
        execution_config=ExecutionConfig(persistence_mode="required"),
    )
    orchestrator.decision_vault = vault
    return connection, stores, projects, orchestrator, vault


def _waiting_task(stores, projects, orchestrator, project_id=None):
    project_id = project_id or projects.create(
        "Project", str(stores.artifact_store.workspace)
    )
    task_id = stores.task_store.create("Choose environment", project_id=project_id)
    stores.task_store.transition(task_id, "ready")
    stores.task_store.approve(task_id, "owner")
    result = orchestrator.run(task_id)
    assert result.status is ResultStatus.WAITING
    interactions = orchestrator.list_human_interactions(task_id)
    assert interactions, (result.message, result.wait_reason)
    interaction_id = interactions[0]["interaction_id"]
    return task_id, interaction_id


def test_answer_projects_once_and_suggests_only_within_project(tmp_path):
    vault_path = tmp_path / "vault"
    connection, stores, projects, orchestrator, _ = _setup(tmp_path, vault_path)
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "staging"
    )
    row = connection.execute(
        "SELECT * FROM vault_decision_outbox WHERE interaction_id = ?",
        (interaction_id,),
    ).fetchone()
    assert row["status"] == "published"
    path = (
        vault_path
        / "decisions"
        / "projects"
        / row["project_key"]
        / f"{interaction_id}.md"
    )
    assert path.exists()
    assert "staging" in path.read_text(encoding="utf-8")
    assert not orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "staging"
    )
    assert orchestrator.publish_vault_decisions() == 0
    suggestions = orchestrator.suggest_human_interaction(
        task_id, "Which deployment environment?"
    )
    assert suggestions and suggestions[0]["requires_confirmation"] is True
    assert suggestions[0]["source"] == str(path)
    assert any(
        "staging" in text
        for text in orchestrator.build_context(task_id).knowledge_documents
    )

    second_project = projects.create("Other", str(tmp_path))
    other_task, _ = _waiting_task(stores, projects, orchestrator, second_project)
    assert (
        orchestrator.suggest_human_interaction(
            other_task, "Which deployment environment?"
        )
        == []
    )
    assert orchestrator.list_human_interactions(other_task)[0]["suggestions"] == []
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("status: active", "status: superseded"), encoding="utf-8"
    )
    assert (
        orchestrator.suggest_human_interaction(task_id, "Which deployment environment?")
        == []
    )
    path.write_text(
        original.replace("status: active", "status: active\nvalid_until: 2020-01-01"),
        encoding="utf-8",
    )
    assert (
        orchestrator.suggest_human_interaction(task_id, "Which deployment environment?")
        == []
    )
    connection.close()


def test_failed_publish_retries_after_restart_without_losing_answer(tmp_path):
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("occupied", encoding="utf-8")
    db = tmp_path / "retry.sqlite"
    connection, stores, projects, orchestrator, _ = _setup(
        tmp_path, blocked_root, db=db
    )
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "staging"
    )
    row = connection.execute(
        "SELECT status, attempts FROM vault_decision_outbox WHERE interaction_id = ?",
        (interaction_id,),
    ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1
    connection.execute(
        """UPDATE vault_decision_outbox SET status = 'publishing',
           claim_token = 'crashed-worker', claim_expires_at = '2000-01-01 00:00:00'
           WHERE interaction_id = ?""",
        (interaction_id,),
    )
    connection.commit()
    connection.close()

    connection, _, _, restarted, _ = _setup(tmp_path, tmp_path / "working-vault", db=db)
    assert restarted.publish_vault_decisions() == 1
    row = connection.execute(
        "SELECT status, attempts FROM vault_decision_outbox WHERE interaction_id = ?",
        (interaction_id,),
    ).fetchone()
    assert row["status"] == "published" and row["attempts"] == 2
    connection.close()


def test_potential_secret_blocks_projection_and_preserves_sqlite_answer(tmp_path):
    connection, stores, projects, orchestrator, _ = _setup(tmp_path, tmp_path / "vault")
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "password=never-publish"
    )
    assert (
        connection.execute(
            "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == "blocked"
    )
    assert (
        connection.execute(
            "SELECT status FROM task_human_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == "answered"
    )
    connection.close()


def test_outbox_insert_failure_rolls_back_answer_and_event(tmp_path):
    connection, stores, projects, orchestrator, _ = _setup(tmp_path, tmp_path / "vault")
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    connection.execute(
        """CREATE TRIGGER reject_vault_outbox BEFORE INSERT ON vault_decision_outbox
           BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END"""
    )
    connection.commit()
    with pytest.raises(Exception, match="injected outbox failure"):
        orchestrator.answer_human_interaction(
            task_id, interaction_id, "owner", "staging"
        )
    assert (
        connection.execute(
            "SELECT status FROM task_human_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == "pending"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM task_events WHERE task_id = ? AND event_type = 'task.interaction.answered'",
            (task_id,),
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_project_key_is_generated_and_immutable(tmp_path):
    connection, _, projects, _, _ = _setup(tmp_path, tmp_path / "vault")
    project_id = projects.create("Before", str(tmp_path))
    key = projects.get(project_id)["project_key"]
    assert len(key) == 32
    connection.execute("UPDATE projects SET name = 'After' WHERE id = ?", (project_id,))
    assert projects.get(project_id)["project_key"] == key
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE projects SET project_key = 'changed' WHERE id = ?", (project_id,)
        )
    connection.rollback()
    connection.execute(
        "INSERT INTO projects (name, path) VALUES ('Raw', ?)", (str(tmp_path),)
    )
    assert projects.list()[-1]["project_key"] is not None
    connection.close()


def test_information_request_needs_explicit_reusable_flag(tmp_path):
    class InformationPlanner(DecisionPlanner):
        def plan(self, context):
            result = super().plan(context)
            result.interaction_request["kind"] = "information_request"
            return result

    connection, stores, projects, orchestrator, _ = _setup(
        tmp_path, tmp_path / "vault", planner=InformationPlanner()
    )
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "staging"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == 0
    )
    connection.close()


@pytest.mark.parametrize("kind", ["human_decision", "information_request"])
def test_reusable_answer_reaches_next_task_planner_after_restart(tmp_path, kind):
    class FirstPlanner(DecisionPlanner):
        def plan(self, context):
            result = super().plan(context)
            result.interaction_request["kind"] = kind
            if kind == "information_request":
                result.interaction_request["request_data"]["reusable"] = True
            return result

    db = tmp_path / "harness.sqlite"
    root = tmp_path / "vault"
    connection, stores, projects, first, _ = _setup(
        tmp_path, root, db=db, planner=FirstPlanner()
    )
    first_task, interaction_id = _waiting_task(stores, projects, first)
    project_id = stores.task_store.get(first_task)["project_id"]
    assert first.answer_human_interaction(
        first_task, interaction_id, "owner", "staging"
    )
    assert (
        connection.execute(
            "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        == "published"
    )
    connection.close()

    class SecondPlanner(DecisionPlanner):
        def __init__(self):
            self.contexts = []

        def plan(self, context):
            self.contexts.append(context)
            return super().plan(context)

    planner = SecondPlanner()
    connection, stores, projects, restarted, _ = _setup(
        tmp_path, root, db=db, planner=planner
    )
    second_task, _ = _waiting_task(stores, projects, restarted, project_id)
    assert len(planner.contexts) == 1
    assert any(
        "staging" in document and interaction_id in document
        for document in planner.contexts[0].knowledge_documents
    )
    pending = restarted.list_human_interactions(second_task)[0]
    assert pending["suggestions"]
    assert interaction_id in pending["suggestions"][0]["source"]
    assert pending["suggestions"][0]["requires_confirmation"] is True
    connection.close()


def _queued_decision(tmp_path, *, vault_path=None):
    """Leave an answered interaction in the outbox without publishing it."""
    unavailable = tmp_path / "unavailable-vault"
    unavailable.write_text("occupied", encoding="utf-8")
    connection, stores, projects, orchestrator, _ = _setup(tmp_path, unavailable)
    task_id, interaction_id = _waiting_task(stores, projects, orchestrator)
    assert orchestrator.answer_human_interaction(
        task_id, interaction_id, "owner", "staging"
    )
    project_key = connection.execute(
        "SELECT project_key FROM vault_decision_outbox WHERE interaction_id = ?",
        (interaction_id,),
    ).fetchone()[0]
    connection.close()
    return (
        tmp_path / "harness.sqlite",
        vault_path or tmp_path / "vault",
        interaction_id,
        project_key,
    )


def test_concurrent_writers_only_one_claims_active_lease(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    entered = Event()
    release = Event()

    class PausedWriter(DecisionVault):
        def _write(self, row):
            entered.set()
            assert release.wait(5)
            super()._write(row)

    def first_worker():
        with connect(db) as connection:
            return PausedWriter(connection, root).publish_pending()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_worker)
        assert entered.wait(5)
        with connect(db) as connection:
            assert DecisionVault(connection, root).publish_pending() == 0
        release.set()
        assert first.result(timeout=5) == 1
    with connect(db) as connection:
        row = connection.execute(
            "SELECT status, attempts FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("published", 2)
    assert (
        len(
            list(
                (root / "decisions" / "projects" / project_key).glob(
                    f"{interaction_id}.md"
                )
            )
        )
        == 1
    )


def test_lease_takeover_cannot_let_old_writer_finish_claim(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    entered = Event()
    release = Event()

    class PausedWriter(DecisionVault):
        def _write(self, row):
            entered.set()
            assert release.wait(5)
            super()._write(row)

    def first_worker():
        with connect(db) as connection:
            return PausedWriter(connection, root).publish_pending()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_worker)
        assert entered.wait(5)
        with connect(db) as connection:
            connection.execute(
                "UPDATE vault_decision_outbox SET claim_expires_at = '2000-01-01 00:00:00' WHERE interaction_id = ?",
                (interaction_id,),
            )
            connection.commit()
            assert DecisionVault(connection, root).publish_pending() == 1
        release.set()
        assert first.result(timeout=5) == 0
    path = root / "decisions" / "projects" / project_key / f"{interaction_id}.md"
    assert path.exists()
    with connect(db) as connection:
        row = connection.execute(
            "SELECT status, attempts FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("published", 3)


def test_restart_after_file_link_before_published_status_is_idempotent(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)

    class CrashingWriter(DecisionVault):
        def _finish(self, interaction_id, token, status, error):
            raise RuntimeError("simulated crash after file link")

    with connect(db) as connection:
        with pytest.raises(RuntimeError, match="simulated crash"):
            CrashingWriter(connection, root).publish_pending()
    path = root / "decisions" / "projects" / project_key / f"{interaction_id}.md"
    before = path.read_bytes()
    with connect(db) as connection:
        connection.execute(
            "UPDATE vault_decision_outbox SET claim_expires_at = '2000-01-01 00:00:00' WHERE interaction_id = ?",
            (interaction_id,),
        )
        connection.commit()
        assert DecisionVault(connection, root).publish_pending() == 1
        assert (
            connection.execute(
                "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()[0]
            == "published"
        )
    assert path.read_bytes() == before


def test_changed_existing_note_is_blocked_without_overwrite(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    directory = root / "decisions" / "projects" / project_key
    directory.mkdir(parents=True)
    path = directory / f"{interaction_id}.md"
    path.write_text("manual correction", encoding="utf-8")
    with connect(db) as connection:
        assert DecisionVault(connection, root).publish_pending() == 0
        row = connection.execute(
            "SELECT status, last_error FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert "differs" in row["last_error"]
    assert path.read_text(encoding="utf-8") == "manual correction"


@pytest.mark.parametrize("same_content", [True, False])
def test_file_created_concurrently_at_atomic_link_is_checked(
    tmp_path, monkeypatch, same_content
):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    destination = root / "decisions" / "projects" / project_key / f"{interaction_id}.md"

    def competing_link(source, target):
        assert target == destination
        target.write_bytes(
            source.read_bytes() if same_content else b"manual correction"
        )
        raise FileExistsError("another writer linked the destination first")

    monkeypatch.setattr("harness.knowledge.decision_vault.os.link", competing_link)
    with connect(db) as connection:
        result = DecisionVault(connection, root).publish_pending()
        status = connection.execute(
            "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
    assert result == int(same_content)
    assert status == ("published" if same_content else "blocked")
    assert not list(destination.parent.glob("*.tmp"))


def test_symlinked_project_directory_is_rejected(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "decisions").mkdir(parents=True)
    (root / "decisions" / "projects").symlink_to(outside, target_is_directory=True)
    with connect(db) as connection:
        assert DecisionVault(connection, root).publish_pending() == 0
        assert (
            connection.execute(
                "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()[0]
            == "blocked"
        )
    assert not (outside / project_key).exists()


def test_symlinked_note_is_rejected_without_touching_target(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    directory = root / "decisions" / "projects" / project_key
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("keep me", encoding="utf-8")
    (directory / f"{interaction_id}.md").symlink_to(outside)
    with connect(db) as connection:
        assert DecisionVault(connection, root).publish_pending() == 0
        assert (
            connection.execute(
                "SELECT status FROM vault_decision_outbox WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()[0]
            == "blocked"
        )
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_malformed_vault_documents_are_skipped(tmp_path):
    db, root, interaction_id, project_key = _queued_decision(tmp_path)
    with connect(db) as connection:
        vault = DecisionVault(connection, root)
        assert vault.publish_pending() == 1
        directory = root / "decisions" / "projects" / project_key
        (directory / "no-frontmatter.md").write_text("plain text", encoding="utf-8")
        (directory / "unterminated.md").write_text("---\nid: broken", encoding="utf-8")
        (directory / "bad-yaml.md").write_text("---\nid: [\n---\n", encoding="utf-8")
        (directory / "wrong-project.md").write_text(
            "---\nid: wrong\ntype: decision\nstatus: active\nproject_key: another\n---\n",
            encoding="utf-8",
        )
        documents = vault.project_documents(project_key)
        assert len(documents) == 1
        assert documents[0]["metadata"]["source_interaction_id"] == interaction_id
