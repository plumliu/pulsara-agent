"""Filtered cosine nearest-neighbour queries over memory_vector_index."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Sequence

from psycopg.rows import dict_row

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.ontology import memory
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class MemoryVectorQuery:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def candidates(
        self,
        *,
        query_vector: Sequence[float],
        embedding_fingerprint: str,
        scopes: Sequence[str] | None,
        types: Sequence[str] | None,
        limit: int,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        if limit <= 0:
            return []
        where = [
            "vector.graph_id = %s",
            "vector.embedding_fingerprint = %s",
            "node.status = %s",
        ]
        params: list[object] = [
            _graph_key(graph_id),
            embedding_fingerprint,
            memory.NodeStatus.ACTIVE.value,
        ]
        if scopes:
            where.append("node.scope = ANY(%s)")
            params.append(list(scopes))
        if types:
            where.append("node.memory_type = ANY(%s)")
            params.append(list(types))
        vector_literal = "[" + ",".join(format(float(value), ".9g") for value in query_vector) + "]"
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT vector.memory_id,
                           1.0 - (vector.embedding <=> %s::vector) AS score
                    FROM memory_vector_index AS vector
                    JOIN memory_nodes AS node
                      ON node.graph_id = vector.graph_id AND node.id = vector.memory_id
                    WHERE {' AND '.join(where)}
                    ORDER BY vector.embedding <=> %s::vector, vector.memory_id
                    LIMIT %s
                    """,
                    (vector_literal, *params, vector_literal, max(limit * 4, limit)),
                )
                rows = cursor.fetchall()
        return [(row["memory_id"], float(row["score"])) for row in rows[:limit]]
