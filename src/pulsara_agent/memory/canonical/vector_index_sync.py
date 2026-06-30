"""Asynchronous pgvector projection driven by the canonical mutation outbox."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import graph_key as _graph_key
from pulsara_agent.memory.canonical.embedded_text import EmbeddedMemoryText, build_embedded_memory_text
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    mark_surface_applied,
    mark_surface_failed,
    parse_mutation_payload,
)
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


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


@dataclass(frozen=True, slots=True)
class _ClaimedOutbox:
    outbox_id: str
    graph_id: str
    claim_token: str
    payload: dict[str, Any]


@dataclass(slots=True)
class MemoryVectorIndexSync:
    dsn: str
    provider: EmbeddingProvider
    provider_name: str = "openai_compatible"
    claim_ttl_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.provider.dimensions != 1024:
            raise ValueError(
                f"memory_vector_index requires 1024 dimensions, got {self.provider.dimensions}"
            )
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    @property
    def embedding_fingerprint(self) -> str:
        return f"{self.provider_name}:{self.provider.model_id}:{self.provider.dimensions}"

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
            return VectorSyncResult(memory_id=memory_id, status=VectorSyncStatus.DELETED)
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

    async def rebuild(self, *, graph_id: str | None = None) -> tuple[VectorSyncResult, ...]:
        graph = _graph_key(graph_id)
        memory_ids = await asyncio.to_thread(self._list_memory_ids, graph)
        return tuple(
            await asyncio.gather(
                *(self.sync_memory(memory_id, graph_id=graph) for memory_id in memory_ids)
            )
        )

    async def consume_outbox(
        self,
        *,
        limit: int = 100,
        graph_id: str | None = None,
    ) -> int:
        claims = await asyncio.to_thread(self._claim_outbox, limit, graph_id)
        applied = 0
        for claim in claims:
            try:
                payload = parse_mutation_payload(claim.payload)
                for memory_id in payload.dirty_memory_ids:
                    result = await self.sync_memory(memory_id, graph_id=claim.graph_id)
                    if result.status is VectorSyncStatus.STALE:
                        await asyncio.to_thread(self._release_claim, claim)
                        break
                else:
                    await asyncio.to_thread(self._mark_claim_applied, claim)
                    applied += 1
            except Exception as exc:
                await asyncio.to_thread(self._mark_claim_failed, claim, exc)
        return applied

    def _load_snapshot(self, graph_id: str, memory_id: str) -> _Snapshot | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
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
        with psycopg.connect(self.dsn) as connection:
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
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
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
                current_text = build_embedded_memory_text(current, document=current["payload"])
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
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM memory_vector_index WHERE graph_id = %s AND memory_id = %s",
                    (graph_id, memory_id),
                )

    def _list_memory_ids(self, graph_id: str) -> tuple[str, ...]:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM memory_nodes WHERE graph_id = %s ORDER BY id",
                    (graph_id,),
                )
                return tuple(row[0] for row in cursor.fetchall())

    def _claim_outbox(self, limit: int, graph_id: str | None) -> tuple[_ClaimedOutbox, ...]:
        where = [
            "status IN ('pending', 'partial', 'failed')",
            "payload->'surface_apply_status'->>'vector_index' IN ('pending', 'failed')",
            "(vector_claimed_until IS NULL OR vector_claimed_until < now())",
        ]
        params: list[object] = []
        if graph_id is not None:
            where.append("graph_id = %s")
            params.append(_graph_key(graph_id))
        params.append(limit)
        claims: list[_ClaimedOutbox] = []
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT outbox_id, graph_id, payload
                    FROM memory_write_outbox
                    WHERE {' AND '.join(where)}
                    ORDER BY sequence_key, created_at, outbox_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    tuple(params),
                )
                for row in cursor.fetchall():
                    token = f"vector-claim:{uuid4().hex}"
                    cursor.execute(
                        """
                        UPDATE memory_write_outbox
                        SET vector_claim_token = %s,
                            vector_claimed_until = now() + %s * interval '1 second'
                        WHERE outbox_id = %s
                        """,
                        (token, self.claim_ttl_seconds, row["outbox_id"]),
                    )
                    claims.append(
                        _ClaimedOutbox(
                            outbox_id=row["outbox_id"],
                            graph_id=row["graph_id"],
                            claim_token=token,
                            payload=row["payload"],
                        )
                    )
        return tuple(claims)

    def _mark_claim_applied(self, claim: _ClaimedOutbox) -> None:
        self._finish_claim(claim, applied=True, error=None)

    def _mark_claim_failed(self, claim: _ClaimedOutbox, exc: Exception) -> None:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self._finish_claim(claim, applied=False, error=error)

    def _finish_claim(
        self,
        claim: _ClaimedOutbox,
        *,
        applied: bool,
        error: str | None,
    ) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM memory_write_outbox
                    WHERE outbox_id = %s AND vector_claim_token = %s
                    FOR UPDATE
                    """,
                    (claim.outbox_id, claim.claim_token),
                )
                row = cursor.fetchone()
                if row is None:
                    return
                current = parse_mutation_payload(row["payload"])
                if applied:
                    payload, status = mark_surface_applied(
                        current, CanonicalMutationSurface.VECTOR_INDEX.value
                    )
                else:
                    payload, status = mark_surface_failed(
                        current, CanonicalMutationSurface.VECTOR_INDEX.value
                    )
                cursor.execute(
                    """
                    UPDATE memory_write_outbox
                    SET payload = %s,
                        status = %s,
                        attempt_count = attempt_count + 1,
                        last_error = %s,
                        applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END,
                        vector_claim_token = NULL,
                        vector_claimed_until = NULL
                    WHERE outbox_id = %s AND vector_claim_token = %s
                    """,
                    (Jsonb(payload), status, error, status, claim.outbox_id, claim.claim_token),
                )

    def _release_claim(self, claim: _ClaimedOutbox) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memory_write_outbox
                    SET vector_claim_token = NULL, vector_claimed_until = NULL
                    WHERE outbox_id = %s AND vector_claim_token = %s
                    """,
                    (claim.outbox_id, claim.claim_token),
                )


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
