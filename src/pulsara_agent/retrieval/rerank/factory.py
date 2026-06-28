"""Factories for rerank providers."""

from __future__ import annotations

from pulsara_agent.retrieval.config import RerankBackendConfig

from .dashscope import DashScopeRerankProvider
from .protocol import RerankProvider


def build_rerank_provider(config: RerankBackendConfig) -> RerankProvider:
    if not config.model:
        raise ValueError("Rerank model is not configured.")
    if not config.api_key:
        raise ValueError("Rerank api_key is not configured.")
    if not config.base_url:
        raise ValueError("Rerank base_url is not configured.")
    if config.provider == "dashscope":
        return DashScopeRerankProvider(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            batch_size=config.batch_size,
            max_concurrent=config.max_concurrent,
        )
    raise ValueError(f"Unknown rerank provider: {config.provider!r}")
