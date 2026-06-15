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
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.graph.jsonld_codec import normalize_jsonld_document
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology import memory
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


_MEMORY_TYPE_NAMES = {
    memory.CLAIM.name,
    memory.DECISION.name,
    memory.PREFERENCE.name,
    memory.ACTION_BOUNDARY.name,
    memory.OBSERVATION.name,
}

_REQUIRED_MEMORY_PROJECTION_KEYS = (
    memory.STATEMENT.name,
    memory.SCOPE.name,
    memory.STATUS.name,
    memory.CREATED_AT.name,
    memory.UPDATED_AT.name,
)
_PROJECTED_RELATION_PREDICATES = frozenset(
    {
        rt.PROVIDES.name,
        memory.SUPPORTS.name,
        memory.SUPERSEDES.name,
        memory.CONTRADICTS.name,
        memory.HAS_EVIDENCE.name,
        memory.BASED_ON.name,
        memory.DERIVED_FROM.name,
    }
)


@dataclass(slots=True)
class PostgresGraphStore:
    """GraphStore backed by Postgres JSONB plus canonical memory projections."""

    dsn: str | None = None
    connection: Connection | None = None
    default_context: dict[str, Any] | None = None
    initialize_schema: bool = True

    def __post_init__(self) -> None:
        if self.default_context is None:
            self.default_context = CORE_CONTEXT
        if self.dsn is None and self.connection is None:
            raise ValueError("PostgresGraphStore requires either dsn or connection")
        if self.initialize_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        normalized = normalize_jsonld_document(document, self.default_context)
        node_id = normalized["@id"]
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        graph = _graph_key(graph_id)
        first_type = _first_type(normalized)
        projection = _memory_node_projection(normalized)
        relation_rows = tuple(_relation_rows(normalized))

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
            cursor.execute(
                "DELETE FROM memory_nodes WHERE graph_id = %s AND id = %s",
                (graph, node_id),
            )
            if projection is not None:
                cursor.execute(
                    """
                    INSERT INTO memory_nodes (
                        graph_id,
                        id,
                        memory_type,
                        scope,
                        status,
                        statement,
                        summary,
                        source_authority,
                        verification_status,
                        confidence_level,
                        applies_when,
                        do_not_apply_when,
                        created_at,
                        updated_at,
                        stale_after,
                        expires_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::timestamptz, %s::timestamptz, %s::timestamptz, %s::timestamptz
                    )
                    """,
                    (
                        graph,
                        node_id,
                        projection["memory_type"],
                        projection["scope"],
                        projection["status"],
                        projection["statement"],
                        projection["summary"],
                        projection["source_authority"],
                        projection["verification_status"],
                        projection["confidence_level"],
                        projection["applies_when"],
                        projection["do_not_apply_when"],
                        projection["created_at"],
                        projection["updated_at"],
                        projection["stale_after"],
                        projection["expires_at"],
                    ),
                )
            self._sync_relations_from_document(cursor, graph_id=graph, source_id=node_id, rows=relation_rows)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
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

    def find_by_type(self, type_name: Term, graph_id: str | None = None) -> list[dict[str, Any]]:
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
                raise TypeError(f"Stored JSON-LD payload is not an object for {node_id}")
            payload[memory.STATUS.name] = status.value
            payload[memory.UPDATED_AT.name] = updated_at.isoformat()
            normalized = normalize_jsonld_document(payload, self.default_context)
            projection = _memory_node_projection(normalized)
            if projection is None:
                raise ValueError(f"node is not a canonical memory node: {node_id}")
            cursor.execute(
                """
                UPDATE graph_documents
                SET payload = %s, updated_at = now()
                WHERE graph_id = %s AND id = %s
                """,
                (Jsonb(normalized), graph, node_id),
            )
            cursor.execute(
                """
                UPDATE memory_nodes
                SET status = %s,
                    updated_at = %s::timestamptz
                WHERE graph_id = %s AND id = %s
                """,
                (status.value, updated_at.isoformat(), graph, node_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(node_id)

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("PostgresGraphStore exposes typed reads via MemoryQuery, not raw SPARQL")

    def update(self, sparql: str) -> None:
        raise NotImplementedError("PostgresGraphStore uses typed mutations, not raw SPARQL")

    def delete_graph(self, graph_id: str) -> None:
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM memory_relations WHERE graph_id = %s", (graph,))
            cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph,))
            cursor.execute("DELETE FROM graph_documents WHERE graph_id = %s", (graph,))

    @staticmethod
    def _sync_relations_from_document(cursor, *, graph_id: str, source_id: str, rows: tuple[tuple[str, str], ...]) -> None:
        cursor.execute(
            "DELETE FROM memory_relations WHERE graph_id = %s AND source_id = %s",
            (graph_id, source_id),
        )
        if not rows:
            return
        cursor.executemany(
            """
            INSERT INTO memory_relations (graph_id, source_id, predicate, target_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(graph_id, source_id, predicate, target_id) for predicate, target_id in rows],
        )

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

        assert self.dsn is not None
        connection_context = (
            psycopg.connect(self.dsn, row_factory=row_factory)
            if row_factory is not None
            else psycopg.connect(self.dsn)
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


def _memory_node_projection(document: dict[str, Any]) -> dict[str, Any] | None:
    memory_type = _canonical_memory_type(document)
    if memory_type is None:
        return None
    if any(not _non_empty(document.get(key)) for key in _REQUIRED_MEMORY_PROJECTION_KEYS):
        return None
    return {
        "memory_type": memory_type,
        "scope": str(document[memory.SCOPE.name]),
        "status": str(document[memory.STATUS.name]),
        "statement": str(document[memory.STATEMENT.name]),
        "summary": _optional_str(document.get(memory.SUMMARY.name)),
        "source_authority": _optional_str(document.get(memory.SOURCE_AUTHORITY.name)),
        "verification_status": _optional_str(document.get(memory.VERIFICATION_STATUS.name)),
        "confidence_level": _optional_str(document.get(memory.CONFIDENCE_LEVEL.name)),
        "applies_when": _optional_str(document.get(memory.APPLIES_WHEN.name)),
        "do_not_apply_when": _optional_str(document.get(memory.DO_NOT_APPLY_WHEN.name)),
        "created_at": str(document[memory.CREATED_AT.name]),
        "updated_at": str(document[memory.UPDATED_AT.name]),
        "stale_after": _optional_str(document.get(memory.STALE_AFTER.name)),
        "expires_at": _optional_str(document.get(memory.EXPIRES_AT.name)),
    }


def _canonical_memory_type(document: dict[str, Any]) -> str | None:
    types = document.get("@type")
    values = types if isinstance(types, list) else [types]
    for value in values:
        type_name = str(value)
        if type_name in _MEMORY_TYPE_NAMES:
            return type_name
    return None


def _relation_rows(document: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for key, value in document.items():
        if key in {"@context", "@id", "@type"}:
            continue
        if key not in _PROJECTED_RELATION_PREDICATES:
            continue
        for target_id in _node_ref_ids(value):
            yield key, target_id


def _node_ref_ids(value: Any) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from _node_ref_ids(item)
        return
    if not isinstance(value, dict):
        return
    node_id = value.get("@id")
    if isinstance(node_id, str) and node_id:
        yield node_id


def _non_empty(value: Any) -> bool:
    return value is not None and str(value) != ""


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
