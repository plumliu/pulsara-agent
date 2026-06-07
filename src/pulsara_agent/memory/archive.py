"""ArchiveStore implementations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(slots=True)
class ArchiveBlob:
    id: str
    content: str
    digest: str


@dataclass(slots=True)
class InMemoryArchiveStore:
    blobs: dict[str, ArchiveBlob] = field(default_factory=dict)

    def put_text(self, blob_id: str, content: str) -> ArchiveBlob:
        digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        blob = ArchiveBlob(id=blob_id, content=content, digest=digest)
        self.blobs[blob_id] = blob
        return blob

    def get_text(self, blob_id: str) -> str:
        return self.blobs[blob_id].content
