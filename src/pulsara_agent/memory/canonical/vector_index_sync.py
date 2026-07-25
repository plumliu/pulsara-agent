"""Deterministic pgvector projection for one canonical memory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.memory.canonical.embedded_text import (
    EmbeddedMemoryText,
    build_embedded_memory_text,
)
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class VectorSyncStatus(StrEnum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    DELETED = "deleted"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class VectorSyncResult:
    memory_id: str
    status: VectorSyncStatus
    embedded_text_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    graph_id: str
    memory_id: str
    embedded: EmbeddedMemoryText


@dataclass(slots=True)
class MemoryVectorIndexSync:
    connection_provider: VerifiedPostgresConnectionProviderProtocol
    provider: EmbeddingProvider
    provider_name: str = "openai_compatible"

    def __post_init__(self) -> None:
        if self.provider.dimensions != 1024:
            raise ValueError(
                f"memory_vector_index requires 1024 dimensions, got {self.provider.dimensions}"
            )

    def _connection(self, *, row_factory: object | None = None):
        return self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )

    @property
    def embedding_fingerprint(self) -> str:
        return (
            f"{self.provider_name}:{self.provider.model_id}:{self.provider.dimensions}"
        )

    async def sync_memory(
        self,
        memory_id: str,
        *,
        graph_id: str | None = None,
    ) -> VectorSyncResult:
        graph = _graph_key(graph_id)
        snapshot = await asyncio.to_thread(self._load_snapshot, graph, memory_id)
        if snapshot is None:
            await asyncio.to_thread(self._delete_vector, graph, memory_id)
            return VectorSyncResult(
                memory_id=memory_id, status=VectorSyncStatus.DELETED
            )
        if await asyncio.to_thread(self._hash_is_current, snapshot):
            return VectorSyncResult(
                memory_id=memory_id,
                status=VectorSyncStatus.SKIPPED,
                embedded_text_hash=snapshot.embedded.text_hash,
            )
        vector = await self.provider.embed(snapshot.embedded.text)
        if len(vector) != self.provider.dimensions:
            raise ValueError(
                f"Embedding dimension mismatch: expected {self.provider.dimensions}, got {len(vector)}"
            )
        applied = await asyncio.to_thread(self._finalize_snapshot, snapshot, vector)
        return VectorSyncResult(
            memory_id=memory_id,
            status=VectorSyncStatus.APPLIED if applied else VectorSyncStatus.STALE,
            embedded_text_hash=snapshot.embedded.text_hash,
        )

    async def sync_memory_inline(
        self,
        memory_id: str,
        *,
        graph_id: str | None = None,
    ) -> VectorSyncResult:
        """Run DB phases in the current projection-maintenance worker."""

        graph = _graph_key(graph_id)
        snapshot = self._load_snapshot(graph, memory_id)
        if snapshot is None:
            self._delete_vector(graph, memory_id)
            return VectorSyncResult(
                memory_id=memory_id,
                status=VectorSyncStatus.DELETED,
            )
        if self._hash_is_current(snapshot):
            return VectorSyncResult(
                memory_id=memory_id,
                status=VectorSyncStatus.SKIPPED,
                embedded_text_hash=snapshot.embedded.text_hash,
            )
        vector = await self.provider.embed(snapshot.embedded.text)
        if len(vector) != self.provider.dimensions:
            raise ValueError(
                f"Embedding dimension mismatch: expected {self.provider.dimensions}, got {len(vector)}"
            )
        applied = self._finalize_snapshot(snapshot, vector)
        return VectorSyncResult(
            memory_id=memory_id,
            status=(VectorSyncStatus.APPLIED if applied else VectorSyncStatus.STALE),
            embedded_text_hash=snapshot.embedded.text_hash,
        )

    async def rebuild(
        self, *, graph_id: str | None = None
    ) -> tuple[VectorSyncResult, ...]:
        graph = _graph_key(graph_id)
        memory_ids = await asyncio.to_thread(self._list_memory_ids, graph)
        return tuple(
            await asyncio.gather(
                *(
                    self.sync_memory(memory_id, graph_id=graph)
                    for memory_id in memory_ids
                )
            )
        )

    def _load_snapshot(self, graph_id: str, memory_id: str) -> _Snapshot | None:
        with self._connection(row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT node.*, doc.payload
                    FROM memory_nodes AS node
                    JOIN graph_documents AS doc
                      ON doc.graph_id = node.graph_id AND doc.id = node.id
                    WHERE node.graph_id = %s AND node.id = %s
                    """,
                    (graph_id, memory_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return _Snapshot(
            graph_id=graph_id,
            memory_id=memory_id,
            embedded=build_embedded_memory_text(row, document=row["payload"]),
        )

    def _hash_is_current(self, snapshot: _Snapshot) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT embedded_text_hash
                    FROM memory_vector_index
                    WHERE graph_id = %s AND memory_id = %s AND embedding_fingerprint = %s
                    """,
                    (snapshot.graph_id, snapshot.memory_id, self.embedding_fingerprint),
                )
                row = cursor.fetchone()
        return row is not None and row[0] == snapshot.embedded.text_hash

    def _finalize_snapshot(self, snapshot: _Snapshot, vector: list[float]) -> bool:
        with self._connection(row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT node.*, doc.payload
                    FROM memory_nodes AS node
                    JOIN graph_documents AS doc
                      ON doc.graph_id = node.graph_id AND doc.id = node.id
                    WHERE node.graph_id = %s AND node.id = %s
                    FOR UPDATE OF node
                    """,
                    (snapshot.graph_id, snapshot.memory_id),
                )
                current = cursor.fetchone()
                if current is None:
                    return False
                current_text = build_embedded_memory_text(
                    current, document=current["payload"]
                )
                if current_text.text_hash != snapshot.embedded.text_hash:
                    return False
                cursor.execute(
                    """
                    INSERT INTO memory_vector_index (
                        graph_id, memory_id, embedding_fingerprint,
                        embedded_text_hash, builder_version, embedding, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s::vector, now())
                    ON CONFLICT (graph_id, memory_id, embedding_fingerprint) DO UPDATE SET
                        embedded_text_hash = EXCLUDED.embedded_text_hash,
                        builder_version = EXCLUDED.builder_version,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    (
                        snapshot.graph_id,
                        snapshot.memory_id,
                        self.embedding_fingerprint,
                        snapshot.embedded.text_hash,
                        snapshot.embedded.builder_version,
                        _vector_literal(vector),
                    ),
                )
        return True

    def _delete_vector(self, graph_id: str, memory_id: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM memory_vector_index WHERE graph_id = %s AND memory_id = %s",
                    (graph_id, memory_id),
                )

    def _list_memory_ids(self, graph_id: str) -> tuple[str, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM memory_nodes WHERE graph_id = %s ORDER BY id",
                    (graph_id,),
                )
                return tuple(row[0] for row in cursor.fetchall())


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
