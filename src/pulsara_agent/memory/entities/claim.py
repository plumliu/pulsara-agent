"""Claim entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class Claim(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = memory.CONTEXT
    TYPE: ClassVar[Term] = memory.CLAIM

    statement: str
    scope: str
    status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    source_authority: memory.SourceAuthority
    created_at: str
    updated_at: str
    gate_reason: str
    evidence: tuple[NodeRef, ...] = ()

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            memory.STATEMENT: self.statement,
            memory.SCOPE: self.scope,
            memory.STATUS: self.status,
            memory.CONFIDENCE_LEVEL: self.confidence_level,
            memory.VERIFICATION_STATUS: self.verification_status,
            memory.SOURCE_AUTHORITY: self.source_authority,
            memory.CREATED_AT: self.created_at,
            memory.UPDATED_AT: self.updated_at,
            memory.GATE_REASON: self.gate_reason,
        }
        if self.evidence:
            values[memory.HAS_EVIDENCE] = list(self.evidence)
        return values
