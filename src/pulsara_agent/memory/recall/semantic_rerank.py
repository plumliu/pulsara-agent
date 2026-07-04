"""Query-time semantic reranking of already-filtered canonical memories."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from pulsara_agent.memory.canonical.query import CanonicalNodeView
from pulsara_agent.memory.recall.service import RecallItem
from pulsara_agent.retrieval.rerank.protocol import RerankProvider


@dataclass(frozen=True, slots=True)
class SemanticRerankOutcome:
    items: tuple[RecallItem, ...]
    scores: dict[str, float]
    below_threshold_ids: tuple[str, ...]


@dataclass(slots=True)
class RecallRerankService:
    provider: RerankProvider
    top_m: int = 20
    minimum_score: float = 0.55

    async def rerank(
        self,
        query_text: str,
        items: Sequence[RecallItem],
        views: Mapping[str, CanonicalNodeView],
    ) -> SemanticRerankOutcome:
        selected = list(items[: self.top_m])
        documents = [_document(views[item.memory_id]) for item in selected]
        scores = await self.provider.rerank(
            query_text,
            documents,
            instruction="Rank durable memories by relevance to the user's current request.",
            top_n=len(documents),
        )
        by_index = {result.index: result.score for result in scores}
        reranked: list[RecallItem] = []
        below_threshold_ids: list[str] = []
        scores_by_id: dict[str, float] = {}
        for index, item in enumerate(selected):
            score = by_index.get(index, 0.0)
            scores_by_id[item.memory_id] = score
            preserve_conflict = "contradiction_warning" in item.why
            if score < self.minimum_score and not preserve_conflict:
                below_threshold_ids.append(item.memory_id)
                continue
            reranked.append(
                replace(
                    item,
                    score=item.score + score,
                    why=tuple(dict.fromkeys((*item.why, f"reranker:{self.provider.model_id}"))),
                    channel_scores={**item.channel_scores, "reranker": score},
                )
            )
        reranked.sort(key=lambda item: (-item.score, item.memory_id))
        return SemanticRerankOutcome(
            items=tuple(reranked),
            scores=scores_by_id,
            below_threshold_ids=tuple(below_threshold_ids),
        )


def _document(view: CanonicalNodeView) -> str:
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
