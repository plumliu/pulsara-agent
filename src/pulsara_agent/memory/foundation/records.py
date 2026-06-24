"""Small immutable records returned by memory runtime operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulsara_agent.jsonld import jsonld_value
from pulsara_agent.memory.foundation.provenance import RuntimeEventSpan
from pulsara_agent.ontology import memory, runtime as rt


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    id: str
    digest: str
    stored_at: str
    size_bytes: int

    @property
    def artifact_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "digest": self.digest,
            "stored_at": self.stored_at,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    media_type: str
    digest: str
    size_bytes: int
    stored_at: str
    created_at: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ArtifactTextSlice:
    artifact: ArtifactRecord
    text: str
    offset_chars: int
    returned_chars: int
    total_chars: int | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ToolResultRecord:
    tool_result_id: str
    artifact_id: str | None
    output_summary: str
    status: rt.ToolExecutionStatus
    event_span: RuntimeEventSpan | None = None
    artifact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_result_id": self.tool_result_id,
            "artifact_id": self.artifact_id,
            "artifact_ids": list(self.artifact_ids),
            "output_summary": self.output_summary,
            "status": self.status,
        }
        if self.event_span is not None:
            payload["event_span"] = self.event_span.to_jsonld()
        return jsonld_value(payload)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    statement: str
    source_id: str
    status: memory.NodeStatus = memory.NodeStatus.ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return jsonld_value(
            {
                "evidence_id": self.evidence_id,
                "statement": self.statement,
                "source_id": self.source_id,
                "status": self.status,
            }
        )


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    statement: str
    status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str

    def to_dict(self) -> dict[str, Any]:
        return jsonld_value(
            {
                "claim_id": self.claim_id,
                "statement": self.statement,
                "status": self.status,
                "confidence_level": self.confidence_level,
                "verification_status": self.verification_status,
                "gate_reason": self.gate_reason,
            }
        )


@dataclass(frozen=True, slots=True)
class MemoryWriteRecord:
    memory_id: str
    statement: str
    status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str

    def to_dict(self) -> dict[str, Any]:
        return jsonld_value(
            {
                "memory_id": self.memory_id,
                "statement": self.statement,
                "status": self.status,
                "confidence_level": self.confidence_level,
                "verification_status": self.verification_status,
                "gate_reason": self.gate_reason,
            }
        )
