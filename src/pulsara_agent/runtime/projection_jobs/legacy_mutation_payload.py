"""Read-only decoder for pre-v6 ``memory_write_outbox`` payloads.

This module is migration authority only. It intentionally exposes no writer or
surface-settlement operation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class LegacyCanonicalMutationLane(StrEnum):
    GOVERNED_MEMORY = "governed_memory"
    RUNTIME_SEMANTIC = "runtime_semantic"
    GRAPH_RESET = "graph_reset"


class LegacyCanonicalMutationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    document: dict[str, Any]


class LegacyCanonicalMutationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "canonical_mutation"
    mutation_id: str | None = None
    mutation_lane: LegacyCanonicalMutationLane
    decision_record: dict[str, Any] | None = None
    dirty_memory_ids: tuple[str, ...] = ()
    documents: tuple[LegacyCanonicalMutationDocument, ...] = ()
    surface_apply_status: dict[str, str] = Field(default_factory=dict)
    source_runtime_session_id: str | None = None
    source_run_id: str | None = None
    source_turn_id: str | None = None
    source_reply_id: str | None = None
    source_artifact_ids: tuple[str, ...] = ()
    graph_reset: bool = False


_LEGACY_PAYLOAD_ADAPTER = TypeAdapter(LegacyCanonicalMutationPayload)


def parse_legacy_mutation_payload(
    value: Any,
) -> LegacyCanonicalMutationPayload:
    return _LEGACY_PAYLOAD_ADAPTER.validate_python(value)


__all__ = [
    "LegacyCanonicalMutationDocument",
    "LegacyCanonicalMutationLane",
    "LegacyCanonicalMutationPayload",
    "parse_legacy_mutation_payload",
]
