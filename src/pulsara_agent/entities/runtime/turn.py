"""Turn entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class Turn(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = rt.TURN

    produced: tuple[NodeRef, ...]
    scope: str
    updated_at: str

    def properties(self) -> dict[Any, Any]:
        return {
            rt.PRODUCED: list(self.produced),
            rt.SCOPE: self.scope,
            rt.UPDATED_AT: self.updated_at,
        }
