"""PostgreSQL-backed ArtifactStore implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.memory.foundation.records import ArtifactWriteResult


@dataclass(slots=True)
class PostgresArtifactStore:
    dsn: str

    def put_text(
        self,
        blob_id: str,
        content: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        media_type: str = "text/plain",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactWriteResult:
        if run_id is not None and session_id is None:
            raise ValueError("PostgresArtifactStore.put_text requires session_id when run_id is provided")

        encoded = content.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        stored_at = f"postgres://artifacts/{blob_id}"

        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                self._lock_artifact(cursor, blob_id)
                self._validate_owner(cursor, session_id=session_id, run_id=run_id)
                cursor.execute(
                    """
                    insert into artifacts (
                        id,
                        session_id,
                        run_id,
                        media_type,
                        text_body,
                        digest,
                        size_bytes,
                        stored_at,
                        metadata
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do nothing
                    """,
                    (
                        blob_id,
                        session_id,
                        run_id,
                        media_type,
                        content,
                        digest,
                        len(encoded),
                        stored_at,
                        Jsonb(metadata or {}),
                    ),
                )
                row = self._artifact_row(cursor, blob_id)
                self._validate_artifact_identity(
                    row,
                    blob_id=blob_id,
                    content=content,
                    digest=digest,
                    size_bytes=len(encoded),
                    media_type=media_type,
                    session_id=session_id,
                    run_id=run_id,
                )
                return ArtifactWriteResult(
                    id=row["id"],
                    digest=row["digest"],
                    stored_at=row["stored_at"],
                    size_bytes=row["size_bytes"],
                )

    def get_text(self, blob_id: str) -> str:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                row = self._artifact_row(cursor, blob_id)
                text_body = row["text_body"]
                if text_body is None:
                    raise ValueError(f"Artifact {blob_id!r} is not a text artifact")
                return text_body

    def _lock_artifact(self, cursor, blob_id: str) -> None:
        cursor.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"artifact:{blob_id}",))

    def _validate_owner(self, cursor, *, session_id: str | None, run_id: str | None) -> None:
        if session_id is None:
            return

        cursor.execute("select id from sessions where id = %s", (session_id,))
        if cursor.fetchone() is None:
            raise ValueError(f"session_id {session_id!r} does not exist")

        if run_id is None:
            return

        cursor.execute("select session_id from runs where id = %s", (run_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"run_id {run_id!r} does not exist")
        if row["session_id"] != session_id:
            raise ValueError(f"run_id {run_id!r} already belongs to runtime session {row['session_id']!r}")

    def _artifact_row(self, cursor, blob_id: str) -> dict[str, Any]:
        cursor.execute(
            """
            select id, session_id, run_id, media_type, text_body, digest, size_bytes, stored_at
            from artifacts
            where id = %s
            """,
            (blob_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(blob_id)
        return row

    def _validate_artifact_identity(
        self,
        row: dict[str, Any],
        *,
        blob_id: str,
        content: str,
        digest: str,
        size_bytes: int,
        media_type: str,
        session_id: str | None,
        run_id: str | None,
    ) -> None:
        if row["digest"] != digest or row["text_body"] != content or row["size_bytes"] != size_bytes:
            raise ValueError(f"artifact {blob_id!r} already exists with different content")
        if row["media_type"] != media_type:
            raise ValueError(f"artifact {blob_id!r} already exists with media_type {row['media_type']!r}")
        if session_id is not None and row["session_id"] != session_id:
            raise ValueError(f"artifact {blob_id!r} already belongs to runtime session {row['session_id']!r}")
        if run_id is not None and row["run_id"] != run_id:
            raise ValueError(f"artifact {blob_id!r} already belongs to run {row['run_id']!r}")
