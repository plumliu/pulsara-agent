"""Explicit search ranks its bounded candidate pool before applying the limit."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.recall import (
    MemoryDenseCandidateDisposition,
    MemoryQueryRow,
    MemoryRelatedSearchResult,
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
    tmp_path,
    monkeypatch,
    *,
    limit,
    mode="success",
    exact_count=None,
    automatic=False,
    rank_automatic=False,
    related=False,
    history_count=0,
    memory_count=40,
    invalidate_history=False,
    timed_out_route=None,
    ranking_mode="rerank",
    scores=None,
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
        await save_model(port._settings, ranking_mode, "test-key", activate=True)
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
        for index in range(memory_count)
    )
    from pulsara_agent.conversation_kernel.history_excerpt import HistoryCandidate
    from pulsara_agent.model_input.contracts import ContextBindingBaseKind

    history = tuple(
        HistoryCandidate(
            session_id=f"session:history:{i}",
            q1_entry_id=f"entry:q:{i}",
            title=f"history {i}",
            score=float(100 - i),
            document={"identity": [i]},
            messages=(
                {
                    "role": "assistant",
                    "label": "助手回答",
                    "at": "2026-01-01",
                    "text": f"Historical Orion observation {i}",
                },
            ),
        )
        for i in range(history_count)
    )

    def route_result(name, value, kwargs):
        if timed_out_route == name:
            from time import sleep

            sleep(max(0, kwargs["deadline_monotonic"] - monotonic()) + 0.01)
            raise TimeoutError(f"{name} query timed out")
        return value

    if timed_out_route is not None:
        monkeypatch.setattr(port, "_canonical_deadline", lambda _: monotonic() + 0.1)
    port._history.automatic_candidates = lambda **kwargs: route_result(
        "history", history, kwargs
    )
    port._history.canonical_refetch = lambda **kwargs: (
        () if invalidate_history else kwargs["candidates"]
    )
    canonical_facts = SimpleNamespace(
        context_binding_fact=SimpleNamespace(
            base_kind=ContextBindingBaseKind.FULL_HISTORY
        ),
        canonical_input=SimpleNamespace(items=()),
    )
    observed = SimpleNamespace(
        stages=[], candidates=(), documents=(), relations=(), purpose=None, calls=0
    )

    def sparse(**kwargs):
        observed.stages.append(kwargs["kind_filter"])
        route_result("sparse", None, kwargs)
        return tuple(
            fact
            for fact in facts
            if kwargs["kind_filter"] is None or fact.fact_kind == kwargs["kind_filter"]
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
        observed.calls += 1
        observed.purpose = kwargs["purpose"]
        observed.documents = tuple(json.loads(item) for item in documents)
        assert len(kwargs["candidate_ids"]) == len(documents)
        if mode == "failure":
            raise TimeoutError("test rerank failure")
        if mode == "malformed":
            return [RerankResult(0, 1.0)] * len(documents)
        if mode == "settings_changed":
            await port._settings.set_retrieval_mode(ranking_mode="off")
        if scores is not None:
            return [RerankResult(index, score) for index, score in enumerate(scores)]
        # Promote an item outside the default result limit to first place.
        promoted = min(11, len(documents) - 1)
        indexes = (
            promoted,
            *(index for index in range(len(documents)) if index != promoted),
        )
        return [
            RerankResult(index, 1.0 / rank) for rank, index in enumerate(indexes, 1)
        ]

    async def rerank_provider():
        return (
            None
            if mode in {"unconfigured", "provider_unavailable"}
            else SimpleNamespace(rerank=rerank)
        )

    monkeypatch.setattr(port._query, "sparse_candidates", sparse)
    monkeypatch.setattr(port._query, "canonical_refetch", canonical)
    monkeypatch.setattr(port._query, "active_contradictions", contradictions)
    monkeypatch.setattr(
        port._query,
        "related_candidates",
        lambda **kwargs: MemoryRelatedSearchResult(
            facts[: kwargs["limit"]], MemoryDenseCandidateDisposition.NOT_REQUESTED
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
            return await port._related_for_remember(
                arguments["query"], context_id=CTX_GLOBAL
            ), observed
        if automatic:
            source = await port.freeze_automatic_recall_source(
                arguments["query"], canonical_facts=canonical_facts
            )
            return (
                {"items": []}
                if not hasattr(source, "variants")
                else json.loads(source.variants[0].text)
            ), observed
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
            "memory:0",
            "memory:1",
            "memory:11",
            "memory:2",
            "memory:3",
        ]
        assert [item["filter_match"] for item in body["memories"]] == [
            "EXACT",
            "EXACT",
            "KIND_RELAXED",
            "KIND_RELAXED",
            "KIND_RELAXED",
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
    assert observed.candidates == tuple(f"memory:{index}" for index in range(20))
    assert observed.documents == ()
    assert observed.relations == ids


@pytest.mark.parametrize("mode", ("success", "failure", "malformed"))
def test_implicit_recall_ranks_twenty_before_injecting_five(
    tmp_path, monkeypatch, mode
):
    body, observed = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            mode=mode,
        )
    )
    ids = tuple(item["memory_id"] for item in body["items"])
    assert len(observed.candidates) == 20
    assert len(observed.documents) == 20
    assert observed.purpose == "automatic_recall"
    if mode == "success":
        assert ids == ("memory:11", "memory:0", "memory:1", "memory:2", "memory:3")
    else:
        assert ids == tuple(f"memory:{index}" for index in range(5))
    assert observed.relations == ids


def test_remember_related_rerank_uses_relation_inspection_purpose(
    tmp_path, monkeypatch
):
    (related, coverage), observed = asyncio.run(
        _search(tmp_path, monkeypatch, limit=5, related=True)
    )
    assert observed.purpose == "related_memory"
    assert len(observed.documents) == 20
    assert tuple(item.memory_id for item in related) == (
        "memory:11",
        "memory:0",
        "memory:1",
    )
    assert coverage["rerank"] == "APPLIED"


@pytest.mark.parametrize(
    "memory_count,history_count,expected_pool",
    [(40, 20, (15, 5)), (2, 20, (2, 18)), (40, 2, (18, 2)), (0, 20, (0, 20))],
)
def test_mixed_pool_is_elastic_and_one_decision_selects_directly(
    tmp_path, monkeypatch, memory_count, history_count, expected_pool
):
    body, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            memory_count=memory_count,
            history_count=history_count,
        )
    )
    assert (
        sum("statement" in x for x in seen.documents),
        sum(x.get("source") == "session_history" for x in seen.documents),
    ) == expected_pool
    assert len(body["items"]) == 5
    for x in body["items"]:
        if x.get("source") == "session_history":
            assert "memory_id" not in x


@pytest.mark.parametrize("mode", ["failure", "malformed", "unconfigured"])
@pytest.mark.parametrize(
    "memory_count,history_count,expected",
    [(40, 20, (4, 1)), (2, 20, (2, 3)), (40, 0, (5, 0)), (0, 20, (0, 5))],
)
def test_mixed_no_decision_fallback_has_elastic_four_plus_one(
    tmp_path, monkeypatch, mode, memory_count, history_count, expected
):
    body, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            mode=mode,
            memory_count=memory_count,
            history_count=history_count,
        )
    )
    assert (
        sum("memory_id" in x for x in body["items"]),
        sum(x.get("source") == "session_history" for x in body["items"]),
    ) == expected


def test_selected_invalid_history_is_omitted_without_final_refill(
    tmp_path, monkeypatch
):
    body, _ = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            memory_count=0,
            history_count=20,
            invalidate_history=True,
        )
    )
    assert body["items"] == []


@pytest.mark.parametrize("timed_out_route", ["history", "sparse"])
@pytest.mark.parametrize("rank_automatic", [False, True])
def test_automatic_route_timeout_preserves_other_source(
    tmp_path, monkeypatch, timed_out_route, rank_automatic
):
    body, observed = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            history_count=1,
            memory_count=1,
            timed_out_route=timed_out_route,
            rank_automatic=rank_automatic,
        )
    )
    assert len(body["items"]) == 1
    assert len(observed.documents) == int(rank_automatic)
    if timed_out_route == "history":
        assert body["items"][0]["memory_id"] == "memory:0"
        assert observed.candidates == ("memory:0",)
    else:
        assert body["items"][0]["source"] == "session_history"


@pytest.mark.parametrize(
    "scores,expected",
    [
        ([0.699, 0.7, 0.95, 0.0, 0.8], ("memory:2", "memory:4", "memory:1")),
        ([0.0, 0.5, 0.699, 0.1, 0.2], ()),
        ([0.8] * 7, tuple(f"memory:{i}" for i in range(5))),
    ],
)
def test_implicit_decision_admission_filters_before_top_five(
    tmp_path, monkeypatch, scores, expected
):
    body, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            ranking_mode="decision",
            memory_count=len(scores),
            scores=scores,
        )
    )
    assert tuple(item["memory_id"] for item in body["items"]) == expected
    assert seen.purpose == "automatic_recall"
    assert seen.calls == 1
    assert len(seen.documents) == len(scores)
    assert seen.relations == expected


def test_implicit_decision_uses_same_threshold_for_memory_and_history(
    tmp_path, monkeypatch
):
    body, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            ranking_mode="decision",
            memory_count=2,
            history_count=3,
            scores=[0.699, 0.7, 0.9, 0.699, 0.7],
        )
    )
    assert [item.get("memory_id", item.get("title")) for item in body["items"]] == [
        "history 0",
        "memory:1",
        "history 2",
    ]
    assert len(seen.documents) == 5


@pytest.mark.parametrize(
    "mode",
    [
        "failure",
        "malformed",
        "provider_unavailable",
        "bad_probability",
        "projection",
        "settings_changed",
    ],
)
@pytest.mark.parametrize("memory_count", [0, 2])
def test_failed_implicit_decision_does_not_bypass_history_admission(
    tmp_path, monkeypatch, mode, memory_count
):
    body, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            automatic=True,
            rank_automatic=True,
            ranking_mode="decision",
            memory_count=memory_count,
            history_count=3,
            mode=mode,
            scores=[1.1] * (memory_count + 3) if mode == "bad_probability" else None,
        )
    )
    assert tuple(item["memory_id"] for item in body["items"]) == tuple(
        f"memory:{i}" for i in range(memory_count)
    )
    assert seen.relations == tuple(f"memory:{i}" for i in range(memory_count))


@pytest.mark.parametrize("related", [False, True])
def test_explicit_decision_and_related_inspection_do_not_use_implicit_threshold(
    tmp_path, monkeypatch, related
):
    result, seen = asyncio.run(
        _search(
            tmp_path,
            monkeypatch,
            limit=5,
            ranking_mode="decision",
            memory_count=5,
            scores=[0.2, 0.5, 0.1, 0.3, 0.4],
            related=related,
        )
    )
    if related:
        related_items, coverage = result
        assert tuple(item.memory_id for item in related_items) == (
            "memory:1",
            "memory:4",
            "memory:3",
        )
        assert coverage["rerank"] == "APPLIED"
        assert seen.purpose == "related_memory"
    else:
        assert tuple(item["memory_id"] for item in result["memories"]) == (
            "memory:1",
            "memory:4",
            "memory:3",
            "memory:0",
            "memory:2",
        )
        assert seen.purpose == "recall"
