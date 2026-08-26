"""Factory for the one sealed Round 8 rerank family."""

from __future__ import annotations

from pulsara_agent.retrieval.config import RerankBackendConfig
from pulsara_agent.process_api_key_boundary import ProcessApiKeyBoundary

from .dashscope import DashScopeRerankProvider
from .protocol import RerankProvider


def build_rerank_provider(
    config: RerankBackendConfig,
    *,
    api_key_boundary: ProcessApiKeyBoundary,
) -> RerankProvider:
    if config.provider != "dashscope":
        raise ValueError("rerank provider is outside the V1 contract")
    if config.model != "qwen3-rerank":
        raise ValueError("rerank model is outside the V1 contract")
    if not config.api_key or not config.base_url:
        raise ValueError("rerank provider is not configured")
    return DashScopeRerankProvider(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        maximum_concurrent=config.max_concurrent,
        api_key_boundary=api_key_boundary,
    )


__all__ = ["build_rerank_provider"]
