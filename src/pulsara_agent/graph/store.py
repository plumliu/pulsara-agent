"""GraphStore boundary for semantic JSON-LD/RDF persistence."""

from __future__ import annotations

from typing import Any, Protocol

from pulsara_agent.jsonld import Term


DEFAULT_GRAPH_ID = "graph:default"


class GraphStore(Protocol):
    """RDF-oriented graph store boundary.

    JSON-LD document methods are ingestion/convenience APIs. SPARQL query and
    update are the long-term semantic interface for Oxigraph-backed stores.
    Methods with ``graph_id=None`` operate on ``DEFAULT_GRAPH_ID``. Cross-graph
    reads must be expressed explicitly by a backend-specific graph/query name.
    """

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None: ...

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]: ...

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool: ...

    def find_by_type(self, type_name: Term, graph_id: str | None = None) -> list[dict[str, Any]]: ...

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def update(self, sparql: str) -> None: ...

    def delete_graph(self, graph_id: str) -> None: ...
