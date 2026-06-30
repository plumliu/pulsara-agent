"""Recall trace persistence and recent-injection lookup."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


class RecallTraceStore(Protocol):
    def record(
        self,
        *,
        graph_id: str | None,
        session_id: str,
        run_id: str,
        turn_id: str,
        reply_id: str,
        query_text: str,
        trigger_kind: str,
        candidate_ids: Sequence[str],
        included_ids: Sequence[str],
        filtered_ids: Sequence[str],
        warnings: Sequence[str],
        latency_ms: int,
        injected: bool,
        selected_by_tool: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> str: ...

    def recent_injected_ids(
        self,
        *,
        graph_id: str | None,
        session_id: str,
        limit: int,
    ) -> tuple[str, ...]: ...


@dataclass(slots=True)
class PostgresRecallTraceStore:
    dsn: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if self.dsn is None and self.connection is None:
            raise ValueError("PostgresRecallTraceStore requires either dsn or connection")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def record(
        self,
        *,
        graph_id: str | None,
        session_id: str,
        run_id: str,
        turn_id: str,
        reply_id: str,
        query_text: str,
        trigger_kind: str,
        candidate_ids: Sequence[str],
        included_ids: Sequence[str],
        filtered_ids: Sequence[str],
        warnings: Sequence[str],
        latency_ms: int,
        injected: bool,
        selected_by_tool: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        trace_id = f"recall:{uuid4().hex}"
        graph = _graph_key(graph_id)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO recall_traces (
                    trace_id,
                    graph_id,
                    session_id,
                    run_id,
                    turn_id,
                    reply_id,
                    query,
                    trigger_kind,
                    candidate_ids,
                    included_ids,
                    filtered_ids,
                    warnings,
                    metadata,
                    latency_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trace_id,
                    graph,
                    session_id,
                    run_id,
                    turn_id,
                    reply_id,
                    query_text,
                    trigger_kind,
                    Jsonb(list(candidate_ids)),
                    Jsonb(list(included_ids)),
                    Jsonb(list(filtered_ids)),
                    Jsonb(list(warnings)),
                    Jsonb(dict(metadata or {})),
                    latency_ms,
                ),
            )
            if included_ids:
                cursor.executemany(
                    """
                    INSERT INTO recall_usages (
                        trace_id,
                        graph_id,
                        memory_id,
                        injected,
                        selected_by_tool
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (trace_id, memory_id) DO NOTHING
                    """,
                    [
                        (trace_id, graph, memory_id, injected, selected_by_tool)
                        for memory_id in included_ids
                    ],
                )
        return trace_id

    def recent_injected_ids(
        self,
        *,
        graph_id: str | None,
        session_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        graph = _graph_key(graph_id)
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT usage.memory_id, max(trace.created_at) AS last_seen_at
                FROM recall_usages AS usage
                JOIN recall_traces AS trace ON trace.trace_id = usage.trace_id
                WHERE usage.graph_id = %s
                  AND trace.graph_id = %s
                  AND trace.session_id = %s
                  AND usage.injected IS TRUE
                GROUP BY usage.memory_id
                ORDER BY last_seen_at DESC, usage.memory_id ASC
                LIMIT %s
                """,
                (graph, graph, session_id, limit),
            )
            return tuple(row["memory_id"] for row in cursor.fetchall())

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
