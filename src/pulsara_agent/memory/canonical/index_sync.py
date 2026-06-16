"""Durable memory search-index synchronization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


@dataclass(slots=True)
class MemorySearchIndexSync:
    dsn: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if self.dsn is None and self.connection is None:
            raise ValueError("MemorySearchIndexSync requires either dsn or connection")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def rebuild(self, *, graph_id: str | None = None) -> int:
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM memory_search_index WHERE graph_id = %s", (graph,))
            cursor.execute(
                """
                INSERT INTO memory_search_index (
                    graph_id,
                    memory_id,
                    memory_type,
                    scope,
                    status,
                    fts,
                    aliases,
                    updated_at
                )
                SELECT
                    source.graph_id,
                    source.id,
                    source.memory_type,
                    source.scope,
                    source.status,
                    to_tsvector(
                        'simple',
                        coalesce(source.statement, '') || ' ' ||
                        coalesce(source.summary, '') || ' ' ||
                        coalesce(source.applies_when, '') || ' ' ||
                        coalesce(source.do_not_apply_when, '') || ' ' ||
                        array_to_string(source.aliases, ' ')
                    ),
                    source.aliases,
                    source.updated_at
                FROM (
                    SELECT
                        node.*,
                        ARRAY(
                            SELECT value
                            FROM (
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerTools')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerActions')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerFileGlobs')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerScopes')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerKeywords')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeTools')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeActions')) AS value
                                UNION ALL
                                SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeFileGlobs')) AS value
                            ) AS aliases
                        ) AS aliases
                    FROM memory_nodes AS node
                    JOIN graph_documents AS doc
                      ON doc.graph_id = node.graph_id AND doc.id = node.id
                    WHERE node.graph_id = %s
                ) AS source
                """,
                (graph,),
            )
            return cursor.rowcount

    def sync_memory(self, memory_id: str, *, graph_id: str | None = None) -> bool:
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            return _sync_memory_with_cursor(cursor, graph_id=graph, memory_id=memory_id)

    def consume_outbox(
        self,
        *,
        limit: int = 100,
        graph_id: str | None = None,
        governance_batch_id: str | None = None,
    ) -> int:
        applied = 0
        where = ["status = 'pending'"]
        params: list[object] = []
        if graph_id is not None:
            where.append("graph_id = %s")
            params.append(_graph_key(graph_id))
        if governance_batch_id is not None:
            where.append("governance_batch_id = %s")
            params.append(governance_batch_id)
        params.append(limit)
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT outbox_id, graph_id, payload
                FROM memory_write_outbox
                WHERE {" AND ".join(where)}
                ORDER BY created_at ASC, outbox_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
            for row in rows:
                try:
                    memory_ids = _outbox_memory_ids(row["payload"])
                    for memory_id in memory_ids:
                        _sync_memory_with_cursor(cursor, graph_id=row["graph_id"], memory_id=memory_id)
                    cursor.execute(
                        """
                        UPDATE memory_write_outbox
                        SET status = 'applied', applied_at = now()
                        WHERE outbox_id = %s
                        """,
                        (row["outbox_id"],),
                    )
                    applied += 1
                except Exception:
                    cursor.execute(
                        """
                        UPDATE memory_write_outbox
                        SET status = 'failed'
                        WHERE outbox_id = %s
                        """,
                        (row["outbox_id"],),
                    )
                    raise
        return applied

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


def _outbox_memory_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    explicit = payload.get("index_dirty")
    if isinstance(explicit, list):
        return tuple(item for item in explicit if isinstance(item, str))
    decision_record = payload.get("decision_record")
    if not isinstance(decision_record, dict):
        return ()
    outcome = decision_record.get("write_outcome")
    if not isinstance(outcome, dict):
        return ()
    memory_id = outcome.get("memory_id")
    if isinstance(memory_id, str):
        return (memory_id,)
    return ()


def _sync_memory_with_cursor(cursor, *, graph_id: str, memory_id: str) -> bool:
    cursor.execute(
        """
        INSERT INTO memory_search_index (
            graph_id,
            memory_id,
            memory_type,
            scope,
            status,
            fts,
            aliases,
            updated_at
        )
        SELECT
            source.graph_id,
            source.id,
            source.memory_type,
            source.scope,
            source.status,
            to_tsvector(
                'simple',
                coalesce(source.statement, '') || ' ' ||
                coalesce(source.summary, '') || ' ' ||
                coalesce(source.applies_when, '') || ' ' ||
                coalesce(source.do_not_apply_when, '') || ' ' ||
                array_to_string(source.aliases, ' ')
            ),
            source.aliases,
            source.updated_at
        FROM (
            SELECT
                node.*,
                ARRAY(
                    SELECT value
                    FROM (
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerTools')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerActions')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerFileGlobs')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerScopes')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'triggerKeywords')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeTools')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeActions')) AS value
                        UNION ALL
                        SELECT jsonb_array_elements_text(pulsara_jsonb_text_array(doc.payload->'negativeFileGlobs')) AS value
                    ) AS aliases
                ) AS aliases
            FROM memory_nodes AS node
            JOIN graph_documents AS doc
              ON doc.graph_id = node.graph_id AND doc.id = node.id
            WHERE node.graph_id = %s AND node.id = %s
        ) AS source
        ON CONFLICT (graph_id, memory_id) DO UPDATE SET
            memory_type = EXCLUDED.memory_type,
            scope = EXCLUDED.scope,
            status = EXCLUDED.status,
            fts = EXCLUDED.fts,
            aliases = EXCLUDED.aliases,
            updated_at = EXCLUDED.updated_at
        """,
        (graph_id, memory_id),
    )
    indexed = cursor.rowcount > 0
    if not indexed:
        cursor.execute(
            "DELETE FROM memory_search_index WHERE graph_id = %s AND memory_id = %s",
            (graph_id, memory_id),
        )
    return indexed
