"""Purpose-neutral immutable blob publication for the Stage 2 kernel."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import (
    BlobContent,
    CanonicalContent,
    InlineContent,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


MAXIMUM_BLOB_BYTES = STAGE2_LIMITS.canonical_blob_hard_bytes
MAXIMUM_CONTENT_CHUNK_BYTES = STAGE2_LIMITS.content_chunk_hard_bytes


def _blob_id(workspace_id: str, digest: str) -> str:
    identity = sha256(f"{workspace_id}\0{digest}".encode()).hexdigest()
    return f"blob:{identity}"


@dataclass(frozen=True, slots=True)
class CanonicalContentChunk:
    blob_id: str
    offset: int
    content: bytes
    total_size: int
    digest: str
    has_more: bool


class PostgresCanonicalBlobStore:
    def __init__(
        self, connection_provider: VerifiedPostgresConnectionProviderProtocol
    ) -> None:
        self._provider = connection_provider

    def publish(
        self,
        *,
        workspace_id: str,
        content: bytes,
        media_type: str,
        codec: str,
        deadline_monotonic: float,
    ) -> BlobContent:
        if not workspace_id or not media_type or not codec:
            raise ValueError("blob publication identity is incomplete")
        value = bytes(content)
        if len(value) > MAXIMUM_BLOB_BYTES:
            raise ValueError("blob exceeds the Stage 2 physical bound")
        digest = "sha256:" + sha256(value).hexdigest()
        blob_id = _blob_id(workspace_id, digest)
        storage_identity = f"postgres:pulsara_v3.blobs/{blob_id}"
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            connection.execute(
                """
                INSERT INTO pulsara_v3.blobs (
                    id, workspace_id, storage_identity, logical_digest,
                    logical_size, media_type, codec, body
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    blob_id,
                    workspace_id,
                    storage_identity,
                    digest,
                    len(value),
                    media_type,
                    codec,
                    value,
                ),
            )
            row = connection.execute(
                """
                SELECT workspace_id, storage_identity, logical_digest,
                       logical_size, media_type, codec, body
                FROM pulsara_v3.blobs WHERE id = %s
                """,
                (blob_id,),
            ).fetchone()
            if row is None or (
                row["workspace_id"] != workspace_id
                or row["storage_identity"] != storage_identity
                or row["logical_digest"] != digest
                or int(row["logical_size"]) != len(value)
                or row["media_type"] != media_type
                or row["codec"] != codec
                or bytes(row["body"]) != value
            ):
                raise ConversationKernelConflict("blob identity conflict")
        return BlobContent(blob_id, digest, len(value), media_type, codec)

    def read_exact(
        self,
        *,
        blob_id: str,
        expected_digest: str,
        expected_size: int,
        deadline_monotonic: float,
    ) -> bytes:
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT logical_digest, logical_size, body
                FROM pulsara_v3.blobs WHERE id = %s
                """,
                (blob_id,),
            ).fetchone()
        if row is None:
            raise KeyError(blob_id)
        content = bytes(row["body"])
        if (
            row["logical_digest"] != expected_digest
            or int(row["logical_size"]) != expected_size
            or len(content) != expected_size
            or "sha256:" + sha256(content).hexdigest() != expected_digest
        ):
            raise ConversationKernelConflict("blob content integrity mismatch")
        return content

    def read_chunk(
        self,
        *,
        blob_id: str,
        expected_digest: str,
        expected_size: int,
        expected_media_type: str,
        expected_codec: str,
        offset: int,
        maximum_bytes: int,
        deadline_monotonic: float,
    ) -> CanonicalContentChunk:
        if offset < 0 or not 1 <= maximum_bytes <= MAXIMUM_CONTENT_CHUNK_BYTES:
            raise ValueError("blob content range is out of bounds")
        if not 0 <= expected_size <= MAXIMUM_BLOB_BYTES or offset > expected_size:
            raise ValueError("blob descriptor range is out of bounds")
        requested_bytes = min(maximum_bytes, expected_size - offset)
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT logical_digest, logical_size, media_type, codec,
                       substring(body FROM %s FOR %s) AS content
                FROM pulsara_v3.blobs WHERE id = %s
                """,
                (offset + 1, requested_bytes, blob_id),
            ).fetchone()
        if row is None:
            raise KeyError(blob_id)
        total_size = int(row["logical_size"])
        content = bytes(row["content"])
        if (
            row["logical_digest"] != expected_digest
            or total_size != expected_size
            or str(row["media_type"]) != expected_media_type
            or str(row["codec"]) != expected_codec
            or len(content) != requested_bytes
        ):
            raise ConversationKernelConflict("blob descriptor integrity mismatch")
        return CanonicalContentChunk(
            blob_id=blob_id,
            offset=offset,
            content=content,
            total_size=total_size,
            digest=expected_digest,
            has_more=offset + len(content) < total_size,
        )

    def delete_orphans(
        self,
        *,
        grace_seconds: int = STAGE2_LIMITS.blob_orphan_grace_seconds,
        maximum_items: int = STAGE2_LIMITS.blob_gc_batch_hard_items,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        """Delete one bounded batch of unreferenced immutable blobs.

        Canonical rows hold restrictive foreign keys.  The candidate query and
        delete execute as one statement so a caller can never treat a stale
        pre-scan as deletion authority.  A transaction-scoped advisory lock
        makes concurrent Host scanners skip an already-owned candidate without
        granting the immutable blob table the otherwise-unneeded UPDATE
        privilege required by ``SELECT ... FOR UPDATE``.
        """

        if not 1 <= grace_seconds <= STAGE2_LIMITS.blob_orphan_grace_seconds:
            raise ValueError("blob orphan grace is outside the frozen contract")
        if not 1 <= maximum_items <= STAGE2_LIMITS.blob_gc_batch_hard_items:
            raise ValueError("blob GC batch is outside the frozen contract")
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT b.id
                    FROM pulsara_v3.blobs AS b
                    WHERE b.created_at <= clock_timestamp()
                        - make_interval(secs => %s)
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.context_snapshots AS s
                          WHERE s.blob_id = b.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.transcript_entries AS e
                          WHERE e.blob_id = b.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.assistant_message_blocks AS m
                          WHERE m.blob_id = b.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.prompt_queue_items AS q
                          WHERE q.blob_id = b.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.durable_jobs AS j
                          WHERE j.result_blob_id = b.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.tool_results AS r
                          WHERE r.output_artifact_blob_id = b.id
                      )
                      AND pg_catalog.pg_try_advisory_xact_lock(
                          pg_catalog.hashtextextended(b.id, 0)
                      )
                    ORDER BY b.created_at, b.id
                    LIMIT %s
                )
                DELETE FROM pulsara_v3.blobs AS b
                USING candidates AS c
                WHERE b.id = c.id
                RETURNING b.id
                """,
                (grace_seconds, maximum_items),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)


class CanonicalContentPublisher:
    """The sole inline/blob selection owner for canonical product facts."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        inline_threshold_bytes: int = 64 << 10,
    ) -> None:
        if not 1 <= inline_threshold_bytes <= 64 << 10:
            raise ValueError("inline threshold is outside the Stage 2 contract")
        self._provider = connection_provider
        self._store = PostgresCanonicalBlobStore(connection_provider)
        self._inline_threshold_bytes = inline_threshold_bytes

    def materialize(
        self,
        *,
        session_id: str,
        content: bytes,
        media_type: str,
        codec: str,
        deadline_monotonic: float,
    ) -> CanonicalContent:
        value = bytes(content)
        if len(value) <= self._inline_threshold_bytes:
            return InlineContent.from_bytes(value, media_type=media_type, codec=codec)
        workspace_id = self._workspace_id(
            session_id=session_id, deadline_monotonic=deadline_monotonic
        )
        return self._store.publish(
            workspace_id=workspace_id,
            content=value,
            media_type=media_type,
            codec=codec,
            deadline_monotonic=deadline_monotonic,
        )

    def _workspace_id(self, *, session_id: str, deadline_monotonic: float) -> str:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                "SELECT workspace_id FROM pulsara_v3.sessions WHERE id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return str(row["workspace_id"])


__all__ = [
    "CanonicalContentChunk",
    "CanonicalContentPublisher",
    "MAXIMUM_BLOB_BYTES",
    "MAXIMUM_CONTENT_CHUNK_BYTES",
    "PostgresCanonicalBlobStore",
]
