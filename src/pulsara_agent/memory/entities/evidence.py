"""Evidence entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class Evidence(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = memory.CONTEXT
    TYPE: ClassVar[Term] = memory.EVIDENCE

    statement: str
    source_type: memory.EvidenceSourceType
    status: memory.NodeStatus
    observed_at: str
    scope: str
    created_from: NodeRef

    def properties(self) -> dict[Any, Any]:
        return {
            memory.STATEMENT: self.statement,
            memory.SOURCE_TYPE: self.source_type,
            memory.STATUS: self.status,
            memory.OBSERVED_AT: self.observed_at,
            memory.SCOPE: self.scope,
            memory.CREATED_FROM: self.created_from,
        }
