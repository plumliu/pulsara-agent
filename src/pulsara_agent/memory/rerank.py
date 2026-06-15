"""Graph-aware in-process memory reranking."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from pulsara_agent.memory.query import CanonicalNodeView
from pulsara_agent.memory.recall import RecallItem
from pulsara_agent.ontology import memory


def direct_relation_rerank(
    items: list[RecallItem],
    views: Mapping[str, CanonicalNodeView],
) -> list[RecallItem]:
    """Apply small grounded bonuses from direct materialized relations."""

    reranked: list[RecallItem] = []
    for item in items:
        view = views.get(item.memory_id)
        if view is None:
            reranked.append(item)
            continue
        bonus, reasons = _direct_relation_bonus(view)
        if bonus == 0:
            reranked.append(item)
            continue
        why = tuple(dict.fromkeys((*item.why, *reasons)))
        reranked.append(replace(item, score=item.score + bonus, why=why))
    reranked.sort(key=lambda candidate: (-candidate.score, candidate.memory_id))
    return reranked


def _direct_relation_bonus(view: CanonicalNodeView) -> tuple[float, tuple[str, ...]]:
    bonus = 0.0
    reasons: list[str] = []
    support_count = sum(1 for predicate, _source_id in view.incoming if predicate == memory.SUPPORTS.name)
    if support_count:
        bonus += min(0.02, support_count * 0.005)
        reasons.append("evidence_support")
    supersedes_count = sum(1 for predicate, _target_id in view.outgoing if predicate == memory.SUPERSEDES.name)
    if supersedes_count:
        bonus += min(0.02, supersedes_count * 0.01)
        reasons.append("supersedes_edge")
    contradiction_count = sum(1 for predicate, _ in (*view.incoming, *view.outgoing) if predicate == memory.CONTRADICTS.name)
    if contradiction_count:
        reasons.append("contradiction_warning")
    return bonus, tuple(reasons)
