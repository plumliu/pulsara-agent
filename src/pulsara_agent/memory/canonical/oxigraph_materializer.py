"""Apply canonical mutation outbox rows into Oxigraph."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import traceback

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    mark_surface_applied,
    mark_surface_failed,
    parse_mutation_payload,
    pending_surface_names,
)
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


@dataclass(slots=True)
class OxigraphMaterializer:
    oxigraph: OxigraphGraphStore
    dsn: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if self.dsn is None and self.connection is None:
            raise ValueError("OxigraphMaterializer requires either dsn or connection")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def consume_outbox(
        self,
        *,
        limit: int = 100,
        graph_id: str | None = None,
    ) -> int:
        applied = 0
        where = ["status IN ('pending', 'partial', 'failed')"]
        params: list[object] = []
        if graph_id is not None:
            where.append("graph_id = %s")
            params.append(graph_id)
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
                payload_model = parse_mutation_payload(row["payload"])
                if not pending_surface_names(payload_model, CanonicalMutationSurface.OXIGRAPH.value):
                    continue
                try:
                    if payload_model.graph_reset:
                        self.oxigraph.delete_graph(row["graph_id"])
                    else:
                        for item in payload_model.documents:
                            self.oxigraph.put_jsonld(item.document, graph_id=row["graph_id"])
                    payload, top_level_status = mark_surface_applied(
                        payload_model,
                        CanonicalMutationSurface.OXIGRAPH.value,
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
                    payload, top_level_status = mark_surface_failed(
                        payload_model,
                        CanonicalMutationSurface.OXIGRAPH.value,
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

        assert self.dsn is not None
        connection_context = (
            psycopg.connect(self.dsn, row_factory=row_factory)
            if row_factory is not None
            else psycopg.connect(self.dsn)
        )
        with connection_context as connection:
            with connection.cursor() as cursor:
                yield cursor
