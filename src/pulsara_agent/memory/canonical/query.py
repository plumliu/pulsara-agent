"""Typed canonical memory queries for recall."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class CanonicalNodeView:
    id: str
    memory_type: str
    scope: str
    status: memory.NodeStatus
    statement: str
    summary: str | None
    source_authority: memory.SourceAuthority | None
    verification_status: memory.VerificationStatus | None
    confidence_level: memory.ConfidenceLevel | None
    applies_when: str | None
    do_not_apply_when: str | None
    created_at: datetime
    updated_at: datetime
    node_revision: int
    evidence_ids: tuple[str, ...]
    outgoing: tuple[tuple[str, str], ...]
    incoming: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class MemoryRelationEdge:
    source_id: str
    predicate: str
    target_id: str


class MemoryQuery(Protocol):
    def fetch_nodes(self, ids: Sequence[str], *, graph_id: str | None = None) -> list[CanonicalNodeView]: ...

    def lexical_candidates(
        self,
        *,
        terms: Sequence[str],
        scopes: Sequence[str] | None,
        types: Sequence[str] | None,
        limit: int,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]: ...

    def fts_candidates(
        self,
        *,
        query_text: str,
        scopes: Sequence[str] | None,
        types: Sequence[str] | None,
        limit: int,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]: ...

    def exact_candidates(
        self,
        *,
        statement: str,
        scope: str,
        memory_type: str,
        graph_id: str | None = None,
    ) -> list[str]: ...

    def missing_vector_ids(
        self,
        *,
        embedding_fingerprint: str,
        scopes: Sequence[str],
        types: Sequence[str],
        limit: int,
        graph_id: str | None = None,
    ) -> list[str]: ...

    def relation_edges(
        self,
        node_ids: Sequence[str],
        *,
        graph_id: str | None = None,
        max_per_source: int | None = None,
    ) -> list[MemoryRelationEdge]: ...


@dataclass(slots=True)
class PostgresMemoryQuery:
    dsn: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if self.dsn is None and self.connection is None:
            raise ValueError("PostgresMemoryQuery requires either dsn or connection")

    def fetch_nodes(self, ids: Sequence[str], *, graph_id: str | None = None) -> list[CanonicalNodeView]:
        ordered_ids = _dedupe_preserving_order(ids)
        if not ordered_ids:
            return []
        graph = _graph_key(graph_id)

        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id, memory_type, scope, status, statement, summary, source_authority,
                       verification_status, confidence_level, applies_when, do_not_apply_when,
                       created_at, updated_at, node_revision
                FROM memory_nodes
                WHERE graph_id = %s AND id = ANY(%s)
                """,
                (graph, ordered_ids),
            )
            node_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT source_id, predicate, target_id
                FROM memory_relations
                WHERE graph_id = %s AND source_id = ANY(%s)
                """,
                (graph, ordered_ids),
            )
            outgoing_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT source_id, predicate, target_id
                FROM memory_relations
                WHERE graph_id = %s AND target_id = ANY(%s)
                """,
                (graph, ordered_ids),
            )
            incoming_rows = cursor.fetchall()

        outgoing_by_id: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in ordered_ids}
        for row in outgoing_rows:
            outgoing_by_id.setdefault(row["source_id"], []).append((row["predicate"], row["target_id"]))

        incoming_by_id: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in ordered_ids}
        for row in incoming_rows:
            incoming_by_id.setdefault(row["target_id"], []).append((row["predicate"], row["source_id"]))

        by_id = {
            row["id"]: _node_view_from_row(
                row,
                outgoing=tuple(outgoing_by_id.get(row["id"], ())),
                incoming=tuple(incoming_by_id.get(row["id"], ())),
            )
            for row in node_rows
        }
        return [by_id[node_id] for node_id in ordered_ids if node_id in by_id]

    def relation_edges(
        self,
        node_ids: Sequence[str],
        *,
        graph_id: str | None = None,
        max_per_source: int | None = None,
    ) -> list[MemoryRelationEdge]:
        ordered_ids = _dedupe_preserving_order(node_ids)
        if not ordered_ids:
            return []
        with self._cursor() as cursor:
            if max_per_source is not None and max_per_source > 0:
                # Bound rows per matched frontier endpoint in SQL so a single
                # high-degree supernode cannot pull thousands of edges onto the
                # hot path before the Python-side fanout cap applies. Each edge
                # is attributed to whichever endpoint(s) sit in the frontier;
                # the per-endpoint ROW_NUMBER cap mirrors graph fanout_per_node.
                cursor.execute(
                    """
                    WITH matched AS (
                        SELECT source_id, predicate, target_id, source_id AS endpoint
                        FROM memory_relations
                        WHERE graph_id = %s AND source_id = ANY(%s)
                        UNION
                        SELECT source_id, predicate, target_id, target_id AS endpoint
                        FROM memory_relations
                        WHERE graph_id = %s AND target_id = ANY(%s)
                    ),
                    ranked AS (
                        SELECT
                            source_id, predicate, target_id, endpoint,
                            ROW_NUMBER() OVER (
                                PARTITION BY endpoint
                                ORDER BY predicate, source_id, target_id
                            ) AS rn
                        FROM matched
                    )
                    SELECT DISTINCT source_id, predicate, target_id
                    FROM ranked
                    WHERE rn <= %s
                    ORDER BY predicate, source_id, target_id
                    """,
                    (
                        _graph_key(graph_id),
                        ordered_ids,
                        _graph_key(graph_id),
                        ordered_ids,
                        max_per_source,
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT source_id, predicate, target_id
                    FROM memory_relations
                    WHERE graph_id = %s
                      AND (source_id = ANY(%s) OR target_id = ANY(%s))
                    ORDER BY predicate, source_id, target_id
                    """,
                    (_graph_key(graph_id), ordered_ids, ordered_ids),
                )
            return [
                MemoryRelationEdge(
                    source_id=row["source_id"],
                    predicate=row["predicate"],
                    target_id=row["target_id"],
                )
                for row in cursor.fetchall()
            ]

    def lexical_candidates(
        self,
        *,
        terms: Sequence[str],
        scopes: Sequence[str] | None,
        types: Sequence[str] | None,
        limit: int,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        normalized_terms = _normalized_terms(terms)
        if not normalized_terms or limit <= 0:
            return []
        where, params = _candidate_filters(graph_id=graph_id, scopes=scopes, types=types)
        patterns = [f"%{term}%" for term in normalized_terms]
        where.append(
            """
            (
                lower(id) = ANY(%s)
                OR lower(scope) = ANY(%s)
                OR lower(memory_type) = ANY(%s)
                OR lower(statement) LIKE ANY(%s)
                OR lower(coalesce(summary, '')) LIKE ANY(%s)
            )
            """
        )
        params.extend([normalized_terms, normalized_terms, normalized_terms, patterns, patterns])

        with self._cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, memory_type, scope, statement, summary, updated_at
                FROM memory_nodes
                WHERE {" AND ".join(where)}
                ORDER BY updated_at DESC, id ASC
                LIMIT %s
                """,
                (*params, max(limit * 4, limit)),
            )
            rows = cursor.fetchall()

        scored = [
            (row["id"], _lexical_score(row, normalized_terms))
            for row in rows
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    def fts_candidates(
        self,
        *,
        query_text: str,
        scopes: Sequence[str] | None,
        types: Sequence[str] | None,
        limit: int,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        normalized = " ".join(query_text.split())
        if not normalized or limit <= 0:
            return []
        where, params = _candidate_filters(graph_id=graph_id, scopes=scopes, types=types)
        where.append("fts @@ q.query")

        with self._cursor() as cursor:
            cursor.execute(
                f"""
                WITH q AS (SELECT plainto_tsquery('simple', %s) AS query)
                SELECT memory_id AS id, ts_rank_cd(fts, q.query) AS rank
                FROM memory_search_index, q
                WHERE {" AND ".join(where)}
                ORDER BY rank DESC, updated_at DESC, id ASC
                LIMIT %s
                """,
                (normalized, *params, limit),
            )
            rows = cursor.fetchall()
        return [(row["id"], float(row["rank"] or 0.0)) for row in rows]

    def exact_candidates(
        self,
        *,
        statement: str,
        scope: str,
        memory_type: str,
        graph_id: str | None = None,
    ) -> list[str]:
        normalized = " ".join(statement.casefold().split())
        if not normalized:
            return []
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM memory_nodes
                WHERE graph_id = %s
                  AND scope = %s
                  AND memory_type = %s
                  AND status = %s
                  AND lower(regexp_replace(trim(statement), '\\s+', ' ', 'g')) = %s
                ORDER BY id
                """,
                (
                    _graph_key(graph_id),
                    scope,
                    memory_type,
                    memory.NodeStatus.ACTIVE.value,
                    normalized,
                ),
            )
            return [row["id"] for row in cursor.fetchall()]

    def missing_vector_ids(
        self,
        *,
        embedding_fingerprint: str,
        scopes: Sequence[str],
        types: Sequence[str],
        limit: int,
        graph_id: str | None = None,
    ) -> list[str]:
        if limit <= 0 or not scopes or not types:
            return []
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT node.id
                FROM memory_nodes AS node
                LEFT JOIN memory_vector_index AS vector
                  ON vector.graph_id = node.graph_id
                 AND vector.memory_id = node.id
                 AND vector.embedding_fingerprint = %s
                WHERE node.graph_id = %s
                  AND node.scope = ANY(%s)
                  AND node.memory_type = ANY(%s)
                  AND node.status = %s
                  AND vector.memory_id IS NULL
                ORDER BY node.updated_at DESC, node.id
                LIMIT %s
                """,
                (
                    embedding_fingerprint,
                    _graph_key(graph_id),
                    list(scopes),
                    list(types),
                    memory.NodeStatus.ACTIVE.value,
                    limit,
                ),
            )
            return [row["id"] for row in cursor.fetchall()]

    @contextmanager
    def _cursor(self) -> Iterator:
        if self.connection is not None:
            with self.connection.cursor(row_factory=dict_row) as cursor:
                yield cursor
            return

        assert self.dsn is not None
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                yield cursor


def _candidate_filters(
    *,
    graph_id: str | None,
    scopes: Sequence[str] | None,
    types: Sequence[str] | None,
) -> tuple[list[str], list[object]]:
    where = ["graph_id = %s", "status = %s"]
    params: list[object] = [_graph_key(graph_id), memory.NodeStatus.ACTIVE.value]
    if scopes:
        where.append("scope = ANY(%s)")
        params.append(list(scopes))
    if types:
        where.append("memory_type = ANY(%s)")
        params.append(list(types))
    return where, params


def _node_view_from_row(
    row: dict,
    *,
    outgoing: tuple[tuple[str, str], ...],
    incoming: tuple[tuple[str, str], ...],
) -> CanonicalNodeView:
    evidence_ids = tuple(source_id for predicate, source_id in incoming if predicate == memory.SUPPORTS.name)
    return CanonicalNodeView(
        id=row["id"],
        memory_type=row["memory_type"],
        scope=row["scope"],
        status=memory.NodeStatus(row["status"]),
        statement=row["statement"],
        summary=row["summary"],
        source_authority=_optional_enum(memory.SourceAuthority, row["source_authority"]),
        verification_status=_optional_enum(memory.VerificationStatus, row["verification_status"]),
        confidence_level=_optional_enum(memory.ConfidenceLevel, row["confidence_level"]),
        applies_when=row["applies_when"],
        do_not_apply_when=row["do_not_apply_when"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        node_revision=int(row["node_revision"]),
        evidence_ids=evidence_ids,
        outgoing=outgoing,
        incoming=incoming,
    )


def _optional_enum(enum_type, value):
    if value is None:
        return None
    return enum_type(value)


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _normalized_terms(terms: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = " ".join(term.casefold().split())
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _lexical_score(row: dict, terms: Sequence[str]) -> float:
    fields = {
        "id": str(row["id"]).casefold(),
        "scope": str(row["scope"]).casefold(),
        "memory_type": str(row["memory_type"]).casefold(),
        "statement": str(row["statement"]).casefold(),
        "summary": str(row["summary"] or "").casefold(),
    }
    score = 0.0
    for term in terms:
        if fields["id"] == term:
            score += 10.0
        if fields["scope"] == term:
            score += 4.0
        if fields["memory_type"] == term:
            score += 3.0
        if term in fields["statement"]:
            score += 2.0
        if term in fields["summary"]:
            score += 1.0
    return score
