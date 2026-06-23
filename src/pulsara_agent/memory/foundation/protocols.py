"""Storage boundary protocols for memory runtime services."""

from __future__ import annotations

from typing import Any, Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.memory.foundation.records import ArtifactRecord, ArtifactTextSlice, ArtifactWriteResult


class ArtifactStore(Protocol):
    """Runtime artifact persistence boundary."""

    def put_text(
        self,
        blob_id: str,
        content: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        media_type: str = "text/plain",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactWriteResult: ...

    def put_bytes(
        self,
        blob_id: str,
        content: bytes,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        media_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactWriteResult: ...

    def get_info(self, blob_id: str, *, session_id: str | None = None) -> ArtifactRecord: ...

    def read_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        offset_chars: int = 0,
        max_chars: int = 20_000,
    ) -> ArtifactTextSlice: ...

    def get_text(self, blob_id: str, *, session_id: str | None = None) -> str: ...

    def get_bytes(self, blob_id: str, *, session_id: str | None = None) -> bytes: ...


class RuntimeEventReadStore(Protocol):
    """Read-only runtime event access needed by memory ingestion."""

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
    ) -> list[AgentEvent]: ...

    def replay(self, reply_id: str) -> Any: ...
