"""Artifact entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, Term
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT

if TYPE_CHECKING:
    from pulsara_agent.memory.provenance import RuntimeEventSpan


@dataclass(frozen=True, slots=True)
class Artifact(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = rt.ARTIFACT

    stored_at: str
    digest: str
    summary: str
    created_at: str
    scope: str
    event_span: RuntimeEventSpan | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            rt.STORED_AT: self.stored_at,
            rt.HASH: self.digest,
            rt.SUMMARY: self.summary,
            rt.CREATED_AT: self.created_at,
            rt.SCOPE: self.scope,
        }
        if self.event_span is not None:
            values[rt.EVENT_SPAN_PROPERTY] = self.event_span.to_jsonld()
        return values
