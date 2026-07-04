"""Policy capability entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, Term
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class Policy(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = cap.POLICY

    allowed_in_scope: str | None = None
    blocked_in_scope: str | None = None
    source_data_uri: str | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        if self.allowed_in_scope is not None:
            values[cap.ALLOWED_IN_SCOPE] = self.allowed_in_scope
        if self.blocked_in_scope is not None:
            values[cap.BLOCKED_IN_SCOPE] = self.blocked_in_scope
        if self.source_data_uri is not None:
            values[cap.SOURCE_DATA_URI] = self.source_data_uri
        return values
