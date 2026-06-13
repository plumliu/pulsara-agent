"""In-memory GraphStore implementation for tests and lightweight demos."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.graph.store import DEFAULT_GRAPH_ID
from pulsara_agent.graph.jsonld_codec import (
    compact_iri,
    expand_id,
    expand_type,
    graph_key as _graph_key,
    normalize_jsonld_document,
)
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(slots=True)
class InMemoryGraphStore:
    graphs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return self.graphs.setdefault(DEFAULT_GRAPH_ID, {})

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        node_id = document.get("@id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        normalized = normalize_jsonld_document(document, CORE_CONTEXT)
        self.graphs.setdefault(_graph_key(graph_id), {})[normalized["@id"]] = normalized

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        graph = self.graphs.get(_graph_key(graph_id), {})
        normalized_id = _normalize_node_id(node_id)
        if normalized_id not in graph:
            raise KeyError(node_id)
        return deepcopy(graph[normalized_id])

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        return _normalize_node_id(node_id) in self.graphs.get(_graph_key(graph_id), {})

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
        graph = self.graphs.get(_graph_key(graph_id), {})
        return [
            deepcopy(doc)
            for doc in graph.values()
            if any(_type_matches(value, type_name) for value in _as_list(doc.get("@type")))
        ]

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("InMemoryGraphStore does not implement SPARQL query")

    def update(self, sparql: str) -> None:
        raise NotImplementedError("InMemoryGraphStore does not implement SPARQL update")

    def delete_graph(self, graph_id: str) -> None:
        self.graphs.pop(_graph_key(graph_id), None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _type_matches(value: Any, type_name: Term) -> bool:
    if isinstance(value, str):
        return expand_type(value, CORE_CONTEXT) == type_name.value
    return False


def _normalize_node_id(node_id: str) -> str:
    return compact_iri(expand_id(node_id, CORE_CONTEXT), CORE_CONTEXT)
