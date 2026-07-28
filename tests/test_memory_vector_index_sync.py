from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
from threading import get_ident
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory.canonical.vector_index_sync import (
    MemoryVectorIndexSync,
    VectorSyncStatus,
)
from pulsara_agent.ontology import memory
from pulsara_agent.runtime.projection_jobs.surface_handlers import _run_on_owner_loop
from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)


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


def test_vector_surface_reuses_retrieval_owner_loop_from_worker_threads() -> None:
    async def scenario() -> None:
        owner_loop = asyncio.get_running_loop()
        owner_thread_id = get_ident()

        async def probe(value: int) -> tuple[int, int]:
            assert asyncio.get_running_loop() is owner_loop
            return value, get_ident()

        def invoke(value: int) -> tuple[int, int]:
            return _run_on_owner_loop(
                owner_loop,
                probe(value),
                deadline_monotonic=monotonic() + 2.0,
            )

        assert await asyncio.to_thread(invoke, 1) == (1, owner_thread_id)
        assert await asyncio.to_thread(invoke, 2) == (2, owner_thread_id)

    asyncio.run(scenario())


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
            with verified_postgres_provider(dsn).connection(
                lane=PostgresConnectionLane.MEMORY_UOW,
                deadline_monotonic=monotonic() + 20.0,
            ) as connection:
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


def _seed_memory(dsn: str, *, statement: str) -> tuple[str, str]:
    graph_id = f"graph:test/vector-sync/{uuid4().hex}"
    memory_id = f"preference:{uuid4().hex}"
    PostgresGraphStore(connection_provider=verified_postgres_provider(dsn)).put_jsonld(
        Preference(
            id=memory_id,
            statement=statement,
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.VERIFIED,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=utc_now(),
            updated_at=utc_now(),
            gate_reason="test seed",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    return graph_id, memory_id


def _delete_graph(dsn: str, graph_id: str) -> None:
    # The database fixture owns teardown; IDs are unique per test.
    del dsn, graph_id
