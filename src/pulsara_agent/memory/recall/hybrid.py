"""Hybrid sparse+dense durable-memory recall orchestration."""

from __future__ import annotations

import asyncio
import time

from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery
from pulsara_agent.memory.recall.candidates import CandidateBatch
from pulsara_agent.memory.recall.dense import DenseCandidateService
from pulsara_agent.memory.recall.rerank import direct_relation_rerank
from pulsara_agent.memory.recall.semantic_rerank import RecallRerankService
from pulsara_agent.memory.recall.service import (
    LexicalMemoryRecallService,
    RecallItem,
    RecallQuery,
    RecallResult,
    RecallStatus,
    RecallTrigger,
    _dedupe_ids,
    _elapsed_ms,
    _empty_guidance,
    _rrf_ranked_ids,
    _snippet,
    _stabilize_visible_conflicts,
    _trim_recall_items,
)
from pulsara_agent.memory.recall.sparse import SparseCandidateService
from pulsara_agent.memory.recall.trace import RecallTraceStore


class HybridMemoryRecallService(LexicalMemoryRecallService):
    def __init__(
        self,
        *,
        memory_query: MemoryQuery,
        sparse: SparseCandidateService,
        dense: DenseCandidateService | None,
        reranker: RecallRerankService | None,
        trace_store: RecallTraceStore | None = None,
        rrf_k: int = 60,
        allow_needs_review: bool = False,
        enable_graph_rerank: bool = True,
        recent_suppression_limit: int = 10,
        auto_dense_timeout_seconds: float = 0.3,
        explicit_dense_timeout_seconds: float = 4.0,
        explicit_rerank_timeout_seconds: float = 4.0,
        explicit_total_deadline_seconds: float = 8.0,
    ) -> None:
        super().__init__(
            memory_query=memory_query,
            trace_store=trace_store,
            rrf_k=rrf_k,
            allow_needs_review=allow_needs_review,
            enable_graph_rerank=enable_graph_rerank,
            recent_suppression_limit=recent_suppression_limit,
        )
        self.sparse = sparse
        self.dense = dense
        self.reranker = reranker
        self.auto_dense_timeout_seconds = auto_dense_timeout_seconds
        self.explicit_dense_timeout_seconds = explicit_dense_timeout_seconds
        self.explicit_rerank_timeout_seconds = explicit_rerank_timeout_seconds
        self.explicit_total_deadline_seconds = explicit_total_deadline_seconds

    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        if query.trigger is not RecallTrigger.EXPLICIT_SEARCH:
            return await self._recall_impl(query, graph_id=graph_id)
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._recall_impl(query, graph_id=graph_id),
                timeout=self.explicit_total_deadline_seconds,
            )
        except TimeoutError:
            result = RecallResult(
                status=RecallStatus.UNAVAILABLE,
                warnings=("explicit_recall_deadline_exceeded",),
                metadata={"deadline_seconds": self.explicit_total_deadline_seconds},
            )
            self._record_trace(
                query,
                result,
                graph_id=graph_id,
                candidate_ids=(),
                latency_ms=_elapsed_ms(started),
            )
            return result

    async def _recall_impl(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        started = time.perf_counter()
        if not query.text.strip():
            result = RecallResult(status=RecallStatus.EMPTY, guidance=_empty_guidance())
            self._record_trace(query, result, graph_id=graph_id, candidate_ids=(), latency_ms=0)
            return result
        deadline = (
            time.monotonic() + self.explicit_total_deadline_seconds
            if query.trigger is RecallTrigger.EXPLICIT_SEARCH
            else None
        )
        warnings: list[str] = []
        metadata: dict[str, object] = {"fusion": "rrf", "rrf_k": self.rrf_k}

        sparse_task = asyncio.create_task(self.sparse.collect(query, graph_id=graph_id))
        dense_task = (
            asyncio.create_task(self.dense.collect(query, graph_id=graph_id))
            if self.dense is not None
            else None
        )
        batches: list[CandidateBatch] = []
        sparse_failed = False
        try:
            sparse_batch = await sparse_task
            batches.append(sparse_batch)
            metadata.update(sparse_batch.metadata)
        except Exception as exc:
            sparse_failed = True
            warnings.append(f"sparse_degraded:{type(exc).__name__}: {exc}")

        if dense_task is not None:
            timeout = (
                self.auto_dense_timeout_seconds
                if query.trigger is RecallTrigger.CHEAP_AUTO
                else min(self.explicit_dense_timeout_seconds, _remaining(deadline))
            )
            try:
                dense_batch = await asyncio.wait_for(dense_task, timeout=max(0.001, timeout))
                batches.append(dense_batch)
                warnings.extend(dense_batch.warnings)
                metadata.update(dense_batch.metadata)
            except TimeoutError:
                warnings.append("dense_degraded:timeout")
                metadata["dense_query"] = "timeout"
            except Exception as exc:
                warnings.append(f"dense_degraded:{type(exc).__name__}: {exc}")
                metadata["dense_query"] = "degraded"
        else:
            metadata["dense_query"] = "disabled"

        channels = tuple(channel for batch in batches for channel in batch.channel_rows())
        ranked_ids, why_by_id = _rrf_ranked_ids(channels=channels, k=self.rrf_k)
        candidate_ids = tuple(ranked_ids)
        metadata["candidate_channels"] = {
            channel: [memory_id for memory_id, _ in rows] for channel, rows in channels
        }
        if not ranked_ids:
            status = RecallStatus.UNAVAILABLE if sparse_failed and not batches else RecallStatus.EMPTY
            result = RecallResult(
                status=status,
                guidance=_empty_guidance() if status is RecallStatus.EMPTY else (),
                warnings=tuple(warnings),
                metadata=metadata,
            )
            self._record_trace(
                query, result, graph_id=graph_id, candidate_ids=candidate_ids, latency_ms=_elapsed_ms(started)
            )
            return result

        channel_scores = _channel_scores(channels)
        fused_scores = _fused_scores(channels, self.rrf_k)
        metadata["channel_scores"] = channel_scores
        items, views, filtered_ids = await asyncio.to_thread(
            self._canonical_items,
            query,
            graph_id,
            ranked_ids,
            why_by_id,
            channel_scores,
            fused_scores,
            warnings,
        )

        if query.trigger is RecallTrigger.EXPLICIT_SEARCH and self.reranker is not None and items:
            timeout = min(self.explicit_rerank_timeout_seconds, _remaining(deadline))
            try:
                rerank_outcome = await asyncio.wait_for(
                    self.reranker.rerank(query.text, items, views),
                    timeout=max(0.001, timeout),
                )
                items = list(rerank_outcome.items)
                metadata["reranker_model"] = self.reranker.provider.model_id
                metadata["reranked_ids"] = [item.memory_id for item in items]
                metadata["reranker_scores"] = rerank_outcome.scores
                metadata["reranker_min_score"] = self.reranker.minimum_score
                metadata["reranker_below_threshold_ids"] = list(
                    rerank_outcome.below_threshold_ids
                )
            except TimeoutError:
                warnings.append("rerank_degraded:timeout")
            except Exception as exc:
                warnings.append(f"rerank_degraded:{type(exc).__name__}: {exc}")

        if self.enable_graph_rerank and items:
            items = direct_relation_rerank(items, views)
        items = _stabilize_visible_conflicts(_trim_recall_items(items, query.limit))
        result = RecallResult(
            status=RecallStatus.OK if items else RecallStatus.EMPTY,
            items=tuple(items),
            filtered_ids=_dedupe_ids(filtered_ids),
            guidance=() if items else _empty_guidance(),
            warnings=tuple(warnings),
            metadata=metadata,
        )
        self._record_trace(
            query, result, graph_id=graph_id, candidate_ids=candidate_ids, latency_ms=_elapsed_ms(started)
        )
        return result

    def _canonical_items(
        self,
        query: RecallQuery,
        graph_id: str | None,
        ranked_ids: list[str],
        why_by_id: dict[str, list[str]],
        channel_scores: dict[str, dict[str, float]],
        fused_scores: dict[str, float],
        warnings: list[str],
    ) -> tuple[list[RecallItem], dict[str, CanonicalNodeView], list[str]]:
        suppressed_ids = self._recent_suppressed_ids(query, graph_id=graph_id, warnings=warnings)
        views = {view.id: view for view in self.memory_query.fetch_nodes(ranked_ids, graph_id=graph_id)}
        filtered_ids: list[str] = []
        items: list[RecallItem] = []
        candidate_limit = max(query.limit * 4, query.limit)
        for memory_id in ranked_ids:
            view = views.get(memory_id)
            if memory_id in suppressed_ids or view is None or not self._passes_canonical_filter(view, query):
                if memory_id not in filtered_ids:
                    filtered_ids.append(memory_id)
                continue
            per_channel = channel_scores.get(memory_id, {})
            items.append(
                RecallItem(
                    memory_id=view.id,
                    memory_type=view.memory_type,
                    scope=view.scope,
                    status=view.status,
                    snippet=_snippet(view),
                    score=fused_scores.get(memory_id, 0.0),
                    why=tuple(why_by_id.get(memory_id, ())),
                    deep_recall=f"memory_get {view.id}",
                    channel_scores=dict(per_channel),
                )
            )
            if len(items) >= candidate_limit:
                break
        if items:
            items, views = self._expand_contradiction_companions(
                items,
                views,
                query,
                graph_id=graph_id,
                suppressed_ids=suppressed_ids,
                filtered_ids=filtered_ids,
            )
        return items, views, filtered_ids


def _channel_scores(
    channels: tuple[tuple[str, list[tuple[str, float]]], ...]
) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for channel, rows in channels:
        for memory_id, score in rows:
            scores.setdefault(memory_id, {})[channel] = float(score)
    return scores


def _fused_scores(
    channels: tuple[tuple[str, list[tuple[str, float]]], ...],
    k: int,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for _channel, rows in channels:
        for rank, (memory_id, _raw_score) in enumerate(rows, start=1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank)
    return scores


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return 3600.0
    return max(0.0, deadline - time.monotonic())
