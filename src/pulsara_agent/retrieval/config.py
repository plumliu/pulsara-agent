"""Sealed configuration for advisory-memory retrieval providers.

The vector space and reranker contract are deliberately not arbitrary runtime
configuration.  A mismatching configuration disables the optional remote
channel; it never reinterprets rows already stored under the V1 contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json


DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_DASHSCOPE_RERANK_BASE_URL = "https://dashscope.aliyuncs.com"


class DenseRecallPurpose(StrEnum):
    AUTOMATIC_ROOT = "AUTOMATIC_ROOT"
    EXPLICIT_SEARCH = "EXPLICIT_SEARCH"
    GOVERNANCE_RELATEDNESS = "GOVERNANCE_RELATEDNESS"


@dataclass(frozen=True, slots=True)
class EmbeddingSemanticContract:
    """The sole V1 vector-space identity.

    Eligibility thresholds intentionally do not participate in this identity:
    changing a coarse query-time floor must never make compatible cached vectors
    look stale or schedule a rebuild.
    """

    contract_id: str = (
        "pulsara.memory.embedding.dashscope-text-embedding-v4-1024.v1"
    )
    contract_version: int = 1
    provider_family: str = "DASHSCOPE_BAILIAN_OPENAI_COMPATIBLE"
    configured_provider: str = "openai_compatible"
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    distance: str = "cosine"
    normalization: str = "provider-native-finite-nonzero"
    retrieval_projection: str = "pulsara.memory-retrieval-text.v1"

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "contract_id": self.contract_id,
                "contract_version": self.contract_version,
                "provider_family": self.provider_family,
                "configured_provider": self.configured_provider,
                "model": self.model,
                "dimensions": self.dimensions,
                "distance": self.distance,
                "normalization": self.normalization,
                "retrieval_projection": self.retrieval_projection,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(encoded).hexdigest()

    def accepts(self, config: "EmbeddingBackendConfig") -> bool:
        return (
            config.provider == self.configured_provider
            and config.model == self.model
            and config.dimensions == self.dimensions
        )


@dataclass(frozen=True, slots=True)
class DenseEligibilityPolicy:
    """Coarse V1 query-time candidate floors, separate from vector identity."""

    policy_id: str = "pulsara.memory-dense-eligibility.coarse-v1"
    automatic_minimum_similarity: float = 0.55
    explicit_minimum_similarity: float = 0.20
    governance_minimum_similarity: float = 0.40

    def minimum_similarity(self, purpose: DenseRecallPurpose) -> float:
        return {
            DenseRecallPurpose.AUTOMATIC_ROOT: self.automatic_minimum_similarity,
            DenseRecallPurpose.EXPLICIT_SEARCH: self.explicit_minimum_similarity,
            DenseRecallPurpose.GOVERNANCE_RELATEDNESS: (
                self.governance_minimum_similarity
            ),
        }[purpose]

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "policy_id": self.policy_id,
                "automatic": self.automatic_minimum_similarity,
                "explicit": self.explicit_minimum_similarity,
                "governance": self.governance_minimum_similarity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(encoded).hexdigest()


MEMORY_EMBEDDING_CONTRACT = EmbeddingSemanticContract()
MEMORY_DENSE_ELIGIBILITY_POLICY = DenseEligibilityPolicy()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "no", "off", "disabled"}


def _embedding_api_key(prefix: str) -> str:
    # Memory retrieval is an independent data-egress boundary.  A model key
    # may name a different provider/trust domain, so it must never be adopted
    # as an embedding credential merely because the dedicated key is absent.
    return os.getenv(f"{prefix}_EMBEDDING_API_KEY", "").strip()


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
class RerankBackendConfig:
    provider: str = "dashscope"
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_DASHSCOPE_RERANK_BASE_URL
    model: str = "qwen3-rerank"
    timeout_seconds: float = 4.0
    max_retries: int = 0
    batch_size: int = 20
    max_concurrent: int = 1

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "RerankBackendConfig":
        return cls(
            provider=os.getenv(f"{prefix}_RERANK_PROVIDER", "dashscope").strip()
            or "dashscope",
            # As with embeddings, reranking has its own explicit egress
            # credential.  Never forward the main model credential here.
            api_key=os.getenv(f"{prefix}_RERANK_API_KEY", "").strip(),
            base_url=(
                os.getenv(
                    f"{prefix}_RERANK_BASE_URL",
                    DEFAULT_DASHSCOPE_RERANK_BASE_URL,
                ).strip()
                or DEFAULT_DASHSCOPE_RERANK_BASE_URL
            ),
            model=os.getenv(f"{prefix}_RERANK_MODEL", "qwen3-rerank").strip()
            or "qwen3-rerank",
            timeout_seconds=_env_float(f"{prefix}_RERANK_TIMEOUT_SECONDS", 4.0),
            max_retries=_env_int(f"{prefix}_RERANK_MAX_RETRIES", 0),
            batch_size=_env_int(f"{prefix}_RERANK_BATCH_SIZE", 20),
            max_concurrent=_env_int(f"{prefix}_RERANK_MAX_CONCURRENT", 1),
        )


@dataclass(frozen=True, slots=True)
class AdvisoryMemoryFeatureConfig:
    automatic_dense: bool = True
    explicit_rerank: bool = True
    cheap_hint_reflection: bool = True
    hint_review_allow_cross_provider: bool = False

    @classmethod
    def from_env(
        cls, prefix: str = "PULSARA"
    ) -> "AdvisoryMemoryFeatureConfig":
        return cls(
            automatic_dense=_env_bool(f"{prefix}_MEMORY_AUTO_DENSE", True),
            explicit_rerank=_env_bool(
                f"{prefix}_MEMORY_EXPLICIT_RERANK", True
            ),
            cheap_hint_reflection=_env_bool(
                f"{prefix}_MEMORY_CHEAP_HINT_REFLECTION", True
            ),
            hint_review_allow_cross_provider=_env_bool(
                f"{prefix}_MEMORY_HINT_REVIEW_ALLOW_CROSS_PROVIDER", False
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    embedding: EmbeddingBackendConfig = EmbeddingBackendConfig()
    rerank: RerankBackendConfig = RerankBackendConfig()
    memory: AdvisoryMemoryFeatureConfig = AdvisoryMemoryFeatureConfig()

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "RetrievalConfig":
        return cls(
            embedding=EmbeddingBackendConfig.from_env(prefix=prefix),
            rerank=RerankBackendConfig.from_env(prefix=prefix),
            memory=AdvisoryMemoryFeatureConfig.from_env(prefix=prefix),
        )


__all__ = [
    "AdvisoryMemoryFeatureConfig",
    "DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL",
    "DEFAULT_DASHSCOPE_RERANK_BASE_URL",
    "DenseEligibilityPolicy",
    "DenseRecallPurpose",
    "EmbeddingSemanticContract",
    "EmbeddingBackendConfig",
    "MEMORY_DENSE_ELIGIBILITY_POLICY",
    "MEMORY_EMBEDDING_CONTRACT",
    "RerankBackendConfig",
    "RetrievalConfig",
]
