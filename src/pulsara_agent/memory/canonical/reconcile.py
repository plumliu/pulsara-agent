"""Memory substrate reconciliation helpers.

The reconciler repairs derived projections (currently search index rows) and
reports canonical-memory rows that lack the governance/outbox audit envelope the
durable write path is expected to create.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic

from psycopg import Connection
from psycopg.rows import dict_row

from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.graph.jsonld_codec import expand_graph_id as _expand_graph_id
from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.graph.jsonld_codec import iri_token as _iri_token
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.index_sync import _outbox_memory_ids
from pulsara_agent.memory.canonical.oxigraph_materializer import OxigraphMaterializer
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class DamagedMemoryNode:
    graph_id: str
    memory_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class OxigraphParityGap:
    graph_id: str
    node_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    outbox_applied_count: int
    damaged_nodes: tuple[DamagedMemoryNode, ...]
    oxigraph_gaps: tuple[OxigraphParityGap, ...] = ()


@dataclass(slots=True)
class PostgresMemoryReconciler:
    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None
    oxigraph: OxigraphGraphStore | None = None

    def __post_init__(self) -> None:
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError(
                "PostgresMemoryReconciler requires exactly one verified provider or transaction connection"
            )

    def reconcile(
        self,
        *,
        graph_id: str | None = None,
        outbox_limit: int = 100,
    ) -> ReconciliationReport:
        applied = self.replay_outbox(graph_id=graph_id, limit=outbox_limit)
        damaged = self.find_damaged_nodes(graph_id=graph_id)
        oxigraph_gaps = self.find_oxigraph_gaps(graph_id=graph_id)
        return ReconciliationReport(
            outbox_applied_count=applied,
            damaged_nodes=damaged,
            oxigraph_gaps=oxigraph_gaps,
        )

    def replay_outbox(self, *, graph_id: str | None = None, limit: int = 100) -> int:
        sync = (
            MemorySearchIndexSync(connection=self.connection)
            if self.connection is not None
            else MemorySearchIndexSync(connection_provider=self.connection_provider)
        )
        applied = sync.consume_outbox(graph_id=graph_id, limit=limit)
        if self.oxigraph is not None:
            materializer = (
                OxigraphMaterializer(oxigraph=self.oxigraph, connection=self.connection)
                if self.connection is not None
                else OxigraphMaterializer(
                    oxigraph=self.oxigraph,
                    connection_provider=self.connection_provider,
                )
            )
            materializer.consume_outbox(graph_id=graph_id, limit=limit)
        return applied

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
                SELECT node.graph_id, node.id, outbox.payload
                FROM memory_nodes AS node
                LEFT JOIN memory_write_outbox AS outbox
                  ON outbox.graph_id = node.graph_id
                 AND (
                      outbox.dirty_memory_ids @> to_jsonb(ARRAY[node.id]::text[])
                   OR outbox.payload->'dirty_memory_ids' @> to_jsonb(ARRAY[node.id]::text[])
                   OR outbox.payload->'decision_record'->'write_outcome'->>'memory_id' = node.id
                   )
                {where_sql}
                ORDER BY node.graph_id ASC, node.id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
            damaged: list[DamagedMemoryNode] = []
            for row in rows:
                payload = row["payload"]
                if payload is None:
                    damaged.append(
                        DamagedMemoryNode(
                            graph_id=row["graph_id"],
                            memory_id=row["id"],
                            reason="missing_governance_outbox",
                        )
                    )
                    continue
                if row["id"] not in _outbox_memory_ids(payload):
                    damaged.append(
                        DamagedMemoryNode(
                            graph_id=row["graph_id"],
                            memory_id=row["id"],
                            reason="missing_governance_outbox",
                        )
                    )
            return tuple(damaged)

    def find_oxigraph_gaps(
        self,
        *,
        graph_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OxigraphParityGap, ...]:
        if self.oxigraph is None:
            return ()
        where = []
        params: list[object] = []
        if graph_id is not None:
            where.append("graph_id = %s")
            params.append(_graph_key(graph_id))
        params.append(limit)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._cursor() as cursor:
            cursor.execute(
                f"""
                SELECT graph_id, id
                FROM graph_documents
                {where_sql}
                ORDER BY graph_id ASC, id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
        postgres_by_graph: dict[str, set[str]] = {}
        for row in rows:
            postgres_by_graph.setdefault(row[0], set()).add(row[1])
        if graph_id is not None and not postgres_by_graph:
            postgres_by_graph[_graph_key(graph_id)] = set()
        gaps: list[OxigraphParityGap] = []
        for graph, postgres_ids in postgres_by_graph.items():
            oxigraph_ids: set[str] = set()
            try:
                rows = self.oxigraph.query(
                    f"""
SELECT DISTINCT ?s WHERE {{
  GRAPH {_iri_token(_expand_graph_id(graph, self.oxigraph.default_context))} {{
    ?s ?p ?o .
  }}
}}
"""
                )
                for row in rows:
                    subject = row.get("s")
                    if isinstance(subject, dict):
                        node_id = subject.get("@id")
                        if isinstance(node_id, str):
                            oxigraph_ids.add(node_id)
                missing_in_oxigraph = sorted(postgres_ids - oxigraph_ids)
                stale_in_oxigraph = sorted(oxigraph_ids - postgres_ids)
                for node_id in missing_in_oxigraph[:limit]:
                    gaps.append(
                        OxigraphParityGap(
                            graph_id=graph,
                            node_id=node_id,
                            reason="missing_in_oxigraph",
                        )
                    )
                remaining = max(0, limit - len(gaps))
                for node_id in stale_in_oxigraph[:remaining]:
                    gaps.append(
                        OxigraphParityGap(
                            graph_id=graph,
                            node_id=node_id,
                            reason="stale_in_oxigraph",
                        )
                    )
            except Exception:
                gaps.append(
                    OxigraphParityGap(
                        graph_id=graph,
                        node_id="*",
                        reason="oxigraph_unavailable",
                    )
                )
                break
        return tuple(gaps)

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
