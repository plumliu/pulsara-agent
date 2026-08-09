from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.host import HostWorkspaceInput
from pulsara_agent.host.core import HostCore
from tests.support import test_llm_config
from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph.durable_facade import DurableGraphFacade
from pulsara_agent.jsonld import utc_now
from pulsara_agent.ontology import memory
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMemoryMutationOperationKind,
    CanonicalMutationSurface,
)
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources
from pulsara_agent.settings import PulsaraSettings, StorageConfig


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


def test_hostcore_shares_one_projection_service_and_materializes_vector_delivery(
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
            "pulsara_agent.host.production_composition.build_retrieval_runtime_resources",
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
        core = HostCore.production(settings=settings)
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
        try:
            assert first.wiring.runtime_wiring.retrieval_resources is resources
            assert second.wiring.runtime_wiring.retrieval_resources is resources
            graph = first.wiring.runtime_wiring.graph
            assert isinstance(graph, DurableGraphFacade)
            graph.put_jsonld(
                Preference(
                    id=memory_id,
                    statement="Worker materialization",
                    scope="ctx:user",
                    status=memory.NodeStatus.ACTIVE,
                    confidence_level=memory.ConfidenceLevel.HIGH,
                    verification_status=memory.VerificationStatus.USER_CONFIRMED,
                    source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                    gate_reason="projection service integration test",
                ).to_jsonld(),
                graph_id=graph_id,
            )
            assert graph.mutation_writer is not None
            graph.mutation_writer.append_canonical_memory_write_mutation(
                payload={
                    "schema_version": "canonical-memory-write-payload.v2",
                    "mutation_lane": "governed_memory",
                    "dirty_memory_ids": [memory_id],
                    "documents": [],
                },
                graph_id=graph_id,
                operation_id=f"vector-delivery:{uuid4().hex}",
                operation_kind=CanonicalMemoryMutationOperationKind.PREFERENCE,
                requested_surfaces=(CanonicalMutationSurface.VECTOR_INDEX,),
            )
            deadline = asyncio.get_running_loop().time() + 10.0
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
                    "HostCore projection service did not materialize vector delivery"
                )
        finally:
            await core.shutdown()
        assert resources.closed is True
        assert provider.close_calls == 1

    asyncio.run(scenario())
