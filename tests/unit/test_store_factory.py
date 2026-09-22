import sqlite3

import pytest

from harness.config import ConfigError, PersistenceMode
from harness.engine.context_builder import ContextBuilder
from harness.engine.orchestrator import Orchestrator
from harness.storage.factory import StoreFactory


class Builder:
    def build(self, _task_id):
        return object()


def test_store_factory_builds_one_connected_bundle():
    connection = sqlite3.connect(":memory:")
    bundle = StoreFactory.create(connection)

    assert bundle.task_store.connection is connection
    assert bundle.event_store.connection is connection
    assert bundle.artifact_store.connection is connection
    assert bundle.transaction_manager.connection is connection


def test_orchestrator_accepts_complete_store_bundle():
    bundle = StoreFactory.create(sqlite3.connect(":memory:"))
    builder = ContextBuilder.from_stores(bundle, object())
    orchestrator = Orchestrator(
        builder, stores=bundle, persistence_mode=PersistenceMode.REQUIRED
    )

    assert orchestrator.task_store is bundle.task_store
    assert orchestrator.event_store is bundle.event_store


def test_orchestrator_rejects_bundle_with_individual_stores():
    bundle = StoreFactory.create(sqlite3.connect(":memory:"))
    with pytest.raises(ConfigError, match="cannot be combined"):
        Orchestrator(Builder(), stores=bundle, task_store=bundle.task_store)


def test_disabled_mode_rejects_store_bundle():
    bundle = StoreFactory.create(sqlite3.connect(":memory:"))
    with pytest.raises(ConfigError, match="disabled"):
        Orchestrator(
            ContextBuilder.from_stores(bundle, object()),
            stores=bundle,
            persistence_mode=PersistenceMode.DISABLED,
        )


def test_orchestrator_rejects_mismatched_context_builder():
    bundle = StoreFactory.create(sqlite3.connect(":memory:"))
    builder = ContextBuilder(
        bundle.task_store,
        bundle.event_store,
        bundle.artifact_store,
        object(),
    )
    with pytest.raises(ConfigError, match="checkpoint_store"):
        Orchestrator(builder, stores=bundle)
