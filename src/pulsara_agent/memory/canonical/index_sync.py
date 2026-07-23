"""Durable memory search-index synchronization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any
import traceback

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    mark_surface_applied,
    mark_surface_failed,
    parse_mutation_payload,
    pending_surface_names,
)
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
        where = ["status IN ('pending', 'partial', 'failed')"]
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
                ORDER BY sequence_key ASC, created_at ASC, outbox_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
            for row in rows:
                try:
                    payload_model = parse_mutation_payload(row["payload"])
                    if not pending_surface_names(
                        payload_model,
                        CanonicalMutationSurface.SEARCH_INDEX.value,
                    ):
                        continue
                    memory_ids = _outbox_memory_ids(payload_model.model_dump(mode="json"))
                    for memory_id in memory_ids:
                        _sync_memory_with_cursor(cursor, graph_id=row["graph_id"], memory_id=memory_id)
                    payload, top_level_status = mark_surface_applied(
                        payload_model,
                        CanonicalMutationSurface.SEARCH_INDEX.value,
                    )
                    cursor.execute(
                        """
                        UPDATE memory_write_outbox
                        SET payload = %s,
                            status = %s,
                            attempt_count = attempt_count + 1,
                            last_error = NULL,
                            applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END
                        WHERE outbox_id = %s
                        """,
                        (Jsonb(payload), top_level_status, top_level_status, row["outbox_id"]),
                    )
                    applied += 1
                except Exception as exc:
                    payload_model = parse_mutation_payload(row["payload"])
                    payload, top_level_status = mark_surface_failed(
                        payload_model,
                        CanonicalMutationSurface.SEARCH_INDEX.value,
                    )
                    cursor.execute(
                        """
                        UPDATE memory_write_outbox
                        SET payload = %s,
                            status = %s,
                            attempt_count = attempt_count + 1,
                            last_error = %s
                        WHERE outbox_id = %s
                        """,
                        (
                            Jsonb(payload),
                            top_level_status,
                            "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                            row["outbox_id"],
                        ),
                    )
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

        assert self.connection_provider is not None
        connection_context = self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )
        with connection_context as connection:
            with connection.cursor() as cursor:
                yield cursor


def _outbox_memory_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    if payload.get("kind") == "canonical_mutation":
        explicit_dirty = payload.get("dirty_memory_ids")
        if isinstance(explicit_dirty, list):
            return tuple(item for item in explicit_dirty if isinstance(item, str))
    explicit = payload.get("index_dirty")
    if isinstance(explicit, list):
        return tuple(item for item in explicit if isinstance(item, str))
    decision_record = payload.get("decision_record")
    if not isinstance(decision_record, dict):
        return ()
    outcome = decision_record.get("write_outcome")
    if not isinstance(outcome, dict):
        return ()
    memory_ids: list[str] = []
    memory_id = outcome.get("memory_id")
    if isinstance(memory_id, str):
        memory_ids.append(memory_id)
    superseded = outcome.get("superseded_memory_ids")
    if isinstance(superseded, list):
        memory_ids.extend(item for item in superseded if isinstance(item, str))
    return tuple(dict.fromkeys(memory_ids))


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
