"""Tool capability entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, Term
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class Tool(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = cap.TOOL

    version: str
    has_input_schema: str | None = None
    has_output_schema: str | None = None
    allowed_in_scope: str | None = None
    blocked_in_scope: str | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {cap.VERSION: self.version}
        if self.has_input_schema is not None:
            values[cap.HAS_INPUT_SCHEMA] = self.has_input_schema
        if self.has_output_schema is not None:
            values[cap.HAS_OUTPUT_SCHEMA] = self.has_output_schema
        if self.allowed_in_scope is not None:
            values[cap.ALLOWED_IN_SCOPE] = self.allowed_in_scope
        if self.blocked_in_scope is not None:
            values[cap.BLOCKED_IN_SCOPE] = self.blocked_in_scope
        return values
