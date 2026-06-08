"""Storage boundary protocols for runtime and semantic memory."""

from __future__ import annotations

from typing import Any, Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.memory.records import ArtifactWriteResult


DEFAULT_GRAPH_ID = "graph:default"


class GraphStore(Protocol):
    """RDF-oriented graph store boundary.

    JSON-LD document methods are ingestion/convenience APIs. SPARQL query and
    update are the long-term semantic interface for Oxigraph-backed stores.
    """

    def put_jsonld(self, document: dict[str, Any], graph_id: str = DEFAULT_GRAPH_ID) -> None: ...

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]: ...

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def update(self, sparql: str) -> None: ...

    def delete_graph(self, graph_id: str) -> None: ...


class ArtifactStore(Protocol):
    """Runtime artifact persistence boundary."""

    def put_text(self, blob_id: str, content: str) -> ArtifactWriteResult: ...

    def get_text(self, blob_id: str) -> str: ...


class RuntimeEventReadStore(Protocol):
    """Read-only runtime event access needed by memory ingestion."""

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
    ) -> list[AgentEvent]: ...

    def replay(self, reply_id: str) -> Any: ...
