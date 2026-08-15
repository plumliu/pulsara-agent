"""OpenAI-compatible embedding provider.

This fits Aliyun Bailian ``text-embedding-v4`` nicely because Bailian exposes
an OpenAI-compatible embedding endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json

import openai

from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.retrieval.errors import EmbeddingServiceError
from pulsara_agent.retrieval.embedding.validation import (
    freeze_v1_embedding_vector,
)


MAXIMUM_EMBEDDING_REQUEST_BODY_BYTES = 16 * 1024 * 1024


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
        if model != "text-embedding-v4" or dimensions != 1024:
            raise ValueError("embedding configuration is outside the V1 vector space")
        if not 1 <= batch_size <= 10 or not 1 <= max_concurrent <= 5:
            raise ValueError("embedding physical bounds are invalid")
        self.model_id = model
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

    async def aclose(self) -> None:
        await self._client.close()

    async def embed(self, text: str) -> list[float]:
        _validate_embedding_inputs((text,))
        vectors = await self._embed_chunk([text])
        return vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        _validate_embedding_inputs(texts)
        chunks = [
            list(texts[offset : offset + self._batch_size])
            for offset in range(0, len(texts), self._batch_size)
        ]
        results = await asyncio.gather(*(self._embed_chunk(chunk) for chunk in chunks))
        return [vector for chunk in results for vector in chunk]

    async def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        _validate_embedding_request_body(
            model=self._model,
            dimensions=self.dimensions,
            texts=texts,
        )
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
        if getattr(response, "model", None) != self._model:
            raise EmbeddingServiceError(
                "Embedding response model violates the sealed V1 contract."
            )
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in response.data:
            try:
                index = int(item.index)
            except (AttributeError, TypeError, ValueError) as exc:
                raise EmbeddingServiceError(
                    "Embedding response item missing index."
                ) from exc
            if index < 0 or index >= len(texts):
                raise EmbeddingServiceError(
                    f"Embedding response index out of range: {index}"
                )
            if vectors[index] is not None:
                raise EmbeddingServiceError(
                    f"Duplicate embedding response index: {index}"
                )
            vectors[index] = list(item.embedding)
        if any(vector is None for vector in vectors):
            raise EmbeddingServiceError(
                "Embedding response missing one or more vectors."
            )
        result = [vector for vector in vectors if vector is not None]
        validated: list[list[float]] = []
        for vector in result:
            try:
                frozen = freeze_v1_embedding_vector(vector)
            except ValueError as exc:
                raise EmbeddingServiceError(
                    "Embedding response vector violates the V1 contract."
                ) from exc
            validated.append(list(frozen))
        return validated


def _validate_embedding_inputs(texts: Sequence[str]) -> None:
    if not 1 <= len(texts) <= 10:
        raise EmbeddingServiceError("Embedding batch is outside 1..10 items.")
    estimator = PulsaraHeuristicTokenEstimatorV1()
    token_ceilings = tuple(estimator.estimate_text(value) for value in texts)
    if any(value < 1 or value > 8_192 for value in token_ceilings):
        raise EmbeddingServiceError("Embedding item exceeds 8192 local token units.")
    if sum(token_ceilings) > 81_920:
        raise EmbeddingServiceError(
            "Embedding batch exceeds the Pulsara-local aggregate token ceiling."
        )


def _validate_embedding_request_body(
    *, model: str, dimensions: int, texts: Sequence[str]
) -> None:
    encoded = json.dumps(
        {
            "model": model,
            "input": tuple(texts),
            "dimensions": dimensions,
            "encoding_format": "float",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_EMBEDDING_REQUEST_BODY_BYTES:
        raise EmbeddingServiceError(
            "Embedding request exceeds the HTTP body bound before provider open."
        )
