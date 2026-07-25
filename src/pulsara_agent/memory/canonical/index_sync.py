"""Durable memory search-index synchronization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic

from psycopg import Connection

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(slots=True)
class MemorySearchIndexSync:
    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError(
                "MemorySearchIndexSync requires exactly one verified provider or transaction connection"
            )

    def rebuild(self, *, graph_id: str | None = None) -> int:
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            cursor.execute(
                "DELETE FROM memory_search_index WHERE graph_id = %s", (graph,)
            )
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
            lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )
        with connection_context as connection:
            with connection.cursor() as cursor:
                yield cursor


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
