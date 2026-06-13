"""Plugin capability entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class Plugin(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = cap.PLUGIN

    version: str
    provides_tool: tuple[NodeRef, ...] = ()
    provides_skill: tuple[NodeRef, ...] = ()
    source_data_uri: str | None = None

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {cap.VERSION: self.version}
        if self.provides_tool:
            values[cap.PROVIDES_TOOL] = list(self.provides_tool)
        if self.provides_skill:
            values[cap.PROVIDES_SKILL] = list(self.provides_skill)
        if self.source_data_uri is not None:
            values[cap.SOURCE_DATA_URI] = self.source_data_uri
        return values
