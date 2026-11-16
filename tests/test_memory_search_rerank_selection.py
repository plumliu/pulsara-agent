"""Mixed recall ranks twenty full candidates and selects five canonical values."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from time import monotonic
import pytest
from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.recall import (
    MemoryQueryRow,
    PostgresMemoryQuery,
)
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.conversation_kernel.history_excerpt import HistoryCandidate
from pulsara_agent.memory.scope import (
    MemoryDomainContext,
    freeze_memory_read_context_binding,
    CTX_GLOBAL,
)
from pulsara_agent.retrieval.config import EmbeddingBackendConfig


def records(count):
    return tuple(
        MemoryQueryRow(
            random_id("memory"),
            "test",
            CTX_GLOBAL,
            "FACT",
            f"title {i}",
            "字" * 8000,
            "2026-01-01T00:00:00.123456Z",
            "2026-01-01T00:00:00.123456Z",
            sparse_rank=i + 1,
        )
        for i in range(count)
    )


def histories(count):
    return tuple(
        HistoryCandidate(
            random_id("session"),
            random_id("entry"),
            f"history {i}",
            100 - i,
            {},
            (
                {
                    "entry_id": random_id("entry"),
                    "role": "assistant",
                    "label": "回答",
                    "at": "2026-01-01",
                    "text": f"Answer {i}",
                },
            ),
            100,
            "QA",
            False,
        )
        for i in range(count)
    )


@pytest.mark.parametrize(
    "memory_count,history_count,applied,expected_memory,expected_history",
    [
        (20, 5, False, 4, 1),
        (20, 0, False, 5, 0),
        (0, 8, False, 0, 5),
        (20, 5, True, 0, 5),
    ],
)
def test_mixed_pool_twenty_then_five_elastic_selection(
    tmp_path,
    monkeypatch,
    memory_count,
    history_count,
    applied,
    expected_memory,
    expected_history,
):
    async def run():
        io = KernelSessionIO()
        binding = freeze_memory_read_context_binding(
            domain=MemoryDomainContext("test", "transient"),
            host_workspace_id=random_id("workspace"),
        )
        settings = SimpleNamespace(
            read=lambda: SimpleNamespace(
                memory_retrieval=SimpleNamespace(
                    history_enabled=True, embedding_enabled=False, ranking_mode="off"
                )
            )
        )
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None),
            session_id=random_id("session"),
            read_binding=binding,
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=settings,
        )
        memory = records(memory_count)
        history = histories(history_count)
        port._query = SimpleNamespace(
            sparse_candidates=lambda **_: memory,
            fuse_candidates=PostgresMemoryQuery.fuse_candidates,
            canonical_refetch=lambda **kw: kw["ranked"],
        )
        port._history = SimpleNamespace(
            automatic_candidates=lambda **_: history,
            canonical_refetch=lambda **kw: kw["candidates"],
        )

        async def embedding(*_, **__):
            return None

        async def rank(query, result, **kwargs):
            assert len(result.facts) == min(20, memory_count + history_count)
            assert sum(isinstance(x, HistoryCandidate) for x in result.facts) == min(
                history_count, 20 - min(memory_count, 15)
            )
            assert all(
                len(x.statement) == 8000
                for x in result.facts
                if isinstance(x, MemoryQueryRow)
            )
            return replace(
                result,
                facts=tuple(reversed(result.facts)) if applied else result.facts,
                rerank_disposition="APPLIED" if applied else "NOT_APPLIED",
            )

        port._query_embedding = embedding
        port._rerank_explicit = rank
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.memory_tools.session_content_range",
            lambda _: None,
        )
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.memory_tools.FrozenHistoryReadScope",
            lambda *args: args,
        )
        try:
            values = await port._mixed_candidates(
                "ordinary project question",
                SimpleNamespace(canonical_input=SimpleNamespace(items=())),
            )
            assert len(values) == 5
            assert (
                sum(x["target"]["kind"] == "MEMORY" for x in values) == expected_memory
            )
            assert (
                sum(x["target"]["kind"] == "SESSION_ENTRY" for x in values)
                == expected_history
            )
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(run())


def test_rerank_cancel_waits_for_owned_provider_cleanup(tmp_path):
    async def run():
        io = KernelSessionIO()
        binding = freeze_memory_read_context_binding(
            domain=MemoryDomainContext("test", "transient"),
            host_workspace_id=random_id("workspace"),
        )
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None),
            session_id=random_id("session"),
            read_binding=binding,
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=SimpleNamespace(),
        )
        started = asyncio.Event()
        finished = asyncio.Event()

        async def remote():
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                finished.set()

        task = asyncio.create_task(
            port._run_remote_exact(
                remote(), timeout_seconds=30, name="memory-cancel-test"
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not port._remote_tasks
        await port.aclose()
        await io.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(run())


def test_optional_dense_failure_keeps_complete_lexical_results(tmp_path):
    async def run():
        io = KernelSessionIO()
        binding = freeze_memory_read_context_binding(
            domain=MemoryDomainContext("test", "transient"),
            host_workspace_id=random_id("workspace"),
        )
        settings = SimpleNamespace(
            read=lambda: SimpleNamespace(
                memory_retrieval=SimpleNamespace(
                    history_enabled=False, embedding_enabled=False, ranking_mode="off"
                )
            )
        )
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None),
            session_id=random_id("session"),
            read_binding=binding,
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=settings,
        )
        values = records(25)
        connection = SimpleNamespace(embedding_contract="test")

        async def embedding(*args, **kwargs):
            return SimpleNamespace(connection=connection, vector=(1.0,))

        def dense(**kwargs):
            raise OSError("dense channel unavailable")

        port._query_embedding = embedding
        port.embedding_connection = lambda: connection
        port._query = SimpleNamespace(
            sparse_candidates=lambda **kwargs: values[: kwargs["limit"]],
            dense_candidates=dense,
            fuse_candidates=PostgresMemoryQuery.fuse_candidates,
            canonical_refetch=lambda **kwargs: kwargs["ranked"],
        )
        try:
            found = await port._search_memories("query", limit=23)
            assert len(found) == 23 and all(
                len(record.statement) == 8000 for record in found
            )
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(run())
