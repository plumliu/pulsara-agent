"""Dense query embedding, bounded run-local cache, and vector candidates."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.memory.recall.candidates import CandidateBatch, ChannelCandidate
from pulsara_agent.memory.recall.service import RecallQuery, RecallTrigger
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider


@dataclass(slots=True)
class DenseCandidateService:
    provider: EmbeddingProvider
    vector_query: MemoryVectorQuery
    provider_name: str = "openai_compatible"
    auto_limit: int = 8
    explicit_limit: int = 30
    auto_min_score: float = 0.55
    explicit_min_score: float = 0.20
    cache_size: int = 64
    _cache: OrderedDict[tuple[str, str, str], tuple[float, ...]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def embedding_fingerprint(self) -> str:
        return f"{self.provider_name}:{self.provider.model_id}:{self.provider.dimensions}"

    async def collect(
        self,
        query: RecallQuery,
        *,
        graph_id: str | None = None,
    ) -> CandidateBatch:
        vector, cache_state = await self._query_embedding(query)
        limit = self.auto_limit if query.trigger is RecallTrigger.CHEAP_AUTO else self.explicit_limit
        rows = await asyncio.to_thread(
            self.vector_query.candidates,
            query_vector=vector,
            embedding_fingerprint=self.embedding_fingerprint,
            scopes=query.scopes or None,
            types=query.types or None,
            limit=limit,
            graph_id=graph_id,
        )
        minimum_score = (
            self.auto_min_score
            if query.trigger is RecallTrigger.CHEAP_AUTO
            else self.explicit_min_score
        )
        dropped_ids = [memory_id for memory_id, score in rows if score < minimum_score]
        rows = [(memory_id, score) for memory_id, score in rows if score >= minimum_score]
        return CandidateBatch(
            candidates=tuple(
                ChannelCandidate(
                    memory_id=memory_id,
                    channel="vector",
                    raw_score=score,
                    rank=rank,
                    embedding_fingerprint=self.embedding_fingerprint,
                )
                for rank, (memory_id, score) in enumerate(rows, start=1)
            ),
            metadata={
                "embedding_fingerprint": self.embedding_fingerprint,
                "vector_candidate_ids": [memory_id for memory_id, _ in rows],
                "dense_query": cache_state,
                "dense_min_score": minimum_score,
                "dense_below_threshold_ids": dropped_ids,
            },
        )

    async def _query_embedding(self, query: RecallQuery) -> tuple[list[float], str]:
        normalized = " ".join(query.text.split()).casefold()
        run_key = query.run_id or query.session_id or "unscoped"
        key = (run_key, self.embedding_fingerprint, normalized)
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return list(cached), "cache_hit"
        vector = await self.provider.embed(query.text)
        async with self._cache_lock:
            self._cache[key] = tuple(vector)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return vector, "remote_call"
