from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
import math
from datetime import UTC, datetime
from typing import Sequence
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.memory.candidates.pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.memory.canonical.query import CanonicalNodeView, PostgresMemoryQuery
from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.memory.governance.relatedness import (
    GovernanceRelatednessService,
    MemoryGovernanceRelatednessOptions,
    RelatednessAvailability,
)
from pulsara_agent.ontology import memory
from pulsara_agent.retrieval.rerank.protocol import RerankResult
from pulsara_agent.retrieval.tokenizer.regex_word_split import RegexWordSplitTokenizer
from pulsara_agent.settings import StorageConfig


class _MemoryQuery:
    def __init__(
        self,
        views: Sequence[CanonicalNodeView],
        *,
        missing_ids: Sequence[str] = (),
        lexical: dict[str, list[tuple[str, float]]] | None = None,
    ) -> None:
        self.views = {view.id: view for view in views}
        self.missing_ids = list(missing_ids)
        self.lexical = lexical or {}

    def fetch_nodes(self, ids, *, graph_id=None):
        return [self.views[memory_id] for memory_id in ids if memory_id in self.views]

    def exact_candidates(self, *, statement, scope, memory_type, graph_id=None):
        normalized = _normalize(statement)
        return [
            view.id
            for view in self.views.values()
            if view.scope == scope
            and view.memory_type == memory_type
            and view.status is memory.NodeStatus.ACTIVE
            and _normalize(view.statement) == normalized
        ]

    def lexical_candidates(self, *, terms, scopes, types, limit, graph_id=None):
        joined = " ".join(terms)
        return self.lexical.get(joined, [])[:limit]

    def fts_candidates(self, **kwargs):
        return []

    def missing_vector_ids(
        self, *, embedding_fingerprint, scopes, types, limit, graph_id=None
    ):
        return self.missing_ids[:limit]


class _Embedding:
    model_id = "fixture-embedding"
    dimensions = 4

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, text):
        return _vector(text)

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [_vector(text) for text in texts]

    async def aclose(self):
        return None


class _Embedding1024(_Embedding):
    dimensions = 1024

    async def embed(self, text):
        return _vector_1024(text)

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [_vector_1024(text) for text in texts]


class _VectorQuery:
    def __init__(self, views: Sequence[CanonicalNodeView]) -> None:
        self.views = list(views)
        self.calls = 0

    def candidates(
        self,
        *,
        query_vector,
        embedding_fingerprint,
        scopes,
        types,
        limit,
        graph_id=None,
    ):
        self.calls += 1
        rows = [
            (view.id, _cosine(query_vector, _vector(view.statement)))
            for view in self.views
            if view.scope in scopes
            and view.memory_type in types
            and view.status is memory.NodeStatus.ACTIVE
        ]
        rows.sort(key=lambda row: (-row[1], row[0]))
        return rows[:limit]


class _Reranker:
    model_id = "fixture-reranker"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def rerank(self, query, documents, *, instruction=None, top_n=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("reranker unavailable")
        return [
            RerankResult(index=index, score=0.9 - index * 0.01)
            for index in range(len(documents))
        ]

    async def aclose(self):
        return None


def test_relatedness_batches_deduplicated_embedding_and_hides_exact_scores_from_prompt() -> (
    None
):
    views = [_view("preference:egg-tart", "The user likes egg tarts.")]
    embedding = _Embedding()
    service = _service(views, embedding=embedding, reranker=_Reranker())
    pending = [
        _candidate("pool:a", "The user likes dan tat."),
        _candidate("pool:b", "The user likes dan tat."),
    ]

    result = asyncio.run(service.collect_batch(pending, graph_id="graph:test"))

    assert len(embedding.calls) == 1
    assert len(embedding.calls[0]) == len(set(embedding.calls[0]))
    assert result.for_candidate("pool:a").allowlist == frozenset(
        {"preference:egg-tart"}
    )
    prompt = result.for_candidate("pool:a").prompt_view()[0]
    assert prompt["match_channels"] == ["dense", "rerank"]
    assert not any("score" in key for key in prompt)
    internal = result.diagnostics["per_candidate"]["pool:a"]["internal_scores"]
    assert internal["preference:egg-tart"]["dense"] > 0.9
    assert result.diagnostics["same_batch_lifecycle_deferred"] is True


def test_relatedness_labeled_semantic_fixture_meets_recall_at_k_gate() -> None:
    labeled = [
        ("pool:cross-lingual", "The user likes dan tat.", "preference:egg-tart"),
        ("pool:alias", "Use JS for browser scripts.", "preference:javascript"),
        ("pool:paraphrase", "Keep answers brief.", "preference:concise"),
        ("pool:cjk", "用户希望回复简洁。", "preference:concise-zh"),
    ]
    views = [
        _view("preference:egg-tart", "The user enjoys egg tarts."),
        _view("preference:javascript", "Use JavaScript for browser scripts."),
        _view("preference:concise", "The user prefers concise answers."),
        _view("preference:concise-zh", "用户偏好简短回答。"),
        _view(
            "preference:hard-negative",
            "The user prefers detailed architecture reports.",
        ),
    ]
    service = _service(views, embedding=_Embedding(), candidate_limit=2)

    result = asyncio.run(
        service.collect_batch(
            [_candidate(entry_id, statement) for entry_id, statement, _ in labeled],
            graph_id="graph:test",
        )
    )

    hits = sum(
        expected in result.for_candidate(entry_id).allowlist
        for entry_id, _, expected in labeled
    )
    recall_at_k = hits / len(labeled)
    miss_rate = 1.0 - recall_at_k
    assert recall_at_k >= 0.95
    assert miss_rate <= 0.05
    assert all(
        len(result.for_candidate(entry_id).memories) <= 2 for entry_id, _, _ in labeled
    )


def test_versioned_deterministic_alias_channel_surfaces_target_without_dense_provider() -> (
    None
):
    view = _view("preference:egg-tart", "The user enjoys egg tarts.")
    query = _MemoryQuery(
        [view],
        lexical={"egg tart egg tarts 蛋挞 蛋塔": [(view.id, 2.0)]},
    )
    service = GovernanceRelatednessService(
        memory_query=query,  # type: ignore[arg-type]
        tokenizer=RegexWordSplitTokenizer(),
        options=MemoryGovernanceRelatednessOptions(candidate_limit=1),
    )

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:alias", "The user likes dan tat.")],
            graph_id="graph:test",
        )
    )

    candidate = result.for_candidate("pool:alias")
    assert candidate.allowlist == frozenset({view.id})
    assert candidate.prompt_view()[0]["match_channels"] == ["alias"]
    assert result.diagnostics["alias_policy_version"] == "governance-aliases:v1"


def test_committed_but_unindexed_gap_repair_is_bounded_and_traced() -> None:
    views = [
        _view("preference:gap-a", "The user likes egg tarts."),
        _view("preference:gap-b", "The user likes tea."),
        _view("preference:gap-c", "The user likes coffee."),
    ]
    embedding = _Embedding()
    service = _service(
        views,
        embedding=embedding,
        vector_views=[],
        missing_ids=[view.id for view in views],
        max_inline_gap_embeds=2,
    )

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user likes dan tat.")],
            graph_id="graph:test",
        )
    )

    assert result.for_candidate("pool:new").allowlist == frozenset({"preference:gap-a"})
    assert result.diagnostics["relatedness_inline_embed_count"] == 2
    assert result.diagnostics["relatedness_gap_candidates_truncated"] is True
    # One candidate query plus exactly two bounded canonical gap texts.
    assert len(embedding.calls[0]) == 3


def test_rerank_failure_keeps_allowlist_but_marks_partial_and_disables_lifecycle() -> (
    None
):
    views = [_view("preference:egg-tart", "The user likes egg tarts.")]
    service = _service(views, embedding=_Embedding(), reranker=_Reranker(fail=True))

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user likes dan tat.")],
            graph_id="graph:test",
        )
    )
    candidate = result.for_candidate("pool:new")
    context = result.execution_context("governance:test")

    assert candidate.allowlist == frozenset({"preference:egg-tart"})
    assert candidate.availability is RelatednessAvailability.PARTIAL
    assert context.allows_lifecycle("pool:new", "preference:egg-tart") is False


def test_dense_failure_preserves_exact_allowlist_as_partial() -> None:
    views = [_view("preference:exact", "The user prefers concise answers.")]
    service = _service(views, embedding=_Embedding(fail=True))

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user prefers concise answers.")],
            graph_id="graph:test",
        )
    )

    candidate = result.for_candidate("pool:new")
    assert candidate.allowlist == frozenset({"preference:exact"})
    assert candidate.availability is RelatednessAvailability.PARTIAL


def test_unconfigured_reranker_is_deployment_relative_full() -> None:
    views = [_view("preference:egg-tart", "The user likes egg tarts.")]
    service = _service(views, embedding=_Embedding(), reranker=None)

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user likes dan tat.")],
            graph_id="graph:test",
        )
    )

    assert result.for_candidate("pool:new").availability is RelatednessAvailability.FULL


def test_sync_face_validation_removes_cross_scope_and_inactive_vector_hits() -> None:
    views = [
        _view("preference:right", "The user likes egg tarts."),
        _view(
            "preference:wrong-scope",
            "The user likes egg tarts.",
            scope="ctx:workspace/x",
        ),
        _view(
            "preference:inactive",
            "The user likes egg tarts.",
            status=memory.NodeStatus.SUPERSEDED,
        ),
    ]
    service = _service(views, embedding=_Embedding())

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user likes dan tat.")],
            graph_id="graph:test",
        )
    )

    assert result.for_candidate("pool:new").allowlist == frozenset({"preference:right"})


def test_options_and_fingerprint_are_present_in_eval_manifest_diagnostics() -> None:
    views = [_view("preference:egg-tart", "The user likes egg tarts.")]
    service = _service(views, embedding=_Embedding(), candidate_limit=3)

    result = asyncio.run(
        service.collect_batch(
            [_candidate("pool:new", "The user likes dan tat.")], graph_id=None
        )
    )

    assert result.diagnostics["policy_version"] == "governance-relatedness:test"
    assert result.diagnostics["embedding_fingerprint"] == "fixture:fixture-embedding:4"
    assert result.diagnostics["dense_candidate_min_score"] == 0.25
    assert result.diagnostics["candidate_limit"] == 3


def test_postgres_committed_but_unindexed_memory_is_found_from_sync_face() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
    graph_id = f"graph:test/relatedness/{uuid4().hex}"
    memory_id = "preference:committed-unindexed-egg-tart"
    now = datetime.now(UTC).isoformat()
    store = PostgresGraphStore(verified_postgres_provider(dsn))
    try:
        store.put_jsonld(
            Preference(
                id=memory_id,
                statement="The user enjoys egg tarts.",
                scope="ctx:user",
                status=memory.NodeStatus.ACTIVE,
                confidence_level=memory.ConfidenceLevel.HIGH,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                created_at=now,
                updated_at=now,
                gate_reason="test",
            ).to_jsonld(),
            graph_id=graph_id,
        )
        embedding = _Embedding1024()
        service = GovernanceRelatednessService(
            memory_query=PostgresMemoryQuery(
                connection_provider=verified_postgres_provider(dsn)
            ),
            tokenizer=RegexWordSplitTokenizer(),
            embedding=embedding,
            vector_query=MemoryVectorQuery(verified_postgres_provider(dsn)),
            provider_name="fixture",
            options=MemoryGovernanceRelatednessOptions(
                dense_candidate_min_score=0.25,
                max_inline_gap_embeds=5,
            ),
        )

        result = asyncio.run(
            service.collect_batch(
                [_candidate("pool:postgres-gap", "The user likes dan tat.")],
                graph_id=graph_id,
            )
        )

        assert result.for_candidate("pool:postgres-gap").allowlist == frozenset(
            {memory_id}
        )
        assert result.diagnostics["relatedness_inline_embed_count"] == 1
        assert len(embedding.calls) == 1
    finally:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_vector_index where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )


def _service(
    views: Sequence[CanonicalNodeView],
    *,
    embedding: _Embedding | None,
    reranker: _Reranker | None = None,
    vector_views: Sequence[CanonicalNodeView] | None = None,
    missing_ids: Sequence[str] = (),
    max_inline_gap_embeds: int = 20,
    candidate_limit: int = 10,
) -> GovernanceRelatednessService:
    return GovernanceRelatednessService(
        memory_query=_MemoryQuery(views, missing_ids=missing_ids),  # type: ignore[arg-type]
        tokenizer=RegexWordSplitTokenizer(),
        embedding=embedding,
        vector_query=_VectorQuery(views if vector_views is None else vector_views)
        if embedding
        else None,  # type: ignore[arg-type]
        reranker=reranker,
        provider_name="fixture",
        options=MemoryGovernanceRelatednessOptions(
            policy_version="governance-relatedness:test",
            candidate_limit=candidate_limit,
            dense_candidate_min_score=0.25,
            rerank_candidate_min_score=0.10,
            max_inline_gap_embeds=max_inline_gap_embeds,
        ),
    )


def _candidate(
    entry_id: str, statement: str, *, scope: str = "ctx:user"
) -> PooledMemoryCandidate:
    return PooledMemoryCandidate(
        entry_id=entry_id,
        payload=ValidCandidatePayload(
            candidate=PreferenceCandidate(
                candidate_id=f"candidate:{entry_id}",
                statement=statement,
                scope=scope,
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
            )
        ),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id="runtime:test",
        source_run_id=f"run:{entry_id}",
        source_turn_id=f"turn:{entry_id}",
        source_reply_id=f"reply:{entry_id}",
        user_quote=statement,
    )


def _view(
    memory_id: str,
    statement: str,
    *,
    scope: str = "ctx:user",
    status: memory.NodeStatus = memory.NodeStatus.ACTIVE,
) -> CanonicalNodeView:
    now = datetime.now(UTC)
    return CanonicalNodeView(
        id=memory_id,
        memory_type="Preference",
        scope=scope,
        status=status,
        statement=statement,
        summary=None,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        confidence_level=memory.ConfidenceLevel.HIGH,
        applies_when=None,
        do_not_apply_when=None,
        created_at=now,
        updated_at=now,
        node_revision=1,
        evidence_ids=(),
        outgoing=(),
        incoming=(),
    )


def _vector(text: str) -> list[float]:
    normalized = text.casefold()
    if any(term in normalized for term in ("egg tart", "dan tat")):
        return [1.0, 0.0, 0.0, 0.0]
    if any(term in normalized for term in ("javascript", " js ")):
        return [0.0, 1.0, 0.0, 0.0]
    if any(term in normalized for term in ("concise", "brief", "简洁", "简短")):
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


def _vector_1024(text: str) -> list[float]:
    prefix = _vector(text)
    return [*prefix, *([0.0] * (1024 - len(prefix)))]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
