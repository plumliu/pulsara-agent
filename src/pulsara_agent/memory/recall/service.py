"""Lexical durable-memory recall."""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery
from pulsara_agent.memory.recall.graph import GraphCandidateService, RecallPath
from pulsara_agent.memory.recall.trace import RecallTraceStore
from pulsara_agent.ontology import memory


class RecallTrigger(StrEnum):
    CHEAP_AUTO = "cheap_auto"
    EXPLICIT_SEARCH = "explicit_search"


class RecallStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RecallQuery:
    text: str
    scopes: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    limit: int = 5
    max_hops: int = 0
    trigger: RecallTrigger = RecallTrigger.CHEAP_AUTO
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    reply_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecallItem:
    memory_id: str
    memory_type: str
    scope: str
    status: memory.NodeStatus
    snippet: str
    score: float
    why: tuple[str, ...]
    deep_recall: str
    conflicts_with: tuple[str, ...] = ()
    channel_scores: dict[str, float] = field(default_factory=dict)
    direct_match: bool = True
    hop_count: int = 0
    paths: tuple[RecallPath, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallResult:
    status: RecallStatus
    items: tuple[RecallItem, ...] = ()
    filtered_ids: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryRecallService(Protocol):
    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult: ...


@dataclass(slots=True)
class LexicalMemoryRecallService(MemoryRecallService):
    memory_query: MemoryQuery
    trace_store: RecallTraceStore | None = None
    lexical_limit: int = 20
    fts_limit: int = 40
    rrf_k: int = 60
    allow_needs_review: bool = False
    enable_graph_rerank: bool = True
    recent_suppression_limit: int = 10
    unavailable_cooldown_seconds: float = 2.0
    graph_candidates: GraphCandidateService | None = None
    _cooldown_until: float = field(default=0.0, init=False, repr=False)

    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        if self._cooldown_until > time.monotonic():
            result = RecallResult(
                status=RecallStatus.UNAVAILABLE,
                warnings=("recall_backend_cooldown",),
            )
            self._record_trace(
                query,
                result,
                graph_id=graph_id,
                candidate_ids=(),
                latency_ms=0,
            )
            return result
        started = time.perf_counter()
        try:
            result, candidate_ids = await asyncio.to_thread(self._recall_sync, query, graph_id)
        except Exception as exc:
            self._cooldown_until = time.monotonic() + self.unavailable_cooldown_seconds
            result = RecallResult(
                status=RecallStatus.UNAVAILABLE,
                warnings=(f"recall_backend_unavailable:{type(exc).__name__}: {exc}",),
            )
            self._record_trace(
                query,
                result,
                graph_id=graph_id,
                candidate_ids=(),
                latency_ms=_elapsed_ms(started),
            )
            return result
        self._record_trace(
            query,
            result,
            graph_id=graph_id,
            candidate_ids=candidate_ids,
            latency_ms=_elapsed_ms(started),
        )
        return result

    def _recall_sync(self, query: RecallQuery, graph_id: str | None) -> tuple[RecallResult, tuple[str, ...]]:
        terms = _query_terms(query.text)
        if not terms:
            return RecallResult(status=RecallStatus.EMPTY, guidance=_empty_guidance()), ()

        scopes = query.scopes or None
        types = query.types or None
        warnings: list[str] = []
        metadata: dict[str, Any] = {
            "graph_max_hops": query.max_hops if query.trigger is RecallTrigger.EXPLICIT_SEARCH else 0,
        }
        suppressed_ids = self._recent_suppressed_ids(query, graph_id=graph_id, warnings=warnings)
        lexical = self.memory_query.lexical_candidates(
            terms=terms,
            scopes=scopes,
            types=types,
            limit=self.lexical_limit,
            graph_id=graph_id,
        )
        fts = self.memory_query.fts_candidates(
            query_text=query.text,
            scopes=scopes,
            types=types,
            limit=self.fts_limit,
            graph_id=graph_id,
        )
        direct_channels = (("lexical", lexical), ("fts", fts))
        direct_ranked_ids, _ = _rrf_ranked_ids(channels=direct_channels, k=self.rrf_k)
        graph_paths: dict[str, tuple[RecallPath, ...]] = {}
        graph_rows: list[tuple[str, float]] = []
        if (
            query.trigger is RecallTrigger.EXPLICIT_SEARCH
            and query.max_hops > 0
            and self.graph_candidates is not None
            and direct_ranked_ids
        ):
            try:
                graph_outcome = self.graph_candidates.collect(
                    seed_ids=direct_ranked_ids,
                    scopes=query.scopes,
                    types=query.types,
                    max_hops=query.max_hops,
                    graph_id=graph_id,
                    suppressed_ids=suppressed_ids,
                )
                graph_rows = list(graph_outcome.batch.channel_rows()[0][1]) if graph_outcome.batch.candidates else []
                graph_paths = graph_outcome.paths_by_id
                warnings.extend(graph_outcome.batch.warnings)
                metadata.update(graph_outcome.batch.metadata)
            except Exception as exc:
                warnings.append(f"graph_expand_degraded:{type(exc).__name__}: {exc}")
                metadata["graph_query"] = "degraded"
        channels = (*direct_channels, ("graph", graph_rows)) if graph_rows else direct_channels
        ranked_ids, why_by_id = _rrf_ranked_ids(channels=channels, k=self.rrf_k)
        candidate_ids = tuple(ranked_ids)
        if not ranked_ids:
            return RecallResult(
                status=RecallStatus.EMPTY,
                guidance=_empty_guidance(),
                warnings=tuple(warnings),
                metadata=metadata,
            ), candidate_ids

        views = {view.id: view for view in self.memory_query.fetch_nodes(ranked_ids, graph_id=graph_id)}
        items: list[RecallItem] = []
        filtered_ids: list[str] = []
        candidate_limit = max(query.limit * 4, query.limit)
        for memory_id in ranked_ids:
            if memory_id in suppressed_ids:
                _append_filtered_id(filtered_ids, memory_id)
                continue
            view = views.get(memory_id)
            if view is None:
                _append_filtered_id(filtered_ids, memory_id)
                continue
            if not self._passes_canonical_filter(view, query):
                _append_filtered_id(filtered_ids, memory_id)
                continue
            items.append(
                RecallItem(
                    memory_id=view.id,
                    memory_type=view.memory_type,
                    scope=view.scope,
                    status=view.status,
                    snippet=_snippet(view),
                    score=_score_for(memory_id, channels=channels, k=self.rrf_k),
                    why=tuple(why_by_id.get(memory_id, ())),
                    deep_recall=f"memory_get {view.id}",
                    direct_match=memory_id in direct_ranked_ids,
                    hop_count=0 if memory_id in direct_ranked_ids else _minimum_hops(graph_paths.get(memory_id, ())),
                    paths=graph_paths.get(memory_id, ()),
                )
            )
            if len(items) >= candidate_limit:
                break

        if self.enable_graph_rerank and items:
            from pulsara_agent.memory.recall.rerank import direct_relation_rerank

            items = direct_relation_rerank(items, views)
        items = _trim_recall_items(items, query.limit)
        items = _expand_contradiction_companions(
            items,
            memory_query=self.memory_query,
            graph_id=graph_id,
            suppressed_ids=suppressed_ids,
            passes_filter=self._passes_canonical_filter,
            query=query,
        )
        items = _mark_visible_conflicts(items, views, {item.memory_id for item in items})

        if not items:
            return RecallResult(
                status=RecallStatus.EMPTY,
                filtered_ids=_dedupe_ids(filtered_ids),
                guidance=_empty_guidance(),
                warnings=tuple(warnings),
                metadata=metadata,
            ), candidate_ids
        return RecallResult(
            status=RecallStatus.OK,
            items=tuple(items),
            filtered_ids=_dedupe_ids(filtered_ids),
            warnings=tuple(warnings),
            metadata=metadata,
        ), candidate_ids

    def _passes_canonical_filter(self, view: CanonicalNodeView, query: RecallQuery) -> bool:
        if view.status is memory.NodeStatus.REJECTED:
            return False
        if view.status is not memory.NodeStatus.ACTIVE:
            if not (query.trigger is RecallTrigger.EXPLICIT_SEARCH and self.allow_needs_review):
                return False
        if query.scopes and view.scope not in query.scopes:
            return False
        if query.types and view.memory_type not in query.types:
            return False
        return True

    def _recent_suppressed_ids(
        self,
        query: RecallQuery,
        *,
        graph_id: str | None,
        warnings: list[str],
    ) -> set[str]:
        if (
            self.trace_store is None
            or query.trigger is not RecallTrigger.CHEAP_AUTO
            or query.session_id is None
            or self.recent_suppression_limit <= 0
        ):
            return set()
        try:
            return set(
                self.trace_store.recent_injected_ids(
                    graph_id=graph_id,
                    session_id=query.session_id,
                    limit=self.recent_suppression_limit,
                )
            )
        except Exception as exc:
            warnings.append(f"recall_suppression_unavailable:{type(exc).__name__}: {exc}")
            return set()

    def _record_trace(
        self,
        query: RecallQuery,
        result: RecallResult,
        *,
        graph_id: str | None,
        candidate_ids: Sequence[str],
        latency_ms: int,
    ) -> None:
        if self.trace_store is None or not _has_trace_coordinates(query):
            return
        try:
            self.trace_store.record(
                graph_id=graph_id,
                session_id=query.session_id or "",
                run_id=query.run_id or "",
                turn_id=query.turn_id or "",
                reply_id=query.reply_id or "",
                query_text=query.text,
                trigger_kind=query.trigger.value,
                candidate_ids=candidate_ids,
                included_ids=[item.memory_id for item in result.items],
                filtered_ids=result.filtered_ids,
                warnings=result.warnings,
                latency_ms=latency_ms,
                injected=query.trigger is RecallTrigger.CHEAP_AUTO,
                selected_by_tool=query.trigger is RecallTrigger.EXPLICIT_SEARCH,
                metadata=result.metadata,
            )
        except Exception:
            return


def _rrf_ranked_ids(
    *,
    channels: Sequence[tuple[str, Sequence[tuple[str, float]]]],
    k: int,
) -> tuple[list[str], dict[str, list[str]]]:
    scores: defaultdict[str, float] = defaultdict(float)
    why_by_id: defaultdict[str, list[str]] = defaultdict(list)
    for channel_name, rows in channels:
        for rank, (memory_id, _raw_score) in enumerate(rows, start=1):
            scores[memory_id] += 1.0 / (k + rank)
            why_by_id[memory_id].append(channel_name)
    ranked = sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))
    return ranked, dict(why_by_id)


def _score_for(
    memory_id: str,
    *,
    channels: Sequence[tuple[str, Sequence[tuple[str, float]]]],
    k: int,
) -> float:
    score = 0.0
    for _channel, rows in channels:
        for rank, (candidate_id, _raw_score) in enumerate(rows, start=1):
            if candidate_id == memory_id:
                score += 1.0 / (k + rank)
                break
    return score


def _query_terms(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for raw in re.findall(r"[\w:/.\-#]+", text.casefold()):
        term = raw.strip("._-:/#")
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return tuple(terms)


def _snippet(view: CanonicalNodeView, max_chars: int = 240) -> str:
    text = view.summary or view.statement
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def _empty_guidance() -> tuple[str, ...]:
    return (
        "Try fewer or more distinctive terms.",
        "Use history search for verbatim past conversation.",
        "Verify current files or tools when asking about current state.",
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _has_trace_coordinates(query: RecallQuery) -> bool:
    return all((query.session_id, query.run_id, query.turn_id, query.reply_id))


def _contradiction_ids(view: CanonicalNodeView) -> tuple[str, ...]:
    ids: list[str] = []
    for predicate, memory_id in (*view.outgoing, *view.incoming):
        if predicate == memory.CONTRADICTS.name and memory_id not in ids:
            ids.append(memory_id)
    return tuple(ids)


def _expand_contradiction_companions(
    items: list[RecallItem],
    *,
    memory_query: MemoryQuery,
    graph_id: str | None,
    suppressed_ids: set[str],
    passes_filter,
    query: RecallQuery,
) -> list[RecallItem]:
    """Surface the active contradiction partner of any recalled memory.

    Governance keeps two conflicting memories ACTIVE and links them with a
    non-destructive ``CONTRADICTS`` edge; that edge is only useful at recall
    time if BOTH sides reach the model. Plain recall may surface only one side,
    so we always (including the 0-hop automatic path) pull in the missing
    partner. This is the one relation where leaving the neighbor unexpanded is a
    correctness hazard, not just incomplete context — every other typed-graph
    relation stays behind ``max_hops>=1``.

    Companions are appended beyond ``limit`` (they are safety additions, not
    ranking competitors) and are subject to the SAME scope/type/status
    visibility and suppression rules as any recalled node, so a hidden- or
    out-of-scope partner is never leaked.
    """
    surfaced = {item.memory_id for item in items}
    wanted: dict[str, list[str]] = {}
    # Collect contradiction partners from each surfaced item's view edges.
    views = {view.id: view for view in memory_query.fetch_nodes(list(surfaced), graph_id=graph_id)}
    for item in items:
        view = views.get(item.memory_id)
        if view is None:
            continue
        for counterpart_id in _contradiction_ids(view):
            if counterpart_id in surfaced or counterpart_id in suppressed_ids:
                continue
            wanted.setdefault(counterpart_id, []).append(item.memory_id)
    if not wanted:
        return items
    companion_views = {
        view.id: view
        for view in memory_query.fetch_nodes(sorted(wanted), graph_id=graph_id)
    }
    companions: list[RecallItem] = []
    for counterpart_id in sorted(wanted):
        view = companion_views.get(counterpart_id)
        if view is None or not passes_filter(view, query):
            continue
        sources = tuple(dict.fromkeys(wanted[counterpart_id]))
        companions.append(
            RecallItem(
                memory_id=view.id,
                memory_type=view.memory_type,
                scope=view.scope,
                status=view.status,
                snippet=_snippet(view),
                score=0.0,
                why=("contradiction_companion", "contradiction_warning"),
                deep_recall=f"memory_get {view.id}",
                conflicts_with=sources,
                direct_match=False,
                hop_count=1,
            )
        )
    return [*items, *companions]


def _mark_visible_conflicts(
    items: list[RecallItem],
    views: dict[str, CanonicalNodeView],
    visible_ids: set[str],
) -> list[RecallItem]:
    marked: list[RecallItem] = []
    for item in items:
        view = views.get(item.memory_id)
        if view is None:
            marked.append(item)
            continue
        conflicts = tuple(memory_id for memory_id in _contradiction_ids(view) if memory_id in visible_ids)
        if not conflicts:
            marked.append(item)
            continue
        why = tuple(dict.fromkeys((*item.why, "contradiction_warning")))
        marked.append(replace(item, why=why, conflicts_with=conflicts))
    return marked


def _append_filtered_id(filtered_ids: list[str], memory_id: str) -> None:
    if memory_id not in filtered_ids:
        filtered_ids.append(memory_id)


def _dedupe_ids(memory_ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(memory_ids))


def _trim_recall_items(items: list[RecallItem], limit: int) -> list[RecallItem]:
    if limit <= 0:
        return []
    return items[:limit]


def _minimum_hops(paths: Sequence[RecallPath]) -> int:
    return min((len(path.steps) for path in paths), default=0)
