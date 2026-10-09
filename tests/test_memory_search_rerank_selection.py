"""Explicit search ranks its bounded candidate pool before applying the limit."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.recall import (
    MemoryDenseCandidateDisposition, MemoryQueryRow, MemoryRelatedSearchResult,
)
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.memory.scope import (
    CTX_GLOBAL,
    MemoryDomainContext,
    freeze_memory_read_context_binding,
)
from tests.retrieval_fixtures import save_model
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.retrieval.rerank.protocol import RerankResult
from pulsara_agent.settings import LocalSettingsStore


async def _search(
    tmp_path, monkeypatch, *, limit, mode="success", exact_count=None, automatic=False, rank_automatic=False, related=False
):
    io = KernelSessionIO()
    port = KernelMemoryToolPort(
        repository=SimpleNamespace(connection_provider=None),
        session_id="session:rerank-selection",
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("u_local", "transient"),
            host_workspace_id="workspace:rerank-selection",
        ),
        embedding_config=EmbeddingBackendConfig(),
        io_owner=io,
        settings=LocalSettingsStore(tmp_path / "settings.yaml"),
    )
    if mode != "unconfigured" and (not automatic or rank_automatic):
        await save_model(port._settings, "rerank", "test-key", activate=True)
    facts = tuple(
        MemoryQueryRow(
            fact_id=f"memory:{index}",
            memory_domain_id="u_local",
            context_id=CTX_GLOBAL,
            fact_kind=(
                "DECISION"
                if exact_count is not None and index < exact_count
                else "FACT"
            ),
            lifecycle="ACTIVE",
            statement=f"Project Orion observation {index}.",
            recorded_at="2026-01-01T00:00:00Z",
            fact_semantic_digest="sha256:" + f"{index:064x}",
            sparse_rank=index + 1,
        )
        for index in range(40)
    )
    observed = SimpleNamespace(stages=[], candidates=(), documents=(), relations=(), purpose=None)

    def sparse(**kwargs):
        observed.stages.append(kwargs["kind_filter"])
        return tuple(
            fact
            for fact in facts
            if kwargs["kind_filter"] is None
            or fact.fact_kind == kwargs["kind_filter"]
        )[: kwargs["limit"]]

    def canonical(**kwargs):
        observed.candidates = tuple(item.fact_id for item in kwargs["ranked"])
        return tuple(kwargs["ranked"])

    def contradictions(**kwargs):
        observed.relations = kwargs["fact_ids"]
        return ()

    async def embedding_provider():
        return None

    async def rerank(_query, documents, **kwargs):
        observed.purpose = kwargs["purpose"]
        observed.documents = tuple(json.loads(item) for item in documents)
        assert len(kwargs["candidate_ids"]) == len(documents)
        if mode == "failure":
            raise TimeoutError("test rerank failure")
        if mode == "malformed":
            return [RerankResult(0, 1.0)] * len(documents)
        # Promote an item outside the default result limit to first place.
        promoted = min(11, len(documents) - 1)
        indexes = (
            promoted,
            *(index for index in range(len(documents)) if index != promoted),
        )
        return [RerankResult(index, 1.0 / rank) for rank, index in enumerate(indexes, 1)]

    async def rerank_provider():
        return None if mode == "unconfigured" else SimpleNamespace(rerank=rerank)

    monkeypatch.setattr(port._query, "tokenize_query", lambda _: ("orion",))
    monkeypatch.setattr(port._query, "sparse_candidates", sparse)
    monkeypatch.setattr(port._query, "canonical_refetch", canonical)
    monkeypatch.setattr(port._query, "active_contradictions", contradictions)
    monkeypatch.setattr(
        port._query, "related_candidates",
        lambda **kwargs: MemoryRelatedSearchResult(
            facts[:kwargs["limit"]], MemoryDenseCandidateDisposition.NOT_REQUESTED
        ),
    )
    monkeypatch.setattr(port, "_embedding_provider", embedding_provider)
    monkeypatch.setattr(port, "_rerank_provider", rerank_provider)
    arguments = {"query": "Project Orion observations", "limit": limit}
    if exact_count is not None:
        arguments["kind"] = "DECISION"
    if mode == "projection":
        arguments["query"] = "x" * (9 * 1024)
    try:
        if related:
            return await port._related_for_remember(arguments["query"], context_id=CTX_GLOBAL), observed
        if automatic:
            source = await port.freeze_automatic_recall_source(arguments["query"])
            return json.loads(source.variants[0].text), observed
        result = await port._search(arguments)
        assert result.state == "SUCCESS"
        return json.loads(result.content), observed
    finally:
        await port.aclose()
        await io.aclose(deadline_monotonic=monotonic() + 10)


@pytest.mark.parametrize("limit", (1, 5, 25))
def test_rerank_can_promote_a_candidate_outside_the_return_limit(
    tmp_path, monkeypatch, limit
):
    body, observed = asyncio.run(_search(tmp_path, monkeypatch, limit=limit))
    ids = tuple(item["memory_id"] for item in body["memories"])
    expected = ("memory:11", *(f"memory:{index}" for index in range(40) if index != 11))
    assert ids == expected[:limit]
    assert len(observed.candidates) == max(20, limit)
    assert len(observed.documents) == 20
    assert observed.relations == ids
    assert body["retrieval_summary"]["ranking"] == "RERANKED"
    assert observed.purpose == "recall"


@pytest.mark.parametrize("mode", ("unconfigured", "failure", "malformed", "projection"))
def test_rerank_fallback_returns_only_the_requested_rrf_prefix(
    tmp_path, monkeypatch, mode
):
    body, observed = asyncio.run(_search(tmp_path, monkeypatch, limit=5, mode=mode))
    ids = tuple(item["memory_id"] for item in body["memories"])
    assert ids == tuple(f"memory:{index}" for index in range(5))
    assert len(observed.candidates) == 20
    assert observed.relations == ids
    assert body["retrieval_summary"]["ranking"] == "SPARSE_ONLY"
    assert len(observed.documents) == (20 if mode in {"failure", "malformed"} else 0)


@pytest.mark.parametrize("exact_count", (2, 3))
def test_rerank_pool_preserves_requested_kind_priority_and_relaxation_threshold(
    tmp_path, monkeypatch, exact_count
):
    body, observed = asyncio.run(
        _search(tmp_path, monkeypatch, limit=5, exact_count=exact_count)
    )
    if exact_count == 2:
        assert observed.stages == ["DECISION", None]
        assert len(observed.documents) == 20
        assert [item["memory_id"] for item in body["memories"]] == [
            "memory:0", "memory:1", "memory:11", "memory:2", "memory:3"
        ]
        assert [item["filter_match"] for item in body["memories"]] == [
            "EXACT", "EXACT", "KIND_RELAXED", "KIND_RELAXED", "KIND_RELAXED"
        ]
    else:
        # Enough exact matches stop relaxation even if the rerank pool is small.
        assert observed.stages == ["DECISION"]
        assert len(body["memories"]) == 3
        assert all(item["kind"] == "DECISION" for item in body["memories"])
    assert body["retrieval_summary"]["ranking"] == "RERANKED"


def test_automatic_recall_still_selects_five_rrf_results_without_rerank(
    tmp_path, monkeypatch
):
    body, observed = asyncio.run(
        _search(tmp_path, monkeypatch, limit=5, automatic=True)
    )
    ids = tuple(item["memory_id"] for item in body["items"])
    assert ids == tuple(f"memory:{index}" for index in range(5))
    assert observed.candidates == ids
    assert observed.documents == ()
    assert observed.relations == ids


@pytest.mark.parametrize("mode", ("success", "failure", "malformed"))
def test_implicit_recall_ranks_twenty_before_injecting_five(tmp_path, monkeypatch, mode):
    body, observed = asyncio.run(_search(tmp_path, monkeypatch, limit=5, automatic=True, rank_automatic=True, mode=mode))
    ids = tuple(item["memory_id"] for item in body["items"])
    assert len(observed.candidates) == 20
    assert len(observed.documents) == 20
    assert observed.purpose == "recall"
    if mode == "success":
        assert ids == ("memory:11", "memory:0", "memory:1", "memory:2", "memory:3")
    else:
        assert ids == tuple(f"memory:{index}" for index in range(5))
    assert observed.relations == ids


def test_remember_related_rerank_uses_relation_inspection_purpose(tmp_path, monkeypatch):
    (related, coverage), observed = asyncio.run(
        _search(tmp_path, monkeypatch, limit=5, related=True)
    )
    assert observed.purpose == "related_memory"
    assert len(observed.documents) == 20
    assert tuple(item.memory_id for item in related) == ("memory:11", "memory:0", "memory:1")
    assert coverage["rerank"] == "APPLIED"
