"""Minimal text embedding shape over the OpenAI SDK public POST entrypoint."""

import asyncio
from collections.abc import Sequence
import json
import openai
from openai.types import CreateEmbeddingResponse

from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.llm.adapters.openai.client import (
    admit_provider_request,
    openai_auth_request_options,
)
from pulsara_agent.retrieval.config import RetrievalConnection, EmbeddingBackendConfig
from pulsara_agent.retrieval.http import sdk_client
from pulsara_agent.retrieval.errors import EmbeddingServiceError
from pulsara_agent.retrieval.embedding.validation import freeze_v1_embedding_vector

MAXIMUM_EMBEDDING_REQUEST_BODY_BYTES = 16 * 1024 * 1024


class HttpEmbeddingProvider:
    def __init__(
        self,
        connection: RetrievalConnection,
        *,
        config=EmbeddingBackendConfig(),
        semaphore=None,
    ):
        if (
            connection.shape != "openai_embedding"
            or not connection.complete
            or config.dimensions != 1024
        ):
            raise ValueError("embedding configuration is invalid")
        if not 1 <= config.batch_size <= 10 or not 1 <= config.max_concurrent <= 5:
            raise ValueError("embedding physical bounds are invalid")
        self.connection = connection
        self.config = config
        self.model_id = connection.model_id
        self.dimensions = 1024
        self._semaphore = semaphore or asyncio.Semaphore(config.max_concurrent)

    async def aclose(self):
        return None

    async def embed(self, text):
        return (await self._embed_chunk([text]))[0]

    async def embed_batch(self, texts: Sequence[str]):
        chunks = [
            list(texts[offset : offset + self.config.batch_size])
            for offset in range(0, len(texts), self.config.batch_size)
        ]
        results = await asyncio.gather(*(self._embed_chunk(chunk) for chunk in chunks))
        return [vector for chunk in results for vector in chunk]

    async def _embed_chunk(self, texts):
        _validate_embedding_inputs(texts)
        payload = {"model": self.model_id, "input": texts}
        if (
            len(json.dumps(payload, ensure_ascii=False).encode())
            > MAXIMUM_EMBEDDING_REQUEST_BODY_BYTES
        ):
            raise EmbeddingServiceError("embedding request exceeds its byte bound")
        async with self._semaphore:
            client, boundary = sdk_client(
                self.connection,
                timeout_seconds=self.config.timeout_seconds,
                maximum_response_bytes=MAXIMUM_EMBEDDING_REQUEST_BODY_BYTES,
            )
            try:
                async with client:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        options = openai_auth_request_options(
                            requires_api_key=self.connection.authentication != "none"
                        )
                        response = await admit_provider_request(
                            credential_boundary=boundary,
                            payload=payload,
                            operation=lambda: client.post(
                                self.connection.endpoint,
                                cast_to=CreateEmbeddingResponse,
                                body=payload,
                                options={"headers": options.get("extra_headers", {})},
                            ),
                        )
            except (ValueError, openai.OpenAIError, TimeoutError) as exc:
                message = (
                    str(exc).replace(self.connection.api_key, "[REDACTED]")
                    if self.connection.api_key
                    else str(exc)
                )
                raise EmbeddingServiceError(
                    f"embedding request failed: {message}"
                ) from None
        vectors = [None] * len(texts)
        try:
            for item in response.data:
                index = item.index
                if (
                    type(index) is not int
                    or index < 0
                    or index >= len(texts)
                    or vectors[index] is not None
                ):
                    raise ValueError("embedding response index is invalid")
                vectors[index] = list(freeze_v1_embedding_vector(item.embedding))
            if any(item is None for item in vectors):
                raise ValueError("embedding response omitted inputs")
        except (TypeError, AttributeError, ValueError) as exc:
            raise EmbeddingServiceError(f"embedding response invalid: {exc}") from exc
        return vectors


def _validate_embedding_inputs(texts):
    if not 1 <= len(texts) <= 10:
        raise EmbeddingServiceError("embedding batch is outside 1..10 items")
    estimator = PulsaraHeuristicTokenEstimatorV3()
    estimates = [estimator.estimate_text(value) for value in texts]
    if any(value < 1 or value > 8192 for value in estimates) or sum(estimates) > 81920:
        raise EmbeddingServiceError("embedding input exceeds the local token boundary")
