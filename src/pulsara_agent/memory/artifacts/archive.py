"""ArchiveStore implementations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pulsara_agent.memory.foundation.records import ArtifactRecord, ArtifactTextSlice, ArtifactWriteResult


@dataclass(slots=True)
class ArchiveBlob:
    id: str
    text_content: str | None
    binary_content: bytes | None
    digest: str
    stored_at: str
    media_type: str
    size_bytes: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    run_id: str | None = None


@dataclass(slots=True)
class InMemoryArchiveStore:
    blobs: dict[str, ArchiveBlob] = field(default_factory=dict)

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
        encoded = content.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        stored_at = f"archive://{blob_id}"
        blob = ArchiveBlob(
            id=blob_id,
            text_content=content,
            binary_content=None,
            digest=digest,
            stored_at=stored_at,
            media_type=media_type,
            size_bytes=len(encoded),
            created_at=_utc_now(),
            metadata=dict(metadata or {}),
            session_id=session_id,
            run_id=run_id,
        )
        existing = self.blobs.get(blob_id)
        if existing is not None:
            _validate_identity(
                existing,
                digest=digest,
                size_bytes=len(encoded),
                media_type=media_type,
                session_id=session_id,
                run_id=run_id,
                text_content=content,
                binary_content=None,
            )
            blob = existing
        else:
            self.blobs[blob_id] = blob
        return ArtifactWriteResult(
            id=blob_id,
            digest=digest,
            stored_at=stored_at,
            size_bytes=len(encoded),
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
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        stored_at = f"archive://{blob_id}"
        blob = ArchiveBlob(
            id=blob_id,
            text_content=None,
            binary_content=content,
            digest=digest,
            stored_at=stored_at,
            media_type=media_type,
            size_bytes=len(content),
            created_at=_utc_now(),
            metadata=dict(metadata or {}),
            session_id=session_id,
            run_id=run_id,
        )
        existing = self.blobs.get(blob_id)
        if existing is not None:
            _validate_identity(
                existing,
                digest=digest,
                size_bytes=len(content),
                media_type=media_type,
                session_id=session_id,
                run_id=run_id,
                text_content=None,
                binary_content=content,
            )
            blob = existing
        else:
            self.blobs[blob_id] = blob
        return ArtifactWriteResult(
            id=blob_id,
            digest=digest,
            stored_at=stored_at,
            size_bytes=len(content),
        )

    def get_info(self, blob_id: str, *, session_id: str | None = None) -> ArtifactRecord:
        blob = self._blob(blob_id, session_id=session_id)
        return _record(blob)

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
        text = self.get_text(blob_id, session_id=session_id)
        total_chars = len(text)
        sliced = text[offset_chars : offset_chars + max_chars]
        return ArtifactTextSlice(
            artifact=self.get_info(blob_id, session_id=session_id),
            text=sliced,
            offset_chars=offset_chars,
            returned_chars=len(sliced),
            total_chars=total_chars,
            has_more=offset_chars + len(sliced) < total_chars,
        )

    def get_text(self, blob_id: str, *, session_id: str | None = None) -> str:
        blob = self._blob(blob_id, session_id=session_id)
        if blob.text_content is None:
            raise ValueError(f"Artifact {blob_id!r} is not a text artifact")
        return blob.text_content

    def get_bytes(self, blob_id: str, *, session_id: str | None = None) -> bytes:
        blob = self._blob(blob_id, session_id=session_id)
        if blob.binary_content is None:
            raise ValueError(f"Artifact {blob_id!r} is not a binary artifact")
        return blob.binary_content

    def _blob(self, blob_id: str, *, session_id: str | None) -> ArchiveBlob:
        blob = self.blobs[blob_id]
        if session_id is not None and blob.session_id != session_id:
            raise KeyError(blob_id)
        return blob


def _record(blob: ArchiveBlob) -> ArtifactRecord:
    return ArtifactRecord(
        id=blob.id,
        media_type=blob.media_type,
        digest=blob.digest,
        size_bytes=blob.size_bytes,
        stored_at=blob.stored_at,
        created_at=blob.created_at,
        metadata=dict(blob.metadata),
    )


def _validate_identity(
    blob: ArchiveBlob,
    *,
    digest: str,
    size_bytes: int,
    media_type: str,
    session_id: str | None,
    run_id: str | None,
    text_content: str | None,
    binary_content: bytes | None,
) -> None:
    if (
        blob.digest != digest
        or blob.size_bytes != size_bytes
        or blob.text_content != text_content
        or blob.binary_content != binary_content
    ):
        raise ValueError(f"artifact {blob.id!r} already exists with different content")
    if blob.media_type != media_type:
        raise ValueError(f"artifact {blob.id!r} already exists with media_type {blob.media_type!r}")
    if session_id is not None and blob.session_id != session_id:
        raise ValueError(f"artifact {blob.id!r} already belongs to runtime session {blob.session_id!r}")
    if run_id is not None and blob.run_id != run_id:
        raise ValueError(f"artifact {blob.id!r} already belongs to run {blob.run_id!r}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
