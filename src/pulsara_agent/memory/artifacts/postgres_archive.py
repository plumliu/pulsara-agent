"""PostgreSQL-backed ArtifactStore implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from math import ceil
from time import monotonic
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.memory.artifacts.archive import canonical_artifact_semantic_metadata
from pulsara_agent.memory.foundation.records import (
    ArtifactContentConflict,
    ArtifactPutConfirmation,
    ArtifactRecord,
    ArtifactTextSlice,
    ArtifactWriteResult,
)


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
            raise ValueError(
                "PostgresArtifactStore.put_text requires session_id when run_id is provided"
            )

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
            raise ValueError(
                "PostgresArtifactStore.put_bytes requires session_id when run_id is provided"
            )

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

    def put_text_if_absent_or_confirm_identical(
        self,
        blob_id: str,
        content: str,
        *,
        session_id: str | None,
        run_id: str | None,
        media_type: str,
        semantic_metadata: dict[str, Any],
        deadline_monotonic: float | None = None,
    ) -> ArtifactPutConfirmation:
        if run_id is not None and session_id is None:
            raise ValueError(
                "PostgresArtifactStore deterministic put requires session_id "
                "when run_id is provided"
            )
        metadata = canonical_artifact_semantic_metadata(semantic_metadata)
        encoded = content.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        stored_at = f"postgres://artifacts/{blob_id}"
        with self._connect(deadline_monotonic) as connection:
            with connection.cursor() as cursor:
                self._apply_statement_deadline(cursor, deadline_monotonic)
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
                        Jsonb(metadata),
                    ),
                )
                inserted = cursor.rowcount == 1
                row = self._artifact_row(cursor, blob_id)
                self._validate_deterministic_text_row(
                    row,
                    blob_id=blob_id,
                    content=content,
                    digest=digest,
                    size_bytes=len(encoded),
                    media_type=media_type,
                    session_id=session_id,
                    run_id=run_id,
                    semantic_metadata=metadata,
                )
                return ArtifactPutConfirmation(
                    status="inserted" if inserted else "confirmed_identical",
                    result=ArtifactWriteResult(
                        id=row["id"],
                        digest=row["digest"],
                        stored_at=row["stored_at"],
                        size_bytes=row["size_bytes"],
                    ),
                )

    def get_info(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> ArtifactRecord:
        with self._connect(deadline_monotonic) as connection:
            with connection.cursor() as cursor:
                self._apply_statement_deadline(cursor, deadline_monotonic)
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                return ArtifactRecord(
                    id=row["id"],
                    media_type=row["media_type"],
                    digest=row["digest"],
                    size_bytes=row["size_bytes"],
                    stored_at=row["stored_at"],
                    created_at=row["created_at"].isoformat()
                    if row["created_at"] is not None
                    else None,
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
                        created_at=row["created_at"].isoformat()
                        if row["created_at"] is not None
                        else None,
                        metadata=dict(row["metadata"] or {}),
                    ),
                    text=sliced,
                    offset_chars=offset_chars,
                    returned_chars=len(sliced),
                    total_chars=total_chars,
                    has_more=offset_chars + len(sliced) < total_chars,
                )

    def get_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> str:
        with self._connect(deadline_monotonic) as connection:
            with connection.cursor() as cursor:
                self._apply_statement_deadline(cursor, deadline_monotonic)
                row = self._artifact_row(cursor, blob_id)
                self._validate_read_owner(row, session_id=session_id)
                text_body = row["text_body"]
                if text_body is None:
                    raise ValueError(f"Artifact {blob_id!r} is not a text artifact")
                return text_body

    def _connect(self, deadline_monotonic: float | None):
        if deadline_monotonic is None:
            return psycopg.connect(self.dsn, row_factory=dict_row)
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("artifact database deadline exceeded before connect")
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            connect_timeout=max(1, ceil(remaining)),
        )

    @staticmethod
    def _apply_statement_deadline(cursor, deadline_monotonic: float | None) -> None:
        if deadline_monotonic is None:
            return
        remaining_ms = int((deadline_monotonic - monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("artifact database deadline exceeded before statement")
        cursor.execute(
            "select set_config('statement_timeout', %s, true)",
            (f"{remaining_ms}ms",),
        )

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
        cursor.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"artifact:{blob_id}",),
        )

    def _validate_owner(
        self, cursor, *, session_id: str | None, run_id: str | None
    ) -> None:
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
            raise ValueError(
                f"run_id {run_id!r} already belongs to runtime session {row['session_id']!r}"
            )

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

    def _validate_read_owner(
        self, row: dict[str, Any], *, session_id: str | None
    ) -> None:
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
            or (
                row["binary_body"] is not None
                and bytes(row["binary_body"]) != binary_content
            )
            or (row["binary_body"] is None and binary_content is not None)
            or row["size_bytes"] != size_bytes
        ):
            raise ValueError(
                f"artifact {blob_id!r} already exists with different content"
            )
        if row["media_type"] != media_type:
            raise ValueError(
                f"artifact {blob_id!r} already exists with media_type {row['media_type']!r}"
            )
        if session_id is not None and row["session_id"] != session_id:
            raise ValueError(
                f"artifact {blob_id!r} already belongs to runtime session {row['session_id']!r}"
            )
        if run_id is not None and row["run_id"] != run_id:
            raise ValueError(
                f"artifact {blob_id!r} already belongs to run {row['run_id']!r}"
            )

    def _validate_deterministic_text_row(
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
        semantic_metadata: dict[str, Any],
    ) -> None:
        conflicts: list[str] = []
        if (
            row["text_body"] != content
            or row["binary_body"] is not None
            or row["digest"] != digest
            or row["size_bytes"] != size_bytes
        ):
            conflicts.append("content")
        if row["media_type"] != media_type:
            conflicts.append("media_type")
        if row["session_id"] != session_id or row["run_id"] != run_id:
            conflicts.append("ownership")
        existing_metadata = canonical_artifact_semantic_metadata(
            dict(row["metadata"] or {})
        )
        if existing_metadata != semantic_metadata:
            conflicts.append("semantic_metadata")
        if conflicts:
            raise ArtifactContentConflict(
                f"artifact {blob_id!r} deterministic identity conflict: "
                f"{','.join(conflicts)}"
            )
