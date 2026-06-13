"""ToolResult entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT

if TYPE_CHECKING:
    from pulsara_agent.memory.provenance import RuntimeEventSpan


@dataclass(frozen=True, slots=True)
class ToolResult(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = rt.TOOL_RESULT

    tool_name: str
    status: rt.ToolExecutionStatus
    input_summary: str
    output_summary: str
    truncated: bool
    scope: str
    created_at: str
    stored_as: NodeRef | None = None
    event_span: RuntimeEventSpan | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            rt.TOOL_NAME: self.tool_name,
            rt.STATUS: self.status,
            rt.INPUT_SUMMARY: self.input_summary,
            rt.OUTPUT_SUMMARY: self.output_summary,
            rt.TRUNCATED: self.truncated,
            rt.SCOPE: self.scope,
            rt.CREATED_AT: self.created_at,
        }
        if self.stored_as is not None:
            values[rt.STORED_AS] = self.stored_as
        if self.event_span is not None:
            values[rt.EVENT_SPAN_PROPERTY] = self.event_span.to_jsonld()
        return values
