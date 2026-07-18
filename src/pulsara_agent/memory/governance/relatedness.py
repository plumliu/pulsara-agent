"""Batch semantic candidate discovery for memory governance.

The service is deliberately advisory.  It discovers committed canonical IDs,
while the governance executor remains the authority for lifecycle validation.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.memory.candidates.pool import PooledMemoryCandidate
from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery
from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.ontology import memory
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.retrieval.rerank.protocol import RerankProvider
from pulsara_agent.retrieval.tokenizer.protocol import Tokenizer


DEFAULT_GOVERNANCE_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("dan tat", "egg tart", "egg tarts", "蛋挞", "蛋塔"),
    ("js", "javascript"),
    ("pg", "postgres", "postgresql"),
)


class RelatednessAvailability(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MemoryGovernanceRelatednessOptions:
    policy_version: str = "governance-relatedness:v1"
    fixture_version: str = "governance-relatedness-fixture:v1"
    alias_policy_version: str = "governance-aliases:v1"
    candidate_limit: int = 5
    lexical_limit: int = 30
    vector_limit: int = 30
    rerank_top_m: int = 20
    dense_candidate_min_score: float = 0.30
    rerank_candidate_min_score: float = 0.20
    max_inline_gap_embeds: int = 20
    provider_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        for name in ("candidate_limit", "lexical_limit", "vector_limit", "rerank_top_m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_inline_gap_embeds < 0:
            raise ValueError("max_inline_gap_embeds must not be negative")
        if self.provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")
        for name in ("dense_candidate_min_score", "rerank_candidate_min_score"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RelatedCanonicalMemory:
    view: CanonicalNodeView
    match_channels: tuple[str, ...]
    is_exact_duplicate: bool
    internal_scores: Mapping[str, float] = field(default_factory=dict, repr=False)

    def prompt_view(self) -> dict[str, Any]:
        return {
            "memory_id": self.view.id,
            "memory_type": self.view.memory_type,
            "statement": self.view.statement,
            "scope": self.view.scope,
            "status": self.view.status.value,
            "verification_status": (
                self.view.verification_status.value if self.view.verification_status else None
            ),
            "source_authority": (
                self.view.source_authority.value if self.view.source_authority else None
            ),
            "applies_when": self.view.applies_when,
            "do_not_apply_when": self.view.do_not_apply_when,
            "is_exact_duplicate": self.is_exact_duplicate,
            "match_channels": list(self.match_channels),
        }


@dataclass(frozen=True, slots=True)
class CandidateRelatedness:
    entry_id: str
    memories: tuple[RelatedCanonicalMemory, ...] = ()
    availability: RelatednessAvailability = RelatednessAvailability.UNAVAILABLE
    warnings: tuple[str, ...] = ()

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(item.view.id for item in self.memories)

    def prompt_view(self) -> list[dict[str, Any]]:
        return [item.prompt_view() for item in self.memories]


@dataclass(frozen=True, slots=True)
class RelatednessExecutionContext:
    governance_batch_id: str
    allowlists: Mapping[str, frozenset[str]]
    availability: Mapping[str, RelatednessAvailability]
    node_revisions: Mapping[str, Mapping[str, int]]
    verified_evidence_refs: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def allows_lifecycle(self, entry_id: str, memory_id: str) -> bool:
        return (
            self.availability.get(entry_id) is RelatednessAvailability.FULL
            and memory_id in self.allowlists.get(entry_id, frozenset())
        )


@dataclass(frozen=True, slots=True)
class RelatednessBatchResult:
    candidates: Mapping[str, CandidateRelatedness]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def for_candidate(self, entry_id: str) -> CandidateRelatedness:
        return self.candidates.get(entry_id, CandidateRelatedness(entry_id=entry_id))

    def execution_context(
        self,
        governance_batch_id: str,
        *,
        verified_evidence_refs: Mapping[str, frozenset[str]] | None = None,
    ) -> RelatednessExecutionContext:
        return RelatednessExecutionContext(
            governance_batch_id=governance_batch_id,
            allowlists=MappingProxyType(
                {entry_id: result.allowlist for entry_id, result in self.candidates.items()}
            ),
            availability=MappingProxyType(
                {entry_id: result.availability for entry_id, result in self.candidates.items()}
            ),
            node_revisions=MappingProxyType(
                {
                    entry_id: MappingProxyType(
                        {
                            item.view.id: item.view.node_revision
                            for item in result.memories
                        }
                    )
                    for entry_id, result in self.candidates.items()
                }
            ),
            verified_evidence_refs=MappingProxyType(
                dict(verified_evidence_refs or {})
            ),
        )

    @classmethod
    def unavailable(
        cls,
        pending: Sequence[PooledMemoryCandidate],
        *,
        warning: str,
    ) -> "RelatednessBatchResult":
        return cls(
            candidates=MappingProxyType(
                {
                    candidate.entry_id: CandidateRelatedness(
                        entry_id=candidate.entry_id,
                        warnings=(warning,),
                    )
                    for candidate in pending
                }
            ),
            diagnostics=MappingProxyType(
                {"status": RelatednessAvailability.UNAVAILABLE.value, "warnings": [warning]}
            ),
        )


@dataclass(slots=True)
class GovernanceRelatednessService:
    memory_query: MemoryQuery
    tokenizer: Tokenizer
    embedding: EmbeddingProvider | None = None
    vector_query: MemoryVectorQuery | None = None
    reranker: RerankProvider | None = None
    provider_name: str = "openai_compatible"
    alias_groups: tuple[tuple[str, ...], ...] = DEFAULT_GOVERNANCE_ALIAS_GROUPS
    options: MemoryGovernanceRelatednessOptions = field(
        default_factory=MemoryGovernanceRelatednessOptions
    )

    @property
    def embedding_fingerprint(self) -> str | None:
        if self.embedding is None:
            return None
        return f"{self.provider_name}:{self.embedding.model_id}:{self.embedding.dimensions}"

    async def collect_batch(
        self,
        pending: Sequence[PooledMemoryCandidate],
        *,
        graph_id: str | None,
    ) -> RelatednessBatchResult:
        specs = tuple(_candidate_spec(candidate) for candidate in pending)
        valid_specs = tuple(spec for spec in specs if spec is not None)
        if not valid_specs:
            return RelatednessBatchResult.unavailable(pending, warning="no_valid_candidates")

        channel_warnings: list[str] = []
        channel_latency_ms: dict[str, float] = {}
        channel_rows: dict[str, dict[str, dict[str, float]]] = {
            spec.entry_id: defaultdict(dict) for spec in valid_specs
        }

        started = time.perf_counter()
        failed_entries = await self._collect_exact_and_lexical(
            valid_specs,
            channel_rows,
            graph_id=graph_id,
            warnings=channel_warnings,
        )
        channel_latency_ms["exact_lexical"] = (time.perf_counter() - started) * 1000.0

        inline_ids: list[str] = []
        inline_views: dict[str, CanonicalNodeView] = {}
        vectors: dict[str, list[float]] = {}
        dense_configured = self.embedding is not None and self.vector_query is not None
        if dense_configured:
            started = time.perf_counter()
            try:
                inline_ids = await asyncio.to_thread(
                    self.memory_query.missing_vector_ids,
                    embedding_fingerprint=self.embedding_fingerprint or "",
                    scopes=tuple(dict.fromkeys(spec.scope for spec in valid_specs)),
                    types=tuple(dict.fromkeys(spec.memory_type for spec in valid_specs)),
                    limit=self.options.max_inline_gap_embeds + 1,
                    graph_id=graph_id,
                )
                inline_truncated = len(inline_ids) > self.options.max_inline_gap_embeds
                inline_ids = inline_ids[: self.options.max_inline_gap_embeds]
                inline_views = {
                    view.id: view
                    for view in await asyncio.to_thread(
                        self.memory_query.fetch_nodes,
                        inline_ids,
                        graph_id=graph_id,
                    )
                }
                vectors = await self._embed_batch(valid_specs, inline_views)
                await self._collect_dense(
                    valid_specs,
                    channel_rows,
                    vectors,
                    inline_views,
                    graph_id=graph_id,
                )
            except Exception as exc:
                inline_truncated = False
                channel_warnings.append(f"dense_degraded:{type(exc).__name__}")
                failed_entries.update(spec.entry_id for spec in valid_specs)
            finally:
                channel_latency_ms["dense_and_gap"] = (time.perf_counter() - started) * 1000.0
        else:
            inline_truncated = False

        all_ids = tuple(
            dict.fromkeys(
                memory_id
                for rows in channel_rows.values()
                for memory_id in rows
            )
        )
        try:
            fetched = await asyncio.to_thread(
                self.memory_query.fetch_nodes,
                all_ids,
                graph_id=graph_id,
            )
            views = {view.id: view for view in fetched}
        except Exception as exc:
            return RelatednessBatchResult.unavailable(
                pending,
                warning=f"canonical_validation_failed:{type(exc).__name__}",
            )

        per_candidate = self._validated_candidates(valid_specs, channel_rows, views)
        rerank_configured = self.reranker is not None
        rerank_failed_entries: set[str] = set()
        if rerank_configured:
            started = time.perf_counter()
            rerank_failed_entries = await self._rerank(valid_specs, per_candidate)
            channel_latency_ms["rerank"] = (time.perf_counter() - started) * 1000.0
            failed_entries.update(rerank_failed_entries)
            if rerank_failed_entries:
                channel_warnings.append("rerank_degraded")

        results: dict[str, CandidateRelatedness] = {}
        valid_by_id = {spec.entry_id: spec for spec in valid_specs}
        for candidate in pending:
            spec = valid_by_id.get(candidate.entry_id)
            if spec is None:
                results[candidate.entry_id] = CandidateRelatedness(
                    entry_id=candidate.entry_id,
                    warnings=("invalid_candidate_payload",),
                )
                continue
            memories = tuple(per_candidate.get(spec.entry_id, ())[: self.options.candidate_limit])
            failed = spec.entry_id in failed_entries
            availability = (
                RelatednessAvailability.UNAVAILABLE
                if not memories
                else RelatednessAvailability.PARTIAL
                if failed
                else RelatednessAvailability.FULL
            )
            results[spec.entry_id] = CandidateRelatedness(
                entry_id=spec.entry_id,
                memories=memories,
                availability=availability,
                warnings=tuple(channel_warnings),
            )

        sibling_groups = _same_batch_sibling_groups(valid_specs)
        diagnostics = {
            "policy_version": self.options.policy_version,
            "fixture_version": self.options.fixture_version,
            "alias_policy_version": self.options.alias_policy_version,
            "embedding_fingerprint": self.embedding_fingerprint,
            "dense_candidate_min_score": self.options.dense_candidate_min_score,
            "candidate_limit": self.options.candidate_limit,
            "rerank_top_m": self.options.rerank_top_m,
            "max_inline_gap_embeds": self.options.max_inline_gap_embeds,
            "batch_candidate_count": len(pending),
            "deduplicated_embed_text_count": len(vectors),
            "relatedness_inline_embed_count": len(inline_views),
            "relatedness_missing_current_fingerprint_count": len(inline_ids)
            + (1 if inline_truncated else 0),
            "relatedness_gap_candidates_truncated": inline_truncated,
            "warnings": list(dict.fromkeys(channel_warnings)),
            "channel_latency_ms": channel_latency_ms,
            "channel_candidate_counts": {
                channel: sum(
                    channel in scores
                    for candidate_rows in channel_rows.values()
                    for scores in candidate_rows.values()
                )
                for channel in ("exact", "alias", "lexical", "dense", "inline_gap")
            },
            "per_candidate": {
                entry_id: {
                    "availability": result.availability.value,
                    "allowlist_ids": sorted(result.allowlist),
                    "internal_scores": {
                        memory.view.id: dict(memory.internal_scores)
                        for memory in result.memories
                    },
                }
                for entry_id, result in results.items()
            },
            "same_batch_lifecycle_deferred": bool(sibling_groups),
            "same_batch_lifecycle_deferred_candidates": sibling_groups,
        }
        return RelatednessBatchResult(
            candidates=MappingProxyType(results),
            diagnostics=MappingProxyType(diagnostics),
        )

    async def _collect_exact_and_lexical(
        self,
        specs: Sequence[_CandidateSpec],
        rows: dict[str, dict[str, dict[str, float]]],
        *,
        graph_id: str | None,
        warnings: list[str],
    ) -> set[str]:
        async def collect(spec: _CandidateSpec) -> None:
            alias_terms = _alias_expansions(spec.lexical_text, self.alias_groups)

            async def alias_candidates() -> list[tuple[str, float]]:
                if not alias_terms:
                    return []
                return await asyncio.to_thread(
                    self.memory_query.lexical_candidates,
                    terms=alias_terms,
                    scopes=(spec.scope,),
                    types=(spec.memory_type,),
                    limit=self.options.lexical_limit,
                    graph_id=graph_id,
                )

            exact_ids, lexical, aliases = await asyncio.gather(
                asyncio.to_thread(
                    self.memory_query.exact_candidates,
                    statement=spec.statement,
                    scope=spec.scope,
                    memory_type=spec.memory_type,
                    graph_id=graph_id,
                ),
                asyncio.to_thread(
                    self.memory_query.lexical_candidates,
                    terms=_lexical_terms(self.tokenizer, spec.lexical_text),
                    scopes=(spec.scope,),
                    types=(spec.memory_type,),
                    limit=self.options.lexical_limit,
                    graph_id=graph_id,
                ),
                alias_candidates(),
            )
            for memory_id in exact_ids:
                rows[spec.entry_id][memory_id]["exact"] = 1.0
            for memory_id, score in lexical:
                if score > 0:
                    rows[spec.entry_id][memory_id]["lexical"] = score
            for memory_id, score in aliases:
                if score > 0:
                    rows[spec.entry_id][memory_id]["alias"] = score

        outcomes = await asyncio.gather(*(collect(spec) for spec in specs), return_exceptions=True)
        failed = {
            spec.entry_id
            for spec, outcome in zip(specs, outcomes, strict=True)
            if isinstance(outcome, Exception)
        }
        if failed:
            warnings.append("lexical_degraded")
        return failed

    async def _embed_batch(
        self,
        specs: Sequence[_CandidateSpec],
        inline_views: Mapping[str, CanonicalNodeView],
    ) -> dict[str, list[float]]:
        assert self.embedding is not None
        texts = tuple(
            dict.fromkeys(
                [spec.query_text for spec in specs]
                + [_canonical_text(view) for view in inline_views.values()]
            )
        )
        if not texts:
            return {}
        embedded = await asyncio.wait_for(
            self.embedding.embed_batch(texts),
            timeout=self.options.provider_timeout_seconds,
        )
        if len(embedded) != len(texts):
            raise ValueError("embedding provider did not preserve batch cardinality")
        return dict(zip(texts, embedded, strict=True))

    async def _collect_dense(
        self,
        specs: Sequence[_CandidateSpec],
        rows: dict[str, dict[str, dict[str, float]]],
        vectors: Mapping[str, list[float]],
        inline_views: Mapping[str, CanonicalNodeView],
        *,
        graph_id: str | None,
    ) -> None:
        assert self.vector_query is not None
        fingerprint = self.embedding_fingerprint or ""

        async def indexed(spec: _CandidateSpec) -> tuple[_CandidateSpec, list[tuple[str, float]]]:
            found = await asyncio.to_thread(
                self.vector_query.candidates,
                query_vector=vectors[spec.query_text],
                embedding_fingerprint=fingerprint,
                scopes=(spec.scope,),
                types=(spec.memory_type,),
                limit=self.options.vector_limit,
                graph_id=graph_id,
            )
            return spec, found

        indexed_rows = await asyncio.gather(*(indexed(spec) for spec in specs))
        for spec, found in indexed_rows:
            for memory_id, score in found:
                if score >= self.options.dense_candidate_min_score:
                    rows[spec.entry_id][memory_id]["dense"] = score
            query_vector = vectors[spec.query_text]
            for view in inline_views.values():
                if view.scope != spec.scope or view.memory_type != spec.memory_type:
                    continue
                score = _cosine(query_vector, vectors[_canonical_text(view)])
                if score >= self.options.dense_candidate_min_score:
                    rows[spec.entry_id][view.id]["inline_gap"] = score

    def _validated_candidates(
        self,
        specs: Sequence[_CandidateSpec],
        rows: Mapping[str, Mapping[str, Mapping[str, float]]],
        views: Mapping[str, CanonicalNodeView],
    ) -> dict[str, list[RelatedCanonicalMemory]]:
        result: dict[str, list[RelatedCanonicalMemory]] = {}
        for spec in specs:
            selected: list[RelatedCanonicalMemory] = []
            for memory_id, scores in rows.get(spec.entry_id, {}).items():
                view = views.get(memory_id)
                if (
                    view is None
                    or view.scope != spec.scope
                    or view.memory_type != spec.memory_type
                    or view.status is not memory.NodeStatus.ACTIVE
                ):
                    continue
                exact = _normalize(view.statement) == _normalize(spec.statement)
                channels = tuple(
                    channel
                    for channel in ("exact", "alias", "lexical", "dense", "inline_gap")
                    if channel in scores
                )
                selected.append(
                    RelatedCanonicalMemory(
                        view=view,
                        match_channels=channels,
                        is_exact_duplicate=exact,
                        internal_scores=MappingProxyType(dict(scores)),
                    )
                )
            selected.sort(key=_candidate_sort_key)
            result[spec.entry_id] = selected[: max(self.options.rerank_top_m, self.options.candidate_limit)]
        return result

    async def _rerank(
        self,
        specs: Sequence[_CandidateSpec],
        candidates: dict[str, list[RelatedCanonicalMemory]],
    ) -> set[str]:
        assert self.reranker is not None

        async def rerank(spec: _CandidateSpec) -> tuple[str, list[RelatedCanonicalMemory]]:
            selected = candidates.get(spec.entry_id, ())[: self.options.rerank_top_m]
            if not selected:
                return spec.entry_id, []
            results = await asyncio.wait_for(
                self.reranker.rerank(
                    spec.query_text,
                    [_canonical_text(item.view) for item in selected],
                    instruction=(
                        "Rank canonical memories by whether they express the same durable subject "
                        "or a directly conflicting/replaced state. Resolve translations, aliases, "
                        "and paraphrases. Mere topical similarity or shared boilerplate is weak."
                    ),
                    top_n=len(selected),
                ),
                timeout=self.options.provider_timeout_seconds,
            )
            by_index = {result.index: result.score for result in results}
            reranked: list[RelatedCanonicalMemory] = []
            for index, item in enumerate(selected):
                score = float(by_index.get(index, 0.0))
                if score < self.options.rerank_candidate_min_score and not item.is_exact_duplicate:
                    continue
                reranked.append(
                    RelatedCanonicalMemory(
                        view=item.view,
                        match_channels=tuple(dict.fromkeys((*item.match_channels, "rerank"))),
                        is_exact_duplicate=item.is_exact_duplicate,
                        internal_scores=MappingProxyType(
                            {**dict(item.internal_scores), "rerank": score}
                        ),
                    )
                )
            reranked.sort(key=_candidate_sort_key)
            return spec.entry_id, reranked

        outcomes = await asyncio.gather(*(rerank(spec) for spec in specs), return_exceptions=True)
        failed: set[str] = set()
        for spec, outcome in zip(specs, outcomes, strict=True):
            if isinstance(outcome, Exception):
                failed.add(spec.entry_id)
                continue
            entry_id, reranked = outcome
            candidates[entry_id] = reranked
        return failed


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    entry_id: str
    statement: str
    query_text: str
    lexical_text: str
    scope: str
    memory_type: str


def _candidate_spec(candidate: PooledMemoryCandidate) -> _CandidateSpec | None:
    if not isinstance(candidate.payload, ValidCandidatePayload):
        return None
    value = candidate.payload.candidate
    context = [value.statement]
    if candidate.user_quote and _normalize(candidate.user_quote) != _normalize(value.statement):
        context.append(candidate.user_quote)
    applies_when = getattr(value, "applies_when", None)
    do_not_apply_when = getattr(value, "do_not_apply_when", None)
    if applies_when:
        context.append(f"Applies when: {applies_when}")
    if do_not_apply_when:
        context.append(f"Do not apply when: {do_not_apply_when}")
    return _CandidateSpec(
        entry_id=candidate.entry_id,
        statement=value.statement,
        query_text="\n".join(context),
        lexical_text="\n".join(
            text
            for text in (value.statement, candidate.user_quote)
            if text
        ),
        scope=value.scope,
        memory_type=value.kind,
    )


def _canonical_text(view: CanonicalNodeView) -> str:
    fields = [
        f"Type: {view.memory_type}",
        f"Scope: {view.scope}",
        f"Statement: {view.statement}",
    ]
    if view.summary:
        fields.append(f"Summary: {view.summary}")
    if view.applies_when:
        fields.append(f"Applies when: {view.applies_when}")
    if view.do_not_apply_when:
        fields.append(f"Do not apply when: {view.do_not_apply_when}")
    return "\n".join(fields)


def _candidate_sort_key(item: RelatedCanonicalMemory) -> tuple[float, float, str]:
    scores = item.internal_scores
    rerank = scores.get("rerank", 0.0)
    semantic = max(scores.get("dense", 0.0), scores.get("inline_gap", 0.0))
    lexical = scores.get("lexical", 0.0)
    alias = scores.get("alias", 0.0)
    total = (
        (100.0 if item.is_exact_duplicate else 0.0)
        + (25.0 if alias > 0.0 else 0.0)
        + rerank * 10.0
        + semantic * 5.0
        + lexical
    )
    return (-total, -semantic, item.view.id)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _lexical_terms(tokenizer: Tokenizer, text: str) -> tuple[str, ...]:
    generic = {
        "user",
        "users",
        "prefer",
        "prefers",
        "preference",
        "like",
        "likes",
        "remember",
        "please",
        "用户",
        "偏好",
        "喜欢",
        "记住",
    }
    return tuple(
        dict.fromkeys(
            token
            for token in tokenizer.tokenize(text)
            if token.casefold() not in generic
        )
    )


def _alias_expansions(
    text: str,
    groups: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    normalized = _normalize(text)
    expanded: list[str] = []
    for group in groups:
        matched = [alias for alias in group if _contains_alias(normalized, alias)]
        if not matched:
            continue
        expanded.extend(alias for alias in group if alias not in matched)
    return tuple(dict.fromkeys(expanded))


def _contains_alias(text: str, alias: str) -> bool:
    normalized_alias = _normalize(alias)
    if not normalized_alias:
        return False
    if any("\u4e00" <= character <= "\u9fff" for character in normalized_alias):
        return normalized_alias in text
    return re.search(rf"(?<!\w){re.escape(normalized_alias)}(?!\w)", text) is not None


def _same_batch_sibling_groups(specs: Sequence[_CandidateSpec]) -> list[list[str]]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for spec in specs:
        grouped[(spec.scope, spec.memory_type)].append(spec.entry_id)
    return [entry_ids for entry_ids in grouped.values() if len(entry_ids) > 1]


__all__ = [
    "CandidateRelatedness",
    "DEFAULT_GOVERNANCE_ALIAS_GROUPS",
    "GovernanceRelatednessService",
    "MemoryGovernanceRelatednessOptions",
    "RelatedCanonicalMemory",
    "RelatednessAvailability",
    "RelatednessBatchResult",
    "RelatednessExecutionContext",
]
