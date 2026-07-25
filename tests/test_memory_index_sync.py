from __future__ import annotations

from tests.support.postgres import (
    guarded_postgres_test_connection,
    verified_postgres_provider,
)

import asyncio
from uuid import uuid4

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import (
    PostgresMemoryQuery,
)
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.recall.service import (
    LexicalMemoryRecallService,
    RecallQuery,
    RecallStatus,
)
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_memory_search_index_rebuild_populates_fts_candidates() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    sync = MemorySearchIndexSync(connection_provider=verified_postgres_provider(dsn))
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:index-rebuild",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM memory_search_index WHERE graph_id = %s", (graph_id,)
                )

        assert (
            query.fts_candidates(
                query_text="concise summaries",
                scopes=["ctx:user"],
                types=["Preference"],
                limit=5,
                graph_id=graph_id,
            )
            == []
        )

        assert sync.rebuild(graph_id=graph_id) == 1
        assert (
            query.fts_candidates(
                query_text="concise summaries",
                scopes=["ctx:user"],
                types=["Preference"],
                limit=5,
                graph_id=graph_id,
            )[0][0]
            == "preference:index-rebuild"
        )
    finally:
        store.delete_graph(graph_id)


def test_recall_filters_stale_index_hit_through_canonical_fetch() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:stale-index",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.REJECTED,
        )
        _insert_stale_active_index_row(
            dsn,
            graph_id=graph_id,
            memory_id="preference:stale-index",
            statement="The user prefers concise summaries.",
        )

        result = asyncio.run(
            LexicalMemoryRecallService(query).recall(
                RecallQuery(text="concise summaries", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.EMPTY
        assert "preference:stale-index" in result.filtered_ids
    finally:
        store.delete_graph(graph_id)


def _put_preference(
    store: PostgresGraphStore,
    *,
    graph_id: str,
    memory_id: str,
    statement: str,
    status: memory.NodeStatus,
) -> None:
    now = utc_now()
    store.put_jsonld(
        Preference(
            id=memory_id,
            statement=statement,
            scope="ctx:user",
            status=status,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="test",
        ).to_jsonld(),
        graph_id=graph_id,
    )


def _insert_stale_active_index_row(
    dsn: str,
    *,
    graph_id: str,
    memory_id: str,
    statement: str,
) -> None:
    with guarded_postgres_test_connection(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_search_index (
                    graph_id,
                    memory_id,
                    memory_type,
                    scope,
                    status,
                    fts,
                    aliases,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'Preference',
                    'ctx:user',
                    'active',
                    to_tsvector('simple', %s),
                    ARRAY[]::text[],
                    now()
                )
                """,
                (graph_id, memory_id, statement),
            )
