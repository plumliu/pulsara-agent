from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.host import HostCore, HostWorkspaceInput
from tests.support import test_llm_config
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationLane,
    CanonicalMutationPayload,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceState,
    MutationOutboxWriter,
)
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


class _ClosingProvider:
    model_id = "test-provider"
    dimensions = 3

    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _WorkerEmbeddingProvider(_ClosingProvider):
    dimensions = 1024

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1023 + [1.0]

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]


class _BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0
        self.wake_calls = 0

    async def run(self) -> None:
        self.started.set()
        await self.release.wait()

    def wake(self) -> None:
        self.wake_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1
        self.release.set()


def test_retrieval_resources_share_workers_and_close_exactly_once() -> None:
    async def scenario() -> None:
        embedding = _ClosingProvider()
        rerank = _ClosingProvider()
        worker = _BlockingWorker()
        resources = RetrievalRuntimeResources(
            embedding=embedding,  # type: ignore[arg-type]
            rerank=rerank,  # type: ignore[arg-type]
            close_timeout_seconds=0.1,
        )
        resources.attach_worker(worker)
        resources.start()
        await worker.started.wait()

        resources.wake_workers()
        await resources.aclose()
        await resources.aclose()

        assert resources.closed is True
        assert worker.wake_calls == 1
        assert worker.close_calls == 1
        assert embedding.close_calls == 1
        assert rerank.close_calls == 1

    asyncio.run(scenario())


def test_retrieval_resources_cancel_hung_tasks_with_bounded_shutdown() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()
        resources = RetrievalRuntimeResources(close_timeout_seconds=0.01)

        async def never_finishes() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        resources.create_task(never_finishes(), name="retrieval:hung-test")
        await resources.aclose()

        assert cancelled.is_set()

    asyncio.run(scenario())


def test_hostcore_shares_one_vector_worker_and_materializes_woken_outbox(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        storage = StorageConfig.from_env()
        try:
            psycopg.connect(storage.postgres_dsn).close()
        except psycopg.Error as exc:
            pytest.skip(f"PostgreSQL unavailable: {exc}")
        provider = _WorkerEmbeddingProvider()
        resources = RetrievalRuntimeResources(embedding=provider)  # type: ignore[arg-type]
        monkeypatch.setattr(
            "pulsara_agent.host.core.build_retrieval_runtime_resources",
            lambda _config: resources,
        )
        settings = PulsaraSettings(
            llm=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
            ),
            storage=storage,
        )
        core = HostCore(settings, durable=True)
        domain_id = f"u_vector_worker_{uuid4().hex}"
        first = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
                memory_domain_id=domain_id,
            )
        )
        second = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
                memory_domain_id=domain_id,
            )
        )
        graph_id = first.workspace.memory_domain.graph_id
        memory_id = f"preference:{uuid4().hex}"
        outbox_id = ""
        try:
            assert first.wiring.runtime_wiring.retrieval_resources is resources
            assert second.wiring.runtime_wiring.retrieval_resources is resources
            with psycopg.connect(storage.postgres_dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
                    cursor.execute(
                        "INSERT INTO graph_documents (graph_id, id, type, payload) VALUES (%s, %s, 'Preference', %s)",
                        (
                            graph_id,
                            memory_id,
                            Jsonb(
                                {
                                    "@id": memory_id,
                                    "statement": "Worker materialization",
                                }
                            ),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO memory_nodes (
                            graph_id, id, memory_type, scope, status, statement, created_at, updated_at
                        ) VALUES (%s, %s, 'Preference', 'ctx:user', 'active', 'Worker materialization', now(), now())
                        """,
                        (graph_id, memory_id),
                    )
            outbox_id = MutationOutboxWriter(dsn=storage.postgres_dsn).append_payload(
                CanonicalMutationPayload(
                    mutation_lane=CanonicalMutationLane.GOVERNED_MEMORY,
                    dirty_memory_ids=(memory_id,),
                    surface_apply_status={
                        CanonicalMutationSurface.VECTOR_INDEX.value: CanonicalMutationSurfaceState.PENDING.value
                    },
                ),
                graph_id=graph_id,
                target_entry_key="pool:worker-integration",
                governance_batch_id=f"governance:{uuid4().hex}",
                decision_id=f"decision:{uuid4().hex}",
            )
            resources.wake_workers()
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                with psycopg.connect(storage.postgres_dsn) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM memory_vector_index WHERE graph_id = %s AND memory_id = %s",
                            (graph_id, memory_id),
                        )
                        if cursor.fetchone() == (1,):
                            break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError(
                    "HostCore vector worker did not materialize the outbox mutation"
                )
        finally:
            await core.shutdown()
            with psycopg.connect(storage.postgres_dsn) as connection:
                with connection.cursor() as cursor:
                    if outbox_id:
                        cursor.execute(
                            "DELETE FROM memory_write_outbox WHERE outbox_id = %s",
                            (outbox_id,),
                        )
                    cursor.execute(
                        "DELETE FROM graph_documents WHERE graph_id = %s", (graph_id,)
                    )
                    cursor.execute(
                        "DELETE FROM memory_nodes WHERE graph_id = %s", (graph_id,)
                    )
        assert resources.closed is True
        assert provider.close_calls == 1

    asyncio.run(scenario())
