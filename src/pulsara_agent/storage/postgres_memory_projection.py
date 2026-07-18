"""Synchronous Postgres projection refresh for canonical memory docs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pulsara_agent.ontology import memory, runtime as rt


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


def refresh_document_projection(cursor, *, graph_id: str, node_id: str, document: dict[str, Any]) -> None:
    projection = memory_node_projection(document)
    relation_rows = tuple(iter_relation_rows(document))
    cursor.execute(
        "SELECT node_revision FROM memory_nodes "
        "WHERE graph_id = %s AND id = %s FOR UPDATE",
        (graph_id, node_id),
    )
    previous = cursor.fetchone()
    previous_revision = (
        None
        if previous is None
        else previous["node_revision"]
        if isinstance(previous, dict)
        else previous[0]
    )
    node_revision = 1 if previous_revision is None else int(previous_revision) + 1
    cursor.execute(
        "DELETE FROM memory_nodes WHERE graph_id = %s AND id = %s",
        (graph_id, node_id),
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
                node_revision,
                stale_after,
                expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::timestamptz, %s::timestamptz, %s,
                %s::timestamptz, %s::timestamptz
            )
            """,
            (
                graph_id,
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
                node_revision,
                projection["stale_after"],
                projection["expires_at"],
            ),
        )
    sync_relations_from_document(cursor, graph_id=graph_id, source_id=node_id, rows=relation_rows)


def sync_relations_from_document(
    cursor,
    *,
    graph_id: str,
    source_id: str,
    rows: tuple[tuple[str, str], ...],
) -> None:
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


def memory_node_projection(document: dict[str, Any]) -> dict[str, Any] | None:
    memory_type = canonical_memory_type(document)
    if memory_type is None:
        return None
    if any(not non_empty(document.get(key)) for key in _REQUIRED_MEMORY_PROJECTION_KEYS):
        return None
    return {
        "memory_type": memory_type,
        "scope": str(document[memory.SCOPE.name]),
        "status": str(document[memory.STATUS.name]),
        "statement": str(document[memory.STATEMENT.name]),
        "summary": optional_str(document.get(memory.SUMMARY.name)),
        "source_authority": optional_str(document.get(memory.SOURCE_AUTHORITY.name)),
        "verification_status": optional_str(document.get(memory.VERIFICATION_STATUS.name)),
        "confidence_level": optional_str(document.get(memory.CONFIDENCE_LEVEL.name)),
        "applies_when": optional_str(document.get(memory.APPLIES_WHEN.name)),
        "do_not_apply_when": optional_str(document.get(memory.DO_NOT_APPLY_WHEN.name)),
        "created_at": str(document[memory.CREATED_AT.name]),
        "updated_at": str(document[memory.UPDATED_AT.name]),
        "stale_after": optional_str(document.get(memory.STALE_AFTER.name)),
        "expires_at": optional_str(document.get(memory.EXPIRES_AT.name)),
    }


def canonical_memory_type(document: dict[str, Any]) -> str | None:
    types = document.get("@type")
    values = types if isinstance(types, list) else [types]
    for value in values:
        type_name = str(value)
        if type_name in _MEMORY_TYPE_NAMES:
            return type_name
    return None


def iter_relation_rows(document: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for key, value in document.items():
        if key in {"@context", "@id", "@type"}:
            continue
        if key not in _PROJECTED_RELATION_PREDICATES:
            continue
        for target_id in node_ref_ids(value):
            yield key, target_id


def node_ref_ids(value: Any) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from node_ref_ids(item)
        return
    if not isinstance(value, dict):
        return
    node_id = value.get("@id")
    if isinstance(node_id, str) and node_id:
        yield node_id


def non_empty(value: Any) -> bool:
    return value is not None and str(value) != ""


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
