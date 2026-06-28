"""OpenAI-compatible embedding provider.

This fits Aliyun Bailian ``text-embedding-v4`` nicely because Bailian exposes
an OpenAI-compatible embedding endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import openai

from pulsara_agent.retrieval.errors import EmbeddingServiceError


class OpenAICompatibleEmbeddingProvider:
    """Async embedding provider over an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        dimensions: int = 1024,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 10,
        max_concurrent: int = 5,
    ) -> None:
        self.dimensions = dimensions
        self._model = model
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    async def embed(self, text: str) -> list[float]:
        vectors = await self._embed_chunk([text])
        return vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        chunks = [
            list(texts[offset : offset + self._batch_size])
            for offset in range(0, len(texts), self._batch_size)
        ]
        results = await asyncio.gather(*(self._embed_chunk(chunk) for chunk in chunks))
        return [vector for chunk in results for vector in chunk]

    async def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        async with self._semaphore:
            try:
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=texts,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
            except openai.OpenAIError as exc:
                raise EmbeddingServiceError(str(exc)) from exc
        return [list(item.embedding) for item in response.data]
