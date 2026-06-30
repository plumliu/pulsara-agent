"""Sparse lexical and PostgreSQL FTS candidate generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.candidates import CandidateBatch, ChannelCandidate
from pulsara_agent.memory.recall.service import RecallQuery, RecallTrigger
from pulsara_agent.retrieval.tokenizer.protocol import Tokenizer


@dataclass(slots=True)
class SparseCandidateService:
    memory_query: MemoryQuery
    tokenizer: Tokenizer
    lexical_limit: int = 20
    fts_limit: int = 40
    auto_lexical_min_score: float = 4.0
    explicit_lexical_min_score: float = 2.0

    async def collect(
        self,
        query: RecallQuery,
        *,
        graph_id: str | None = None,
    ) -> CandidateBatch:
        return await asyncio.to_thread(self._collect_sync, query, graph_id)

    def _collect_sync(self, query: RecallQuery, graph_id: str | None) -> CandidateBatch:
        terms = tuple(dict.fromkeys(self.tokenizer.tokenize(query.text)))
        if not terms:
            return CandidateBatch(metadata={"lexical_candidate_ids": [], "fts_candidate_ids": []})
        lexical = self.memory_query.lexical_candidates(
            terms=terms,
            scopes=query.scopes or None,
            types=query.types or None,
            limit=self.lexical_limit,
            graph_id=graph_id,
        )
        lexical_min_score = (
            self.auto_lexical_min_score
            if query.trigger is RecallTrigger.CHEAP_AUTO
            else self.explicit_lexical_min_score
        )
        lexical_dropped_ids = [
            memory_id for memory_id, score in lexical if score < lexical_min_score
        ]
        lexical = [
            (memory_id, score) for memory_id, score in lexical if score >= lexical_min_score
        ]
        fts = self.memory_query.fts_candidates(
            query_text=query.text,
            scopes=query.scopes or None,
            types=query.types or None,
            limit=self.fts_limit,
            graph_id=graph_id,
        )
        candidates = tuple(
            ChannelCandidate(memory_id=memory_id, channel=channel, raw_score=score, rank=rank)
            for channel, rows in (("lexical", lexical), ("fts", fts))
            for rank, (memory_id, score) in enumerate(rows, start=1)
        )
        return CandidateBatch(
            candidates=candidates,
            metadata={
                "lexical_candidate_ids": [memory_id for memory_id, _ in lexical],
                "fts_candidate_ids": [memory_id for memory_id, _ in fts],
                "lexical_min_score": lexical_min_score,
                "lexical_below_threshold_ids": lexical_dropped_ids,
            },
        )
