"""ToolResult entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class ToolResult(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = memory.CONTEXT
    TYPE: ClassVar[Term] = memory.TOOL_RESULT

    tool_name: str
    status: memory.ToolExecutionStatus
    input_summary: str
    output_summary: str
    truncated: bool
    scope: str
    created_at: str
    stored_as: NodeRef | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            memory.TOOL_NAME: self.tool_name,
            memory.STATUS: self.status,
            memory.INPUT_SUMMARY: self.input_summary,
            memory.OUTPUT_SUMMARY: self.output_summary,
            memory.TRUNCATED: self.truncated,
            memory.SCOPE: self.scope,
            memory.CREATED_AT: self.created_at,
        }
        if self.stored_as is not None:
            values[memory.STORED_AS] = self.stored_as
        return values
