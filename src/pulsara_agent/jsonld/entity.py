"""Base class for typed JSON-LD entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld.term import Term
from pulsara_agent.jsonld.value import jsonld_value


@dataclass(frozen=True, slots=True)
class JsonLdEntity:
    """A typed node that can be serialized as JSON-LD."""

    id: str

    CONTEXT: ClassVar[dict[str, Any]]
    TYPE: ClassVar[Term]

    def properties(self) -> dict[Any, Any]:
        return {}

    def to_jsonld(self) -> dict[str, Any]:
        return jsonld_value(
            {
                "@context": self.CONTEXT,
                "@id": self.id,
                "@type": [self.TYPE],
                **self.properties(),
            }
        )
