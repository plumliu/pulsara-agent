"""Factories for embedding providers."""

from __future__ import annotations

from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.settings import LocalSettingsStore

from .openai_compatible import OpenAICompatibleEmbeddingProvider
from .protocol import EmbeddingProvider


def build_embedding_provider(
    config: EmbeddingBackendConfig,
    *,
    settings: LocalSettingsStore,
) -> EmbeddingProvider:
    if not config.model:
        raise ValueError("Embedding model is not configured.")
    if not config.base_url:
        raise ValueError("Embedding base_url is not configured.")
    if (
        config.provider == "openai_compatible"
        and config.model == "text-embedding-v4"
        and config.dimensions == 1024
    ):
        return OpenAICompatibleEmbeddingProvider(
            model=config.model,
            base_url=config.base_url,
            dimensions=config.dimensions,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            batch_size=config.batch_size,
            max_concurrent=config.max_concurrent,
            settings=settings,
        )
    raise ValueError("embedding configuration is outside the V1 contract")
