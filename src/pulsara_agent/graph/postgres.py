"""PostgreSQL-backed GraphStore implementation.

This store keeps the generic GraphStore truth in ``graph_documents`` and
maintains typed canonical-memory projections in ``memory_nodes`` and
``memory_relations`` during the same write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.graph.jsonld_codec import normalize_jsonld_document
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.storage.postgres_memory_projection import refresh_document_projection
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)

if TYPE_CHECKING:
    from pulsara_agent.projection_jobs.contracts import (
        CanonicalGraphNodeReadViewFact,
    )


@dataclass(slots=True)
class PostgresGraphStore:
    """GraphStore backed by Postgres JSONB plus canonical memory projections."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None
    default_context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.default_context is None:
            self.default_context = CORE_CONTEXT
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError(
                "PostgresGraphStore requires exactly one verified provider or transaction connection"
            )

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        from pulsara_agent.graph.projection_relations import (
            reject_owned_relation_predicates,
        )

        reject_owned_relation_predicates(document)
        normalized = normalize_jsonld_document(document, self.default_context)
        node_id = normalized["@id"]
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        graph = _graph_key(graph_id)
        first_type = _first_type(normalized)

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO graph_documents (graph_id, id, type, payload, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (graph_id, id) DO UPDATE SET
                    type = EXCLUDED.type,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (graph, node_id, first_type, Jsonb(normalized)),
            )
            refresh_document_projection(
                cursor, graph_id=graph, node_id=node_id, document=normalized
            )

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        payload = self._get_base_jsonld(node_id, graph_id=graph_id)
        from pulsara_agent.graph.projection_relations import (
            PostgresCanonicalGraphRelationRepository,
        )

        repository = (
            PostgresCanonicalGraphRelationRepository(connection=self.connection)
            if self.connection is not None
            else PostgresCanonicalGraphRelationRepository(
                connection_provider=self.connection_provider
            )
        )
        return repository.merge_jsonld(
            graph_id=_graph_key(graph_id),
            node_id=node_id,
            base_document=payload,
        )

    def get_jsonld_read_view(
        self,
        node_id: str,
        graph_id: str | None = None,
    ) -> CanonicalGraphNodeReadViewFact:
        payload = self._get_base_jsonld(node_id, graph_id=graph_id)
        from pulsara_agent.graph.projection_relations import (
            PostgresCanonicalGraphRelationRepository,
        )

        repository = (
            PostgresCanonicalGraphRelationRepository(connection=self.connection)
            if self.connection is not None
            else PostgresCanonicalGraphRelationRepository(
                connection_provider=self.connection_provider
            )
        )
        return repository.merge_read_view(
            graph_id=_graph_key(graph_id),
            node_id=node_id,
            base_document=payload,
            base_document_semantic_fingerprint=context_fingerprint(
                "canonical-graph-base-document:v1", payload
            ),
        )

    def _get_base_jsonld(
        self,
        node_id: str,
        *,
        graph_id: str | None,
    ) -> dict[str, Any]:
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM graph_documents
                WHERE graph_id = %s AND id = %s
                """,
                (_graph_key(graph_id), node_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(node_id)
        payload = row["payload"]
        if not isinstance(payload, dict):
            raise TypeError(f"Stored JSON-LD payload is not an object for {node_id}")
        return payload

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM graph_documents
                WHERE graph_id = %s AND id = %s
                """,
                (_graph_key(graph_id), node_id),
            )
            return cursor.fetchone() is not None

    def find_by_type(
        self, type_name: Term, graph_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM graph_documents
                WHERE graph_id = %s AND type = %s
                ORDER BY id
                """,
                (_graph_key(graph_id), type_name.name),
            )
            rows = cursor.fetchall()
        return [row["payload"] for row in rows if isinstance(row.get("payload"), dict)]

    def set_status(
        self,
        node_id: str,
        status: memory.NodeStatus,
        *,
        updated_at: datetime,
        graph_id: str | None = None,
    ) -> None:
        graph = _graph_key(graph_id)
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM graph_documents
                WHERE graph_id = %s AND id = %s
                FOR UPDATE
                """,
                (graph, node_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(node_id)
            payload = row["payload"]
            if not isinstance(payload, dict):
                raise TypeError(
                    f"Stored JSON-LD payload is not an object for {node_id}"
                )
            payload[memory.STATUS.name] = status.value
            payload[memory.UPDATED_AT.name] = updated_at.isoformat()
            normalized = normalize_jsonld_document(payload, self.default_context)
            cursor.execute(
                """
                UPDATE graph_documents
                SET payload = %s, updated_at = now()
                WHERE graph_id = %s AND id = %s
                """,
                (Jsonb(normalized), graph, node_id),
            )
            if memory.STATUS.name not in normalized:
                raise ValueError(f"node is not a canonical memory node: {node_id}")
            refresh_document_projection(
                cursor, graph_id=graph, node_id=node_id, document=normalized
            )
            cursor.execute(
                "SELECT 1 FROM memory_nodes WHERE graph_id = %s AND id = %s",
                (graph, node_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(node_id)

    def query(
        self, sparql: str, bindings: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "PostgresGraphStore exposes typed reads via MemoryQuery, not raw SPARQL"
        )

    def update(self, sparql: str) -> None:
        raise NotImplementedError(
            "PostgresGraphStore uses typed mutations, not raw SPARQL"
        )

    def delete_graph(self, graph_id: str) -> None:
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            # Immutable relation facts are audit history and are never deleted by
            # the runtime role. Once their source document is absent they are no
            # longer reachable from a relation-aware graph read.
            cursor.execute(
                "DELETE FROM memory_vector_index WHERE graph_id = %s", (graph,)
            )
            cursor.execute(
                "DELETE FROM memory_search_index WHERE graph_id = %s", (graph,)
            )
            cursor.execute("DELETE FROM memory_relations WHERE graph_id = %s", (graph,))
            cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph,))
            cursor.execute("DELETE FROM graph_documents WHERE graph_id = %s", (graph,))

    @contextmanager
    def _cursor(self, *, row_factory=None) -> Iterator:
        if self.connection is not None:
            cursor_context = (
                self.connection.cursor(row_factory=row_factory)
                if row_factory is not None
                else self.connection.cursor()
            )
            with cursor_context as cursor:
                yield cursor
            return

        assert self.connection_provider is not None
        connection_context = self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_UOW,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )
        with connection_context as connection:
            with connection.cursor() as cursor:
                yield cursor


def _first_type(document: dict[str, Any]) -> str | None:
    types = document.get("@type")
    if isinstance(types, list) and types:
        first = types[0]
        return str(first) if first is not None else None
    if isinstance(types, str):
        return types
    return None
