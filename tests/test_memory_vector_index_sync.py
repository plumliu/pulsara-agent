from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationLane,
    CanonicalMutationPayload,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceState,
    MutationOutboxWriter,
)
from pulsara_agent.memory.canonical.vector_index_sync import (
    MemoryVectorIndexSync,
    VectorSyncStatus,
)
from pulsara_agent.settings import StorageConfig


class _FakeEmbeddingProvider:
    model_id = "fake-embedding-v1"
    dimensions = 1024

    def __init__(self, *, failures: int = 0, blocking: bool = False) -> None:
        self.calls: list[str] = []
        self.failures = failures
        self.blocking = blocking
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        self.started.set()
        if self.blocking:
            await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("embedding unavailable")
        return [0.0] * 1023 + [1.0]

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    async def aclose(self) -> None:
        return None


def test_vector_sync_applies_and_skips_unchanged_hash() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id, memory_id = _seed_memory(dsn, statement="Prefer concise summaries.")
    provider = _FakeEmbeddingProvider()
    sync = MemoryVectorIndexSync(
        connection_provider=verified_postgres_provider(dsn),
        provider=provider,
        provider_name="fake",
    )
    try:
        first = asyncio.run(sync.sync_memory(memory_id, graph_id=graph_id))
        second = asyncio.run(sync.sync_memory(memory_id, graph_id=graph_id))

        assert first.status is VectorSyncStatus.APPLIED
        assert second.status is VectorSyncStatus.SKIPPED
        assert len(provider.calls) == 1
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT embedding_fingerprint, embedded_text_hash, builder_version
                    FROM memory_vector_index WHERE graph_id = %s AND memory_id = %s
                    """,
                    (graph_id, memory_id),
                )
                fingerprint, text_hash, builder_version = cursor.fetchone()
                cursor.execute(
                    "SELECT payload ? 'embedding' FROM graph_documents WHERE graph_id = %s AND id = %s",
                    (graph_id, memory_id),
                )
                assert cursor.fetchone() == (False,)
        assert fingerprint == "fake:fake-embedding-v1:1024"
        assert text_hash == first.embedded_text_hash
        assert builder_version == "memory-embedded-text:v1"
    finally:
        _delete_graph(dsn, graph_id)


def test_vector_sync_remote_call_holds_no_row_lock_and_stale_completion_is_rejected() -> (
    None
):
    async def scenario() -> None:
        dsn = StorageConfig.from_env().postgres_dsn
        graph_id, memory_id = _seed_memory(dsn, statement="Old statement")
        provider = _FakeEmbeddingProvider(blocking=True)
        sync = MemoryVectorIndexSync(
            connection_provider=verified_postgres_provider(dsn),
            provider=provider,
            provider_name="fake",
        )
        try:
            task = asyncio.create_task(sync.sync_memory(memory_id, graph_id=graph_id))
            await provider.started.wait()
            # This update would time out if the remote embedding call held the node row lock.
            with psycopg.connect(dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '250ms'")
                    cursor.execute(
                        "UPDATE memory_nodes SET statement = 'New statement', updated_at = now() WHERE graph_id = %s AND id = %s",
                        (graph_id, memory_id),
                    )
                    cursor.execute(
                        "UPDATE graph_documents SET payload = jsonb_set(payload, '{statement}', %s), updated_at = now() WHERE graph_id = %s AND id = %s",
                        (Jsonb("New statement"), graph_id, memory_id),
                    )
            provider.release.set()
            result = await task

            assert result.status is VectorSyncStatus.STALE
            with psycopg.connect(dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM memory_vector_index WHERE graph_id = %s AND memory_id = %s",
                        (graph_id, memory_id),
                    )
                    assert cursor.fetchone() == (0,)
        finally:
            _delete_graph(dsn, graph_id)

    asyncio.run(scenario())


def test_vector_outbox_claim_retries_failure_and_completes_surface() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id, memory_id = _seed_memory(dsn, statement="Retry vector projection")
    provider = _FakeEmbeddingProvider(failures=1)
    sync = MemoryVectorIndexSync(
        connection_provider=verified_postgres_provider(dsn),
        provider=provider,
        provider_name="fake",
    )
    payload = CanonicalMutationPayload(
        mutation_lane=CanonicalMutationLane.GOVERNED_MEMORY,
        dirty_memory_ids=(memory_id,),
        surface_apply_status={
            CanonicalMutationSurface.VECTOR_INDEX.value: CanonicalMutationSurfaceState.PENDING.value
        },
    )
    outbox_id = MutationOutboxWriter(
        connection_provider=verified_postgres_provider(dsn)
    ).append_payload(
        payload,
        graph_id=graph_id,
        target_entry_key="pool:vector-retry",
        governance_batch_id=f"governance:{uuid4().hex}",
        decision_id=f"decision:{uuid4().hex}",
    )
    try:
        assert asyncio.run(sync.consume_outbox(graph_id=graph_id)) == 0
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM memory_nodes WHERE graph_id = %s AND id = %s",
                    (graph_id, memory_id),
                )
                assert cursor.fetchone() == (1,)
        assert asyncio.run(sync.consume_outbox(graph_id=graph_id)) == 1
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, payload, vector_claim_token FROM memory_write_outbox WHERE outbox_id = %s",
                    (outbox_id,),
                )
                status, stored_payload, claim = cursor.fetchone()
        assert status == "applied"
        assert stored_payload["surface_apply_status"]["vector_index"] == "applied"
        assert claim is None
        assert len(provider.calls) == 2
    finally:
        _delete_graph(dsn, graph_id)


def test_vector_finalize_preserves_concurrent_surface_updates() -> None:
    async def scenario() -> None:
        dsn = StorageConfig.from_env().postgres_dsn
        graph_id, memory_id = _seed_memory(dsn, statement="Concurrent surfaces")
        provider = _FakeEmbeddingProvider(blocking=True)
        sync = MemoryVectorIndexSync(
            connection_provider=verified_postgres_provider(dsn),
            provider=provider,
            provider_name="fake",
        )
        writer = MutationOutboxWriter(
            connection_provider=verified_postgres_provider(dsn)
        )
        payload = CanonicalMutationPayload(
            mutation_lane=CanonicalMutationLane.GOVERNED_MEMORY,
            dirty_memory_ids=(memory_id,),
            surface_apply_status={
                CanonicalMutationSurface.SEARCH_INDEX.value: CanonicalMutationSurfaceState.PENDING.value,
                CanonicalMutationSurface.VECTOR_INDEX.value: CanonicalMutationSurfaceState.PENDING.value,
            },
        )
        outbox_id = writer.append_payload(
            payload,
            graph_id=graph_id,
            target_entry_key="pool:concurrent-surfaces",
            governance_batch_id=f"governance:{uuid4().hex}",
            decision_id=f"decision:{uuid4().hex}",
        )
        try:
            task = asyncio.create_task(sync.consume_outbox(graph_id=graph_id))
            await provider.started.wait()
            await asyncio.to_thread(
                writer.mark_surface_applied,
                outbox_id,
                CanonicalMutationSurface.SEARCH_INDEX.value,
            )
            provider.release.set()
            assert await task == 1
            with psycopg.connect(dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT status, payload FROM memory_write_outbox WHERE outbox_id = %s",
                        (outbox_id,),
                    )
                    status, stored = cursor.fetchone()
            assert status == "applied"
            assert stored["surface_apply_status"] == {
                "search_index": "applied",
                "vector_index": "applied",
            }
        finally:
            _delete_graph(dsn, graph_id)

    asyncio.run(scenario())


def _seed_memory(dsn: str, *, statement: str) -> tuple[str, str]:
    graph_id = f"graph:test/vector-sync/{uuid4().hex}"
    memory_id = f"preference:{uuid4().hex}"
    try:
        connection = psycopg.connect(dsn)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO graph_documents (graph_id, id, type, payload)
                VALUES (%s, %s, 'Preference', %s)
                """,
                (
                    graph_id,
                    memory_id,
                    Jsonb(
                        {
                            "@id": memory_id,
                            "@type": ["Preference"],
                            "statement": statement,
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO memory_nodes (
                    graph_id, id, memory_type, scope, status, statement, created_at, updated_at
                ) VALUES (%s, %s, 'Preference', 'ctx:user', 'active', %s, now(), now())
                """,
                (graph_id, memory_id, statement),
            )
    return graph_id, memory_id


def _delete_graph(dsn: str, graph_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM memory_write_outbox WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute(
                "DELETE FROM graph_documents WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph_id,))
