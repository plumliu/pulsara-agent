"""Configuration for embedding and rerank providers.

This package intentionally stays separate from ``llm/``:

- Pulsara already has a full LLM runtime stack under ``llm/``.
- Embedding + rerank are retrieval-side model services with different
  request/latency/cost semantics.
- The retrieval stack will be consumed first by memory recall, but should not
  be hard-coded under ``memory/`` because governance relatedness and future
  search surfaces may reuse it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_RERANK_BASE_URL = "https://dashscope.aliyuncs.com"


def _fallback_api_key(prefix: str, specific: str) -> str:
    return (
        os.getenv(f"{prefix}_{specific}", "").strip()
        or os.getenv(f"{prefix}_DASHSCOPE_API_KEY", "").strip()
        or os.getenv(f"{prefix}_API_KEY", "").strip()
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True, slots=True)
class EmbeddingBackendConfig:
    provider: str = "openai_compatible"
    api_key: str = ""
    base_url: str = DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    timeout_seconds: float = 30.0
    max_retries: int = 3
    batch_size: int = 10
    max_concurrent: int = 5

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "EmbeddingBackendConfig":
        return cls(
            provider=os.getenv(f"{prefix}_EMBEDDING_PROVIDER", "openai_compatible").strip()
            or "openai_compatible",
            api_key=_fallback_api_key(prefix, "EMBEDDING_API_KEY"),
            base_url=(
                os.getenv(
                    f"{prefix}_EMBEDDING_BASE_URL",
                    DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
                ).strip()
                or DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL
            ),
            model=os.getenv(f"{prefix}_EMBEDDING_MODEL", "text-embedding-v4").strip()
            or "text-embedding-v4",
            dimensions=_env_int(f"{prefix}_EMBEDDING_DIMENSIONS", 1024),
            timeout_seconds=_env_float(f"{prefix}_EMBEDDING_TIMEOUT_SECONDS", 30.0),
            max_retries=_env_int(f"{prefix}_EMBEDDING_MAX_RETRIES", 3),
            batch_size=_env_int(f"{prefix}_EMBEDDING_BATCH_SIZE", 10),
            max_concurrent=_env_int(f"{prefix}_EMBEDDING_MAX_CONCURRENT", 5),
        )


@dataclass(frozen=True, slots=True)
class RerankBackendConfig:
    provider: str = "dashscope"
    api_key: str = ""
    base_url: str = DEFAULT_DASHSCOPE_RERANK_BASE_URL
    model: str = "qwen3-rerank"
    timeout_seconds: float = 30.0
    max_retries: int = 3
    batch_size: int = 50
    max_concurrent: int = 4

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "RerankBackendConfig":
        return cls(
            provider=os.getenv(f"{prefix}_RERANK_PROVIDER", "dashscope").strip()
            or "dashscope",
            api_key=_fallback_api_key(prefix, "RERANK_API_KEY"),
            base_url=(
                os.getenv(
                    f"{prefix}_RERANK_BASE_URL",
                    DEFAULT_DASHSCOPE_RERANK_BASE_URL,
                ).strip()
                or DEFAULT_DASHSCOPE_RERANK_BASE_URL
            ),
            model=os.getenv(f"{prefix}_RERANK_MODEL", "qwen3-rerank").strip()
            or "qwen3-rerank",
            timeout_seconds=_env_float(f"{prefix}_RERANK_TIMEOUT_SECONDS", 30.0),
            max_retries=_env_int(f"{prefix}_RERANK_MAX_RETRIES", 3),
            batch_size=_env_int(f"{prefix}_RERANK_BATCH_SIZE", 50),
            max_concurrent=_env_int(f"{prefix}_RERANK_MAX_CONCURRENT", 4),
        )


@dataclass(frozen=True, slots=True)
class TokenizerBackendConfig:
    provider: str = "jieba_search"
    min_token_length: int = 2
    lowercase: bool = True

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "TokenizerBackendConfig":
        return cls(
            provider=os.getenv(f"{prefix}_TOKENIZER_PROVIDER", "jieba_search").strip()
            or "jieba_search",
            min_token_length=_env_int(f"{prefix}_TOKENIZER_MIN_TOKEN_LENGTH", 2),
            lowercase=(
                os.getenv(f"{prefix}_TOKENIZER_LOWERCASE", "true").strip().lower()
                not in {"0", "false", "no", "off", "disabled"}
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    embedding: EmbeddingBackendConfig = EmbeddingBackendConfig()
    rerank: RerankBackendConfig = RerankBackendConfig()
    tokenizer: TokenizerBackendConfig = TokenizerBackendConfig()

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "RetrievalConfig":
        return cls(
            embedding=EmbeddingBackendConfig.from_env(prefix=prefix),
            rerank=RerankBackendConfig.from_env(prefix=prefix),
            tokenizer=TokenizerBackendConfig.from_env(prefix=prefix),
        )
