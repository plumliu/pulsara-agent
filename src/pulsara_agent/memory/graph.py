"""GraphStore abstractions.

The MVP keeps an in-memory JSON-LD document store. RDF expansion/SPARQL will be
added behind this boundary without changing callers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.jsonld import Term


@dataclass(slots=True)
class InMemoryGraphStore:
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put_jsonld(self, document: dict[str, Any]) -> None:
        node_id = document.get("@id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        self.documents[node_id] = deepcopy(document)

    def get_jsonld(self, node_id: str) -> dict[str, Any]:
        return deepcopy(self.documents[node_id])

    def has_jsonld(self, node_id: str) -> bool:
        return node_id in self.documents

    def add_relation(self, source_id: str, relation: Term, target_id: str) -> None:
        document = self.get_jsonld(source_id)
        values = _as_list(document.get(relation.name))
        target = {"@id": target_id}
        if target not in values:
            values.append(target)
        document[relation.name] = values
        self.put_jsonld(document)

    def find_by_type(self, type_name: Term) -> list[dict[str, Any]]:
        return [
            doc
            for doc in self.documents.values()
            if type_name.name in _as_list(doc.get("@type"))
        ]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
