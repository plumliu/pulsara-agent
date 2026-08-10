"""Configuration for the conversation-kernel embedding provider."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)


def _embedding_api_key(prefix: str) -> str:
    return (
        os.getenv(f"{prefix}_EMBEDDING_API_KEY", "").strip()
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
    api_key: str = field(default="", repr=False)
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
            provider=os.getenv(
                f"{prefix}_EMBEDDING_PROVIDER", "openai_compatible"
            ).strip()
            or "openai_compatible",
            api_key=_embedding_api_key(prefix),
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
class RetrievalConfig:
    embedding: EmbeddingBackendConfig = EmbeddingBackendConfig()

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "RetrievalConfig":
        return cls(
            embedding=EmbeddingBackendConfig.from_env(prefix=prefix),
        )
