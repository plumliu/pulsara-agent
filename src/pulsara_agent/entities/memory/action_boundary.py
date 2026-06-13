"""ActionBoundary entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class ActionBoundary(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = memory.ACTION_BOUNDARY

    statement: str
    scope: str
    status: memory.NodeStatus
    applies_when: str
    do_not_apply_when: str
    source_authority: memory.SourceAuthority
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    created_at: str
    updated_at: str
    gate_reason: str
    evidence: tuple[NodeRef, ...] = ()

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            memory.STATEMENT: self.statement,
            memory.SCOPE: self.scope,
            memory.STATUS: self.status,
            memory.APPLIES_WHEN: self.applies_when,
            memory.DO_NOT_APPLY_WHEN: self.do_not_apply_when,
            memory.SOURCE_AUTHORITY: self.source_authority,
            memory.CONFIDENCE_LEVEL: self.confidence_level,
            memory.VERIFICATION_STATUS: self.verification_status,
            memory.CREATED_AT: self.created_at,
            memory.UPDATED_AT: self.updated_at,
            memory.GATE_REASON: self.gate_reason,
        }
        if self.evidence:
            values[memory.HAS_EVIDENCE] = list(self.evidence)
        return values
