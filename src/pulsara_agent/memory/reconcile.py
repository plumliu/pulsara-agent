"""Memory substrate reconciliation helpers.

The reconciler repairs derived projections (currently search index rows) and
reports canonical-memory rows that lack the governance/outbox audit envelope the
durable write path is expected to create.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.memory.index_sync import MemorySearchIndexSync
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


@dataclass(frozen=True, slots=True)
class DamagedMemoryNode:
    graph_id: str
    memory_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    outbox_applied_count: int
    damaged_nodes: tuple[DamagedMemoryNode, ...]


@dataclass(slots=True)
class PostgresMemoryReconciler:
    dsn: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if self.dsn is None and self.connection is None:
            raise ValueError("PostgresMemoryReconciler requires either dsn or connection")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def reconcile(
        self,
        *,
        graph_id: str | None = None,
        outbox_limit: int = 100,
    ) -> ReconciliationReport:
        applied = self.replay_outbox(graph_id=graph_id, limit=outbox_limit)
        damaged = self.find_damaged_nodes(graph_id=graph_id)
        return ReconciliationReport(
            outbox_applied_count=applied,
            damaged_nodes=damaged,
        )

    def replay_outbox(self, *, graph_id: str | None = None, limit: int = 100) -> int:
        sync = (
            MemorySearchIndexSync(connection=self.connection)
            if self.connection is not None
            else MemorySearchIndexSync(dsn=self.dsn)
        )
        return sync.consume_outbox(graph_id=graph_id, limit=limit)

    def find_damaged_nodes(
        self,
        *,
        graph_id: str | None = None,
        limit: int = 100,
    ) -> tuple[DamagedMemoryNode, ...]:
        where = []
        params: list[object] = []
        if graph_id is not None:
            where.append("node.graph_id = %s")
            params.append(_graph_key(graph_id))
        params.append(limit)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT node.graph_id, node.id
                FROM memory_nodes AS node
                LEFT JOIN memory_write_outbox AS outbox
                  ON outbox.graph_id = node.graph_id
                 AND outbox.payload->'decision_record'->'write_outcome'->>'memory_id' = node.id
                {where_sql}
                  {"AND" if where else "WHERE"} outbox.outbox_id IS NULL
                ORDER BY node.graph_id ASC, node.id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            return tuple(
                DamagedMemoryNode(
                    graph_id=row["graph_id"],
                    memory_id=row["id"],
                    reason="missing_governance_outbox",
                )
                for row in cursor.fetchall()
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
