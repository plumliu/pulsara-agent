"""Artifact entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, Term
from pulsara_agent.memory.provenance import RuntimeEventSpan
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class Artifact(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = memory.CONTEXT
    TYPE: ClassVar[Term] = memory.ARTIFACT

    stored_at: str
    digest: str
    summary: str
    created_at: str
    scope: str
    event_span: RuntimeEventSpan | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            memory.STORED_AT: self.stored_at,
            memory.HASH: self.digest,
            memory.SUMMARY: self.summary,
            memory.CREATED_AT: self.created_at,
            memory.SCOPE: self.scope,
        }
        if self.event_span is not None:
            values[memory.EVENT_SPAN] = self.event_span.to_jsonld()
        return values
