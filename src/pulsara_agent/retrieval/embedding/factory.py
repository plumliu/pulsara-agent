"""Factories for embedding providers."""

from __future__ import annotations

from pulsara_agent.retrieval.config import EmbeddingBackendConfig

from .openai_compatible import OpenAICompatibleEmbeddingProvider
from .protocol import EmbeddingProvider


def build_embedding_provider(config: EmbeddingBackendConfig) -> EmbeddingProvider:
    if not config.model:
        raise ValueError("Embedding model is not configured.")
    if not config.api_key:
        raise ValueError("Embedding api_key is not configured.")
    if not config.base_url:
        raise ValueError("Embedding base_url is not configured.")
    if config.provider == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            dimensions=config.dimensions,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            batch_size=config.batch_size,
            max_concurrent=config.max_concurrent,
        )
    raise ValueError(f"Unknown embedding provider: {config.provider!r}")
