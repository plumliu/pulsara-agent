"""Evidence entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class Evidence(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = rt.EVIDENCE

    statement: str
    source_type: rt.EvidenceSourceType
    status: memory.NodeStatus
    observed_at: str
    scope: str
    created_from: NodeRef

    def properties(self) -> dict[Any, Any]:
        return {
            rt.STATEMENT: self.statement,
            rt.SOURCE_TYPE: self.source_type,
            rt.STATUS: self.status,
            rt.OBSERVED_AT: self.observed_at,
            rt.SCOPE: self.scope,
            rt.CREATED_FROM: self.created_from,
        }
