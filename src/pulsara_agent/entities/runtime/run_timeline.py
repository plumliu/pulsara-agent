"""RunTimeline entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class RunTimelineRecord(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = rt.RUN_TIMELINE

    run_id: str
    turn_id: str
    reply_id: str
    scope: str
    status: str
    item_count: int
    created_at: str
    updated_at: str
    stored_as: NodeRef
    runtime_session_id: str

    def properties(self) -> dict[Any, Any]:
        return {
            rt.SOURCE_SESSION: self.runtime_session_id,
            rt.SOURCE_RUN: self.run_id,
            rt.SOURCE_TURN: self.turn_id,
            rt.SOURCE_REPLY: self.reply_id,
            rt.SCOPE: self.scope,
            rt.STATUS: self.status,
            rt.ITEM_COUNT: self.item_count,
            rt.CREATED_AT: self.created_at,
            rt.UPDATED_AT: self.updated_at,
            rt.STORED_AS: self.stored_as,
        }
