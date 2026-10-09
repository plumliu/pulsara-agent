"""Single-slot retrieval connections and physical retrieval boundaries."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
from urllib.parse import urlsplit, urlunsplit

SLOT_SHAPES = {
    "embedding": frozenset({"openai_embedding"}),
    "rerank": frozenset({"flat_rerank", "nested_rerank"}),
    "decision": frozenset({"system_one", "openai_decisions"}),
}


@dataclass(frozen=True, slots=True)
class RetrievalConnection:
    endpoint: str
    model_id: str
    shape: str
    authentication: str = "bearer_api_key"
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or not parts.path.strip("/")
            or self.endpoint != self.endpoint.strip()
        ):
            raise ValueError("请填写完整请求地址，包含 API 路径。")
        if not self.model_id or self.model_id != self.model_id.strip():
            raise ValueError("请填写模型 ID。")
        if self.shape not in set().union(*SLOT_SHAPES.values()):
            raise ValueError("请求格式不受支持。")
        if self.authentication not in {"bearer_api_key", "none"}:
            raise ValueError("认证方式不受支持。")
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not self.api_key
        ):
            raise ValueError("API key 不能为空。")
        if self.authentication == "none" and self.api_key is not None:
            raise ValueError("无需认证的连接不能保存 API key。")
        if self.shape == "openai_decisions" and (
            not parts.path.endswith("/decisions") or parts.query
        ):
            raise ValueError("OpenAI Decisions 请求地址必须以 /decisions 结尾。")

    @property
    def complete(self):
        return self.authentication == "none" or self.api_key is not None

    @property
    def embedding_contract(self):
        if self.shape != "openai_embedding":
            raise ValueError("not an embedding connection")
        parts = urlsplit(self.endpoint)
        endpoint = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")
        )
        return EmbeddingSemanticContract(endpoint, self.model_id, self.shape)

    def public(self):
        return {
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "shape": self.shape,
            "authentication": self.authentication,
            "credential_configured": self.api_key is not None,
        }


def connection_from_dict(slot: str, value: object, *, private: bool = False):
    if slot not in SLOT_SHAPES:
        raise ValueError("未知记忆模型类型。")
    fields = {"endpoint", "model_id", "shape", "authentication"}
    if private:
        fields.add("api_key")
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("模型配置字段不完整或包含未知字段。")
    if any(not isinstance(value[name], str) for name in fields - {"api_key"}):
        raise ValueError("模型配置字段必须为文字。")
    connection = RetrievalConnection(**value)
    if connection.shape not in SLOT_SHAPES[slot]:
        raise ValueError("该模型类型不支持所选请求格式。")
    return connection


@dataclass(frozen=True, slots=True)
class MemoryRetrievalSettings:
    embedding: RetrievalConnection | None = None
    embedding_enabled: bool = False
    rerank: RetrievalConnection | None = None
    decision: RetrievalConnection | None = None
    ranking_mode: str = "off"

    def __post_init__(self):
        if (
            type(self.embedding_enabled) is not bool
            or not isinstance(self.ranking_mode, str)
            or self.ranking_mode not in {"off", "decision", "rerank"}
        ):
            raise ValueError("记忆检索开关值无效。")
        for slot, shapes in SLOT_SHAPES.items():
            connection = getattr(self, slot)
            if connection is not None and connection.shape not in shapes:
                raise ValueError("模型类型与请求格式不符。")
        if self.embedding_enabled and (
            self.embedding is None or not self.embedding.complete
        ):
            raise ValueError("请先配置 Embedding 模型及 API key。")
        if self.ranking_mode != "off":
            connection = getattr(self, self.ranking_mode)
            if connection is None or not connection.complete:
                raise ValueError("请先配置所选重排模型及 API key。")

    def public(self):
        return {
            **{
                slot: None
                if getattr(self, slot) is None
                else getattr(self, slot).public()
                for slot in SLOT_SHAPES
            },
            "embedding_enabled": self.embedding_enabled,
            "ranking_mode": self.ranking_mode,
        }

    def private(self):
        return {
            **{
                slot: None
                if getattr(self, slot) is None
                else {
                    name: getattr(getattr(self, slot), name)
                    for name in (
                        "endpoint",
                        "model_id",
                        "shape",
                        "authentication",
                        "api_key",
                    )
                }
                for slot in SLOT_SHAPES
            },
            "embedding_enabled": self.embedding_enabled,
            "ranking_mode": self.ranking_mode,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
            *SLOT_SHAPES,
            "embedding_enabled",
            "ranking_mode",
        }:
            raise ValueError("memory retrieval settings have an invalid closed shape")
        return cls(
            **{
                **value,
                **{
                    slot: None
                    if value[slot] is None
                    else connection_from_dict(slot, value[slot], private=True)
                    for slot in SLOT_SHAPES
                },
            }
        )

    def replacing(self, slot, connection, *, activate=False):
        if slot not in SLOT_SHAPES or type(activate) is not bool:
            raise ValueError("记忆模型提交无效。")
        changes = {slot: connection}
        if slot == "embedding" and (activate or connection is None):
            changes["embedding_enabled"] = connection is not None
        elif slot != "embedding" and (
            activate or (connection is None and self.ranking_mode == slot)
        ):
            changes["ranking_mode"] = slot if connection is not None else "off"
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class EmbeddingSemanticContract:
    endpoint: str
    model: str
    shape: str = "openai_embedding"
    dimensions: int = 1024
    contract_version: int = 1

    @property
    def contract_id(self):
        encoded = json.dumps(
            (
                self.endpoint,
                self.shape,
                self.model,
                self.dimensions,
                "cosine",
                "pulsara.memory-retrieval-text.v1",
            ),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return "pulsara.memory.embedding:" + sha256(encoded).hexdigest()


class DenseRecallPurpose(StrEnum):
    AUTOMATIC_ROOT = "AUTOMATIC_ROOT"
    EXPLICIT_SEARCH = "EXPLICIT_SEARCH"
    RELATION_CANDIDATES = "RELATION_CANDIDATES"


@dataclass(frozen=True, slots=True)
class DenseEligibilityPolicy:
    policy_id: str = "pulsara.memory-dense-eligibility.coarse-v1"
    automatic_minimum_similarity: float = 0.55
    explicit_minimum_similarity: float = 0.20
    relation_candidate_minimum_similarity: float = 0.40

    def minimum_similarity(self, purpose):
        return {
            DenseRecallPurpose.AUTOMATIC_ROOT: self.automatic_minimum_similarity,
            DenseRecallPurpose.EXPLICIT_SEARCH: self.explicit_minimum_similarity,
            DenseRecallPurpose.RELATION_CANDIDATES: self.relation_candidate_minimum_similarity,
        }[purpose]


MEMORY_DENSE_ELIGIBILITY_POLICY = DenseEligibilityPolicy()


@dataclass(frozen=True, slots=True)
class EmbeddingBackendConfig:
    dimensions: int = 1024
    timeout_seconds: float = 30.0
    batch_size: int = 10
    max_concurrent: int = 5


@dataclass(frozen=True, slots=True)
class RerankBackendConfig:
    timeout_seconds: float = 4.0
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
