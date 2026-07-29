"""Storage boundary protocols for memory runtime services."""

from __future__ import annotations

from typing import Any, Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.memory.foundation.records import (
    ArtifactPutConfirmation,
    ArtifactRecord,
    ArtifactTextSlice,
    ArtifactWriteResult,
)


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
    ) -> ArtifactPutConfirmation: ...

    def get_info(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> ArtifactRecord: ...

    def read_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        offset_chars: int = 0,
        max_chars: int = 20_000,
    ) -> ArtifactTextSlice: ...

    def get_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> str: ...

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


class MemoryModelRuntimeGateway(Protocol):
    """Structural authority used by background memory model owners."""

    runtime_session_id: str
    provider_input_generation_coordinator: Any
    model_stream_execution_registry: Any
    transcript_projection_state_store: Any
    context_input_io_service: Any

    async def write_events(
        self,
        events: tuple[AgentEvent, ...],
        **kwargs: Any,
    ) -> Any: ...

    def latch_memory_governance_reconciliation_required(self) -> None: ...


class GovernanceTranscriptAuthority(Protocol):
    reducer_evidence_snapshot: Any
    document_view: Any
    ledger_through_sequence: int
    ledger_continuity_accumulator: str
    transcript_semantic_event_count: int
    transcript_semantic_accumulator: str
