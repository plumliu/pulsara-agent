"""Factory for the one sealed Round 8 rerank family."""

from __future__ import annotations

from pulsara_agent.retrieval.config import RerankBackendConfig
from pulsara_agent.settings import LocalSettingsStore

from .dashscope import DashScopeRerankProvider
from .protocol import RerankProvider


def build_rerank_provider(
    config: RerankBackendConfig,
    *,
    settings: LocalSettingsStore,
) -> RerankProvider:
    if config.provider != "dashscope":
        raise ValueError("rerank provider is outside the V1 contract")
    if config.model != "qwen3-rerank":
        raise ValueError("rerank model is outside the V1 contract")
    if not config.base_url:
        raise ValueError("rerank provider is not configured")
    return DashScopeRerankProvider(
        model=config.model,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        maximum_concurrent=config.max_concurrent,
        settings=settings,
    )


__all__ = ["build_rerank_provider"]
