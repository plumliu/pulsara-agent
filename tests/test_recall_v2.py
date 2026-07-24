from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.query import PostgresMemoryQuery
from pulsara_agent.memory.canonical.vector_index_sync import MemoryVectorIndexSync
from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.memory.recall.dense import DenseCandidateService
from pulsara_agent.memory.recall.graph import GraphCandidateService
from pulsara_agent.memory.recall.hybrid import HybridMemoryRecallService
from pulsara_agent.memory.recall.semantic_rerank import RecallRerankService
from pulsara_agent.memory.recall.service import RecallQuery, RecallStatus, RecallTrigger
from pulsara_agent.memory.recall.sparse import SparseCandidateService
from pulsara_agent.memory.recall.trace import PostgresRecallTraceStore
from pulsara_agent.retrieval.rerank.protocol import RerankResult
from pulsara_agent.retrieval.tokenizer.regex_word_split import RegexWordSplitTokenizer
from pulsara_agent.settings import StorageConfig


class _SemanticEmbeddingProvider:
    model_id = "semantic-fake-v1"
    dimensions = 1024

    def __init__(self, *, fail_queries: bool = False) -> None:
        self.fail_queries = fail_queries
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail_queries and "concise" in text.casefold():
            raise RuntimeError("dense provider down")
        normalized = text.casefold()
        if "cat" in normalized or "feline" in normalized:
            return [1.0, 0.0] + [0.0] * 1022
        return [0.0, 1.0] + [0.0] * 1022

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    async def aclose(self) -> None:
        return None


class _ReverseReranker:
    model_id = "reverse-reranker"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def rerank(self, query, documents, *, instruction=None, top_n=None):
        if self.fail:
            raise RuntimeError("reranker down")
        return [
            RerankResult(
                index=index, score=0.9 if "beta" in document.casefold() else 0.4
            )
            for index, document in enumerate(documents)
        ]

    async def aclose(self) -> None:
        return None


class _SlowEmbeddingProvider(_SemanticEmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SlowReranker(_ReverseReranker):
    async def rerank(self, query, documents, *, instruction=None, top_n=None):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SlowSparseService:
    async def collect(self, query, *, graph_id=None):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_postgres_hybrid_recall_discovers_two_hop_shared_evidence_with_grounded_path() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    seed_id = _seed_memory(dsn, graph_id, "Blue launch checklist alpha.")
    target_id = _seed_memory(dsn, graph_id, "Emergency rollback protocol omega.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO memory_relations (graph_id, source_id, predicate, target_id)
                VALUES (%s, 'evidence:shared-path', 'supports', %s)
                """,
                [(graph_id, seed_id), (graph_id, target_id)],
            )
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    service = HybridMemoryRecallService(
        memory_query=query,
        sparse=SparseCandidateService(
            query, RegexWordSplitTokenizer(min_token_length=2)
        ),
        dense=None,
        reranker=None,
        enable_graph_rerank=False,
        graph_candidates=GraphCandidateService(query),
        trace_store=PostgresRecallTraceStore(
            connection_provider=verified_postgres_provider(dsn)
        ),
    )
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text="Blue launch checklist alpha",
                    scopes=("ctx:user",),
                    limit=5,
                    max_hops=2,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                    session_id="session:graph-path",
                    run_id="run:graph-path",
                    turn_id="turn:graph-path",
                    reply_id="reply:graph-path",
                ),
                graph_id=graph_id,
            )
        )

        by_id = {item.memory_id: item for item in result.items}
        assert by_id[seed_id].direct_match is True
        assert by_id[target_id].direct_match is False
        assert by_id[target_id].hop_count == 2
        assert [step.traversal for step in by_id[target_id].paths[0].steps] == [
            "reverse",
            "forward",
        ]
        assert result.metadata["graph_path_count"] == 1
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT metadata
                    FROM recall_traces
                    WHERE graph_id = %s AND run_id = 'run:graph-path'
                    """,
                    (graph_id,),
                )
                trace_metadata = cursor.fetchone()[0]
        assert trace_metadata["graph_max_hops"] == 2
        assert trace_metadata["graph_path_count"] == 1
        assert trace_metadata["graph_candidate_ids"] == [target_id]
    finally:
        _delete_graph(dsn, graph_id)


def test_hybrid_recall_finds_semantic_only_hit_and_caches_auto_query_embedding() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    cat_id = _seed_memory(dsn, graph_id, "Cats enjoy warm windowsills.")
    _seed_memory(dsn, graph_id, "Database backups run nightly.")
    provider = _SemanticEmbeddingProvider()
    vector_sync = MemoryVectorIndexSync(
        connection_provider=verified_postgres_provider(dsn),
        provider=provider,
        provider_name="fake",
    )
    asyncio.run(vector_sync.rebuild(graph_id=graph_id))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    service = _hybrid(dsn, query, provider=provider)
    recall_query = RecallQuery(
        text="feline resting places",
        scopes=("ctx:user",),
        limit=1,
        run_id="run:semantic-cache",
    )
    calls_after_index = provider.calls
    try:
        first = asyncio.run(service.recall(recall_query, graph_id=graph_id))
        second = asyncio.run(
            service.recall(
                RecallQuery(
                    text=recall_query.text,
                    scopes=recall_query.scopes,
                    limit=recall_query.limit,
                    run_id=recall_query.run_id,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=graph_id,
            )
        )

        assert first.status is RecallStatus.OK
        assert first.items[0].memory_id == cat_id
        assert "vector" in first.items[0].why
        assert first.metadata["dense_query"] == "remote_call"
        assert second.metadata["dense_query"] == "cache_hit"
        assert provider.calls == calls_after_index + 1
    finally:
        _delete_graph(dsn, graph_id)


def test_hybrid_recall_dense_failure_falls_back_to_sparse_with_structured_trace() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    memory_id = _seed_memory(dsn, graph_id, "The user prefers concise summaries.")
    MemorySearchIndexSync(
        connection_provider=verified_postgres_provider(dsn)
    ).sync_memory(memory_id, graph_id=graph_id)
    provider = _SemanticEmbeddingProvider(fail_queries=True)
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    service = _hybrid(
        dsn,
        query,
        provider=provider,
        trace_store=PostgresRecallTraceStore(
            connection_provider=verified_postgres_provider(dsn)
        ),
    )
    session_id = f"runtime:{uuid4().hex}"
    recall_query = RecallQuery(
        text="concise summaries",
        scopes=("ctx:user",),
        trigger=RecallTrigger.EXPLICIT_SEARCH,
        session_id=session_id,
        run_id="run:dense-fallback",
        turn_id="turn:dense-fallback",
        reply_id="reply:dense-fallback",
    )
    try:
        result = asyncio.run(service.recall(recall_query, graph_id=graph_id))

        assert result.status is RecallStatus.OK
        assert result.items[0].memory_id == memory_id
        assert any(
            warning.startswith("dense_degraded:RuntimeError")
            for warning in result.warnings
        )
        assert result.metadata["dense_query"] == "degraded"
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT trigger_kind, metadata FROM recall_traces WHERE graph_id = %s AND session_id = %s",
                    (graph_id, session_id),
                )
                trigger, metadata = cursor.fetchone()
        assert trigger == "explicit_search"
        assert metadata["dense_query"] == "degraded"
        assert memory_id in metadata["lexical_candidate_ids"]
    finally:
        _delete_graph(dsn, graph_id)


def test_cheap_auto_dense_timeout_is_bounded_and_falls_back_to_sparse() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    memory_id = _seed_memory(dsn, graph_id, "The user prefers concise summaries.")
    MemorySearchIndexSync(
        connection_provider=verified_postgres_provider(dsn)
    ).sync_memory(memory_id, graph_id=graph_id)
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    provider = _SlowEmbeddingProvider()
    service = HybridMemoryRecallService(
        memory_query=query,
        sparse=SparseCandidateService(
            query, RegexWordSplitTokenizer(min_token_length=2)
        ),
        dense=DenseCandidateService(
            provider,
            MemoryVectorQuery(verified_postgres_provider(dsn)),
            provider_name="fake",
        ),
        reranker=None,
        auto_dense_timeout_seconds=0.01,
    )
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(text="concise summaries", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.OK
        assert result.items[0].memory_id == memory_id
        assert "dense_degraded:timeout" in result.warnings
        assert result.metadata["dense_query"] == "timeout"
    finally:
        _delete_graph(dsn, graph_id)


def test_cheap_auto_dense_floor_drops_nearest_but_irrelevant_candidates() -> None:
    class LowScoreVectorQuery:
        def candidates(self, **_kwargs):
            return [("memory:weak", 0.42), ("memory:strong", 0.78)]

    provider = _SemanticEmbeddingProvider()
    dense = DenseCandidateService(
        provider=provider,
        vector_query=LowScoreVectorQuery(),  # type: ignore[arg-type]
        provider_name="fake",
        auto_min_score=0.55,
    )

    batch = asyncio.run(dense.collect(RecallQuery(text="unrelated arithmetic")))

    assert [candidate.memory_id for candidate in batch.candidates] == ["memory:strong"]
    assert batch.metadata["dense_below_threshold_ids"] == ["memory:weak"]
    assert batch.metadata["dense_min_score"] == 0.55


def test_explicit_rerank_timeout_obeys_total_deadline_and_falls_back() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    memory_id = _seed_memory(dsn, graph_id, "Project output alpha")
    MemorySearchIndexSync(
        connection_provider=verified_postgres_provider(dsn)
    ).sync_memory(memory_id, graph_id=graph_id)
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    service = HybridMemoryRecallService(
        memory_query=query,
        sparse=SparseCandidateService(
            query, RegexWordSplitTokenizer(min_token_length=2)
        ),
        dense=None,
        reranker=RecallRerankService(_SlowReranker()),  # type: ignore[arg-type]
        explicit_rerank_timeout_seconds=0.01,
        explicit_total_deadline_seconds=0.2,
    )
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text="project output",
                    scopes=("ctx:user",),
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.OK
        assert result.items[0].memory_id == memory_id
        assert "rerank_degraded:timeout" in result.warnings
    finally:
        _delete_graph(dsn, graph_id)


def test_explicit_total_deadline_bounds_slow_candidate_generation() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    service = HybridMemoryRecallService(
        memory_query=PostgresMemoryQuery(
            connection_provider=verified_postgres_provider(dsn)
        ),
        sparse=_SlowSparseService(),  # type: ignore[arg-type]
        dense=None,
        reranker=None,
        explicit_total_deadline_seconds=0.01,
    )

    result = asyncio.run(
        service.recall(
            RecallQuery(text="anything", trigger=RecallTrigger.EXPLICIT_SEARCH)
        )
    )

    assert result.status is RecallStatus.UNAVAILABLE
    assert result.warnings == ("explicit_recall_deadline_exceeded",)


def test_hybrid_preserves_canonical_filter_and_visible_contradiction_pair() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    active_id = _seed_memory(dsn, graph_id, "The user prefers concise summaries.")
    conflict_id = _seed_memory(
        dsn, graph_id, "The user now prefers expansive explanations."
    )
    rejected_id = _seed_memory(
        dsn,
        graph_id,
        "Rejected concise summaries duplicate.",
        status="rejected",
    )
    sync = MemorySearchIndexSync(connection_provider=verified_postgres_provider(dsn))
    for memory_id in (active_id, conflict_id, rejected_id):
        sync.sync_memory(memory_id, graph_id=graph_id)
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_relations (graph_id, source_id, predicate, target_id)
                VALUES (%s, %s, 'contradicts', %s)
                """,
                (graph_id, active_id, conflict_id),
            )
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    service = HybridMemoryRecallService(
        memory_query=query,
        sparse=SparseCandidateService(
            query, RegexWordSplitTokenizer(min_token_length=2)
        ),
        dense=None,
        reranker=None,
        enable_graph_rerank=True,
        graph_candidates=GraphCandidateService(query),
    )
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text="concise summaries",
                    scopes=("ctx:user",),
                    limit=2,
                    max_hops=1,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.OK
        assert {item.memory_id for item in result.items} == {active_id, conflict_id}
        assert rejected_id not in {item.memory_id for item in result.items}
        by_id = {item.memory_id: item for item in result.items}
        assert conflict_id in by_id[active_id].conflicts_with
        assert active_id in by_id[conflict_id].conflicts_with
        assert all("contradiction_warning" in item.why for item in result.items)
    finally:
        _delete_graph(dsn, graph_id)


@pytest.mark.parametrize("rerank_fails", [False, True])
def test_explicit_rerank_reorders_or_falls_back_without_losing_candidates(
    rerank_fails: bool,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/recall-v2/{uuid4().hex}"
    first_id = _seed_memory(dsn, graph_id, "Project output alpha")
    second_id = _seed_memory(dsn, graph_id, "Project output beta")
    sync = MemorySearchIndexSync(connection_provider=verified_postgres_provider(dsn))
    sync.sync_memory(first_id, graph_id=graph_id)
    sync.sync_memory(second_id, graph_id=graph_id)
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    sparse = SparseCandidateService(query, RegexWordSplitTokenizer(min_token_length=2))
    service = HybridMemoryRecallService(
        memory_query=query,
        sparse=sparse,
        dense=None,
        reranker=RecallRerankService(_ReverseReranker(fail=rerank_fails)),  # type: ignore[arg-type]
        enable_graph_rerank=False,
    )
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text="project output",
                    scopes=("ctx:user",),
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                    limit=2,
                ),
                graph_id=graph_id,
            )
        )

        if rerank_fails:
            assert {item.memory_id for item in result.items} == {first_id, second_id}
            assert any(
                warning.startswith("rerank_degraded:RuntimeError")
                for warning in result.warnings
            )
        else:
            assert [item.memory_id for item in result.items] == [second_id]
            assert result.items[0].memory_id == second_id
            assert result.metadata["reranker_model"] == "reverse-reranker"
            assert result.metadata["reranker_below_threshold_ids"] == [first_id]
    finally:
        _delete_graph(dsn, graph_id)


def _hybrid(dsn, query, *, provider, trace_store=None):
    return HybridMemoryRecallService(
        memory_query=query,
        sparse=SparseCandidateService(
            query, RegexWordSplitTokenizer(min_token_length=2)
        ),
        dense=DenseCandidateService(
            provider=provider,
            vector_query=MemoryVectorQuery(verified_postgres_provider(dsn)),
            provider_name="fake",
        ),
        reranker=None,
        trace_store=trace_store,
        enable_graph_rerank=False,
    )


def _seed_memory(
    dsn: str, graph_id: str, statement: str, *, status: str = "active"
) -> str:
    memory_id = f"preference:{uuid4().hex}"
    try:
        connection = psycopg.connect(dsn)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO graph_documents (graph_id, id, type, payload) VALUES (%s, %s, 'Preference', %s)",
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
                ) VALUES (%s, %s, 'Preference', 'ctx:user', %s, %s, now(), now())
                """,
                (graph_id, memory_id, status, statement),
            )
    return memory_id


def _delete_graph(dsn: str, graph_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM recall_traces WHERE graph_id = %s", (graph_id,))
            cursor.execute(
                "DELETE FROM memory_write_outbox WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute(
                "DELETE FROM memory_relations WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute(
                "DELETE FROM graph_documents WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph_id,))
