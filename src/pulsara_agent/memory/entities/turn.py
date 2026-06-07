"""Turn entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class Turn(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = memory.CONTEXT
    TYPE: ClassVar[Term] = memory.TURN

    produced: tuple[NodeRef, ...]
    scope: str
    updated_at: str

    def properties(self) -> dict[Any, Any]:
        return {
            memory.PRODUCED: list(self.produced),
            memory.SCOPE: self.scope,
            memory.UPDATED_AT: self.updated_at,
        }
