from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from pulsara_agent.entities.memory import Preference
from pulsara_agent.event import EventType
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.lifecycle import MemoryLifecycle
from pulsara_agent.memory.recall.service import (
    LexicalMemoryRecallService,
    RecallQuery,
    RecallStatus,
)
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_postgres_lifecycle_supersede_updates_status_and_relation() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    graph = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    lifecycle = MemoryLifecycle(graph=graph, mutable=graph)
    try:
        graph.put_jsonld(
            _preference("preference:old", "The user prefers verbose summaries."),
            graph_id=graph_id,
        )
        graph.put_jsonld(
            _preference("preference:new", "The user prefers concise summaries."),
            graph_id=graph_id,
        )

        events = lifecycle.supersede(
            old_id="preference:old",
            new_id="preference:new",
            governance_batch_id="governance:test:lifecycle",
            graph_id=graph_id,
        )

        old_doc = graph.get_jsonld("preference:old", graph_id=graph_id)
        new_doc = graph.get_jsonld("preference:new", graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.SUPERSEDED.value
        assert {"@id": "preference:old"} in new_doc[memory.SUPERSEDES.name]
        assert events[0].memory_id == "preference:old"
        assert events[0].superseded_by == "preference:new"
    finally:
        graph.delete_graph(graph_id)


def test_postgres_set_status_keeps_graph_document_and_memory_projection_in_sync() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    try:
        store.put_jsonld(
            _preference("preference:status", "The user prefers concise summaries."),
            graph_id=graph_id,
        )

        store.set_status(
            "preference:status",
            memory.NodeStatus.STALE,
            updated_at=datetime.now(timezone.utc),
            graph_id=graph_id,
        )

        document = store.get_jsonld("preference:status", graph_id=graph_id)
        fetched = query.fetch_nodes(["preference:status"], graph_id=graph_id)
        assert document[memory.STATUS.name] == memory.NodeStatus.STALE.value
        assert fetched[0].status is memory.NodeStatus.STALE
    finally:
        store.delete_graph(graph_id)


def test_postgres_lifecycle_superseded_node_is_not_recalled_and_edge_is_materialized() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    lifecycle = MemoryLifecycle(graph=store, mutable=store)
    try:
        store.put_jsonld(
            _preference("preference:old", "The user prefers verbose summaries."),
            graph_id=graph_id,
        )
        store.put_jsonld(
            _preference("preference:new", "The user prefers concise summaries."),
            graph_id=graph_id,
        )
        MemorySearchIndexSync(
            connection_provider=verified_postgres_provider(dsn)
        ).rebuild(graph_id=graph_id)

        lifecycle.supersede(
            old_id="preference:old",
            new_id="preference:new",
            governance_batch_id="governance:test:postgres-supersede",
            graph_id=graph_id,
        )
        MemorySearchIndexSync(
            connection_provider=verified_postgres_provider(dsn)
        ).sync_memory("preference:old", graph_id=graph_id)
        MemorySearchIndexSync(
            connection_provider=verified_postgres_provider(dsn)
        ).sync_memory("preference:new", graph_id=graph_id)

        fetched = query.fetch_nodes(
            ["preference:old", "preference:new"], graph_id=graph_id
        )
        by_id = {view.id: view for view in fetched}
        result = asyncio.run(
            LexicalMemoryRecallService(query).recall(
                RecallQuery(text="summaries preference", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert by_id["preference:old"].status is memory.NodeStatus.SUPERSEDED
        assert (memory.SUPERSEDES.name, "preference:old") in by_id[
            "preference:new"
        ].outgoing
        assert result.status is RecallStatus.OK
        assert [item.memory_id for item in result.items] == ["preference:new"]
    finally:
        store.delete_graph(graph_id)


def test_postgres_lifecycle_link_contradiction_keeps_both_nodes_active_and_adds_edges() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    graph = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    lifecycle = MemoryLifecycle(graph=graph, mutable=graph)
    try:
        graph.put_jsonld(
            _preference("preference:left", "The user prefers verbose summaries."),
            graph_id=graph_id,
        )
        graph.put_jsonld(
            _preference("preference:right", "The user prefers concise summaries."),
            graph_id=graph_id,
        )

        events = lifecycle.link_contradiction(
            left_id="preference:left",
            right_id="preference:right",
            governance_batch_id="governance:test:contradiction",
            graph_id=graph_id,
        )

        left_doc = graph.get_jsonld("preference:left", graph_id=graph_id)
        right_doc = graph.get_jsonld("preference:right", graph_id=graph_id)
        assert left_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert right_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert {"@id": "preference:right"} in left_doc[memory.CONTRADICTS.name]
        assert {"@id": "preference:left"} in right_doc[memory.CONTRADICTS.name]
        assert [event.type for event in events] == [
            EventType.MEMORY_CONTRADICTION_LINKED,
            EventType.MEMORY_CONTRADICTION_LINKED,
        ]
        assert [(event.memory_id, event.contradicts) for event in events] == [
            ("preference:left", "preference:right"),
            ("preference:right", "preference:left"),
        ]
    finally:
        graph.delete_graph(graph_id)


def _preference(memory_id: str, statement: str) -> dict:
    now = utc_now()
    return Preference(
        id=memory_id,
        statement=statement,
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()
