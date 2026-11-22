"""Disposable MCP tool vectors indexed by the exact encoding input."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from math import hypot, isfinite

from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.retrieval.embedding.validation import freeze_v1_embedding_vector
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


def mcp_tool_embedding_cache_key(connection: RetrievalConnection, text: str) -> bytes:
    space = connection.embedding_contract
    return sha256(
        canonical_json_bytes(
            [space.endpoint, space.model, space.shape, space.dimensions, text]
        )
    ).digest()


def _unit_vector(values: Sequence[float]) -> list[float]:
    vector = freeze_v1_embedding_vector(values)
    # pgvector uses float32; normalize before casting so a valid direction
    # survives both its component conversion and cosine norm calculation.
    norm = hypot(*vector)
    return [value / norm for value in vector]


class PostgresMcpToolEmbeddingCache:
    def __init__(self, provider: VerifiedPostgresConnectionProviderProtocol) -> None:
        self._provider = provider

    def lookup(
        self,
        keys: Sequence[bytes],
        *,
        deadline_monotonic: float,
        lane: PostgresConnectionLane = PostgresConnectionLane.HOST_CONTROL,
    ) -> frozenset[bytes]:
        if not keys:
            return frozenset()
        with self._provider.connection(
            lane=lane,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT cache_key FROM pulsara_v3.mcp_tool_embeddings
                WHERE cache_key = ANY(%s::bytea[])
                """,
                (list(dict.fromkeys(keys)),),
            ).fetchall()
        return frozenset(key for (key,) in rows)

    def rank(
        self,
        keys: Sequence[bytes],
        query_vector: Sequence[float],
        *,
        deadline_monotonic: float,
    ) -> dict[bytes, float]:
        if not keys:
            return {}
        vector = _unit_vector(query_vector)
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT cached.cache_key,
                    1 - (cached.embedding <=> %s::double precision[]::public.vector) AS similarity
                FROM unnest(%s::bytea[]) WITH ORDINALITY AS visible(cache_key, ordinal)
                JOIN pulsara_v3.mcp_tool_embeddings AS cached USING (cache_key)
                ORDER BY similarity DESC, visible.ordinal
                """,
                (vector, list(dict.fromkeys(keys))),
            ).fetchall()
        return {key: similarity for key, similarity in rows if isfinite(similarity)}

    def insert(
        self, rows: Mapping[bytes, Sequence[float]], *, deadline_monotonic: float
    ) -> None:
        if not rows:
            return
        # Identical key order prevents competing batches from taking locks in reverse order.
        vectors = [(key, _unit_vector(vector)) for key, vector in sorted(rows.items())]
        with (
            self._provider.connection(
                lane=PostgresConnectionLane.BACKGROUND_WORK,
                deadline_monotonic=deadline_monotonic,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.executemany(
                """
                INSERT INTO pulsara_v3.mcp_tool_embeddings (cache_key, embedding)
                VALUES (%s, %s::double precision[]::public.vector)
                ON CONFLICT (cache_key) DO NOTHING
                """,
                vectors,
            )
