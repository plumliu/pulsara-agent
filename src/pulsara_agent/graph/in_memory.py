"""In-memory GraphStore implementation for tests and lightweight demos."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.graph.store import DEFAULT_GRAPH_ID
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
        self.graphs.setdefault(_graph_key(graph_id), {})[node_id] = deepcopy(document)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        graph = self.graphs.get(_graph_key(graph_id), {})
        if node_id not in graph:
            raise KeyError(node_id)
        return deepcopy(graph[node_id])

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        return node_id in self.graphs.get(_graph_key(graph_id), {})

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


def _graph_key(graph_id: str | None) -> str:
    if graph_id is None:
        return DEFAULT_GRAPH_ID
    if not graph_id:
        raise ValueError("graph_id must be a non-empty string or None")
    return graph_id


def _type_matches(value: Any, type_name: Term) -> bool:
    if value == type_name.name or value == type_name.value:
        return True
    if isinstance(value, str):
        return _expand_compact_iri(value) == type_name.value
    return False


def _expand_compact_iri(value: str) -> str:
    if "://" in value or value.startswith("urn:"):
        return value
    prefix, sep, suffix = value.partition(":")
    if sep:
        base = CORE_CONTEXT.get(prefix)
        if isinstance(base, str):
            return base + suffix
    mapped = CORE_CONTEXT.get(value)
    if isinstance(mapped, str):
        return mapped
    return value
