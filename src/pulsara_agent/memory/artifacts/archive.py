"""ArchiveStore implementations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.memory.foundation.records import ArtifactWriteResult


@dataclass(slots=True)
class ArchiveBlob:
    id: str
    content: str
    digest: str
    stored_at: str


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
        digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored_at = f"archive://{blob_id}"
        blob = ArchiveBlob(
            id=blob_id,
            content=content,
            digest=digest,
            stored_at=stored_at,
        )
        self.blobs[blob_id] = blob
        return ArtifactWriteResult(
            id=blob_id,
            digest=digest,
            stored_at=stored_at,
            size_bytes=len(content.encode("utf-8")),
        )

    def get_text(self, blob_id: str) -> str:
        return self.blobs[blob_id].content
