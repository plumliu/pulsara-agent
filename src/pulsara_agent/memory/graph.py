"""GraphStore abstractions.

The MVP keeps an in-memory JSON-LD document store. RDF expansion/SPARQL will be
added behind this boundary without changing callers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.jsonld import Term
from pulsara_agent.memory.protocols import DEFAULT_GRAPH_ID


@dataclass(slots=True)
class InMemoryGraphStore:
    graphs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return self.graphs.setdefault(DEFAULT_GRAPH_ID, {})

    def put_jsonld(self, document: dict[str, Any], graph_id: str = DEFAULT_GRAPH_ID) -> None:
        node_id = document.get("@id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        self.graphs.setdefault(graph_id, {})[node_id] = deepcopy(document)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        graph_key = graph_id or DEFAULT_GRAPH_ID
        graph = self.graphs.get(graph_key, {})
        if node_id not in graph:
            raise KeyError(node_id)
        return deepcopy(graph[node_id])

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        graph_key = graph_id or DEFAULT_GRAPH_ID
        return node_id in self.graphs.get(graph_key, {})

    def add_relation(
        self,
        source_id: str,
        relation: Term,
        target_id: str,
        graph_id: str = DEFAULT_GRAPH_ID,
    ) -> None:
        document = self.get_jsonld(source_id, graph_id=graph_id)
        values = _as_list(document.get(relation.name))
        target = {"@id": target_id}
        if target not in values:
            values.append(target)
        document[relation.name] = values
        self.put_jsonld(document, graph_id=graph_id)

    def find_by_type(self, type_name: Term, graph_id: str | None = None) -> list[dict[str, Any]]:
        graphs = [self.graphs.get(graph_id, {})] if graph_id is not None else list(self.graphs.values())
        return [
            doc
            for graph in graphs
            for doc in graph.values()
            if type_name.name in _as_list(doc.get("@type"))
        ]

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("InMemoryGraphStore does not implement SPARQL query")

    def update(self, sparql: str) -> None:
        raise NotImplementedError("InMemoryGraphStore does not implement SPARQL update")

    def delete_graph(self, graph_id: str) -> None:
        self.graphs.pop(graph_id, None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
