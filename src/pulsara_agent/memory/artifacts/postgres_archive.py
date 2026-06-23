"""PostgreSQL-backed ArtifactStore implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.memory.foundation.records import ArtifactRecord, ArtifactTextSlice, ArtifactWriteResult


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
                    text_content=content,
                    binary_content=None,
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

    def put_bytes(
        self,
        blob_id: str,
        content: bytes,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        media_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactWriteResult:
        if run_id is not None and session_id is None:
            raise ValueError("PostgresArtifactStore.put_bytes requires session_id when run_id is provided")

        digest = "sha256:" + hashlib.sha256(content).hexdigest()
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
                        binary_body,
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
                        len(content),
                        stored_at,
                        Jsonb(metadata or {}),
                    ),
                )
                row = self._artifact_row(cursor, blob_id)
                self._validate_artifact_identity(
                    row,
                    blob_id=blob_id,
                    text_content=None,
                    binary_content=content,
                    digest=digest,
                    size_bytes=len(content),
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

    def get_info(self, blob_id: str, *, session_id: str | None = None) -> ArtifactRecord:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                return ArtifactRecord(
                    id=row["id"],
                    media_type=row["media_type"],
                    digest=row["digest"],
                    size_bytes=row["size_bytes"],
                    stored_at=row["stored_at"],
                    created_at=row["created_at"].isoformat() if row["created_at"] is not None else None,
                    metadata=dict(row["metadata"] or {}),
                )

    def read_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        offset_chars: int = 0,
        max_chars: int = 20_000,
    ) -> ArtifactTextSlice:
        if offset_chars < 0:
            raise ValueError("offset_chars must be >= 0")
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                text_body = row["text_body"]
                if text_body is None:
                    raise ValueError(f"Artifact {blob_id!r} is not a text artifact")
                total_chars = len(text_body)
                sliced = text_body[offset_chars : offset_chars + max_chars]
                return ArtifactTextSlice(
                    artifact=ArtifactRecord(
                        id=row["id"],
                        media_type=row["media_type"],
                        digest=row["digest"],
                        size_bytes=row["size_bytes"],
                        stored_at=row["stored_at"],
                        created_at=row["created_at"].isoformat() if row["created_at"] is not None else None,
                        metadata=dict(row["metadata"] or {}),
                    ),
                    text=sliced,
                    offset_chars=offset_chars,
                    returned_chars=len(sliced),
                    total_chars=total_chars,
                    has_more=offset_chars + len(sliced) < total_chars,
                )

    def get_text(self, blob_id: str, *, session_id: str | None = None) -> str:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                text_body = row["text_body"]
                if text_body is None:
                    raise ValueError(f"Artifact {blob_id!r} is not a text artifact")
                return text_body

    def get_bytes(self, blob_id: str, *, session_id: str | None = None) -> bytes:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                binary_body = row["binary_body"]
                if binary_body is None:
                    raise ValueError(f"Artifact {blob_id!r} is not a binary artifact")
                return bytes(binary_body)

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
            select
                id,
                session_id,
                run_id,
                media_type,
                text_body,
                binary_body,
                digest,
                size_bytes,
                stored_at,
                created_at,
                metadata
            from artifacts
            where id = %s
            """,
            (blob_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(blob_id)
        return row

    def _validate_read_owner(self, row: dict[str, Any], *, session_id: str | None) -> None:
        if session_id is not None and row["session_id"] != session_id:
            raise KeyError(row["id"])

    def _validate_artifact_identity(
        self,
        row: dict[str, Any],
        *,
        blob_id: str,
        text_content: str | None,
        binary_content: bytes | None,
        digest: str,
        size_bytes: int,
        media_type: str,
        session_id: str | None,
        run_id: str | None,
    ) -> None:
        if (
            row["digest"] != digest
            or row["text_body"] != text_content
            or (row["binary_body"] is not None and bytes(row["binary_body"]) != binary_content)
            or (row["binary_body"] is None and binary_content is not None)
            or row["size_bytes"] != size_bytes
        ):
            raise ValueError(f"artifact {blob_id!r} already exists with different content")
        if row["media_type"] != media_type:
            raise ValueError(f"artifact {blob_id!r} already exists with media_type {row['media_type']!r}")
        if session_id is not None and row["session_id"] != session_id:
            raise ValueError(f"artifact {blob_id!r} already belongs to runtime session {row['session_id']!r}")
        if run_id is not None and row["run_id"] != run_id:
            raise ValueError(f"artifact {blob_id!r} already belongs to run {row['run_id']!r}")
