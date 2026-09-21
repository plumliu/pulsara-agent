"""Sealed configuration for advisory-memory retrieval providers.

The vector space and reranker contract are deliberately not arbitrary runtime
configuration.  A mismatching configuration disables the optional remote
channel; it never reinterprets rows already stored under the V1 contract.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    RELATION_CANDIDATES = "RELATION_CANDIDATES"


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
    relation_candidate_minimum_similarity: float = 0.40

    def minimum_similarity(self, purpose: DenseRecallPurpose) -> float:
        return {
            DenseRecallPurpose.AUTOMATIC_ROOT: self.automatic_minimum_similarity,
            DenseRecallPurpose.EXPLICIT_SEARCH: self.explicit_minimum_similarity,
            DenseRecallPurpose.RELATION_CANDIDATES: (
                self.relation_candidate_minimum_similarity
            ),
        }[purpose]

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "policy_id": self.policy_id,
                "automatic": self.automatic_minimum_similarity,
                "explicit": self.explicit_minimum_similarity,
                "relation_candidates": self.relation_candidate_minimum_similarity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(encoded).hexdigest()


MEMORY_EMBEDDING_CONTRACT = EmbeddingSemanticContract()
MEMORY_DENSE_ELIGIBILITY_POLICY = DenseEligibilityPolicy()


@dataclass(frozen=True, slots=True)
class EmbeddingBackendConfig:
    provider: str = "openai_compatible"
    base_url: str = DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    timeout_seconds: float = 30.0
    max_retries: int = 3
    batch_size: int = 10
    max_concurrent: int = 5

@dataclass(frozen=True, slots=True)
class RerankBackendConfig:
    provider: str = "dashscope"
    base_url: str = DEFAULT_DASHSCOPE_RERANK_BASE_URL
    model: str = "qwen3-rerank"
    timeout_seconds: float = 4.0
    max_retries: int = 0
    batch_size: int = 20
    max_concurrent: int = 1

@dataclass(frozen=True, slots=True)
class AdvisoryMemoryFeatureConfig:
    automatic_dense: bool = True
    explicit_rerank: bool = True


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    embedding: EmbeddingBackendConfig = EmbeddingBackendConfig()
    rerank: RerankBackendConfig = RerankBackendConfig()
    memory: AdvisoryMemoryFeatureConfig = AdvisoryMemoryFeatureConfig()



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
