"""Bounded, read-only doctor for optional context-input audit artifacts."""

from __future__ import annotations

from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import ContextCompiledEvent, EventType
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.event_log.historical_decoder import (
    decode_raw_stored_event_envelope,
)
from pulsara_agent.runtime.context_input.replay import (
    AuditIntegrityFailure,
    AuditUnavailable,
    ContextInputReplayError,
    ExactAuditArtifact,
    ReconstructedAudit,
    load_context_input_audit,
)


class ContextInputAuditDoctorEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    event_sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    resolved_model_call_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason_code: str | None = None
    exact_root_artifact_id: str | None = None
    reconstructed_component_kinds: tuple[str, ...] = ()
    omitted_component_kinds: tuple[str, ...] = ()


class ContextInputAuditDoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    require_exact_audit: bool
    catalog_event_count: int = Field(ge=0)
    catalog_payload_bytes: int = Field(ge=0)
    exact_count: int = Field(ge=0)
    reconstructed_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    integrity_failure_count: int = Field(ge=0)
    entries: tuple[ContextInputAuditDoctorEntry, ...]
    continuation_through_sequence: int | None = Field(default=None, ge=0)


def inspect_context_input_audits(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    artifact_store,
    require_exact_audit: bool = False,
    through_sequence: int | None = None,
    max_events: int = 10_000,
    max_payload_bytes: int = 256 * 1024 * 1024,
    operation_timeout_seconds: float = 120.0,
) -> ContextInputAuditDoctorReport:
    """Resolve each compact compile through exact or canonical bounded paths."""

    if not runtime_session_id:
        raise ValueError("context-input audit doctor requires a session ID")
    if max_events < 1 or max_payload_bytes < 1 or operation_timeout_seconds <= 0:
        raise ValueError("context-input audit doctor bounds must be positive")
    deadline = monotonic() + operation_timeout_seconds
    cursor = through_sequence
    catalog_count = 0
    catalog_bytes = 0
    entries: list[ContextInputAuditDoctorEntry] = []
    continuation: int | None = None

    while monotonic() < deadline and catalog_count < max_events:
        page_limit = min(32, max_events - catalog_count)
        rows = event_log.read_raw_events_by_type(
            EventType.CONTEXT_COMPILED.value,
            limit=page_limit,
            through_sequence=cursor,
            deadline_monotonic=deadline,
        )
        if not rows:
            break
        page_bytes = sum(len(row.canonical_payload_bytes) for row in rows)
        if page_bytes > 8 * 1024 * 1024 or any(
            len(row.canonical_payload_bytes) > 256 * 1024 for row in rows
        ):
            raise RuntimeError("context-input audit doctor catalog page is oversized")
        if catalog_bytes + page_bytes > max_payload_bytes:
            continuation = rows[0].sequence
            break
        catalog_count += len(rows)
        catalog_bytes += page_bytes
        for raw in rows:
            event = decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(event, ContextCompiledEvent):
                raise RuntimeError("context-input audit doctor catalog type mismatch")
            if event.status != "compiled" or event.semantic_commit is None:
                continue
            commit = event.semantic_commit
            if commit.runtime_session_id != runtime_session_id:
                raise RuntimeError("context-input audit doctor owner mismatch")
            outcome = load_context_input_audit(
                event=event,
                event_log=event_log,
                provider_input_store=None,
                artifact_store=artifact_store,
                require_exact=False,
                deadline_monotonic=deadline,
            )
            if isinstance(outcome, ExactAuditArtifact):
                reason = None
                root_id = event.audit_expectation.expected_root_artifact_id
                reconstructed = ()
                omitted = ()
            elif isinstance(outcome, ReconstructedAudit):
                reason = outcome.artifact_diagnostic_code
                root_id = None
                reconstructed = outcome.reconstructed_component_kinds
                omitted = outcome.omitted_component_kinds
            else:
                assert isinstance(outcome, (AuditUnavailable, AuditIntegrityFailure))
                reason = outcome.reason
                root_id = None
                reconstructed = ()
                omitted = ()
            if require_exact_audit and not isinstance(outcome, ExactAuditArtifact):
                raise ContextInputReplayError(outcome.status, reason or "not_exact")
            entries.append(
                ContextInputAuditDoctorEntry(
                    event_id=event.id,
                    event_sequence=raw.sequence,
                    run_id=event.run_id,
                    context_id=event.context_id,
                    resolved_model_call_id=commit.resolved_model_call_id,
                    status=outcome.status.value,
                    reason_code=reason,
                    exact_root_artifact_id=root_id,
                    reconstructed_component_kinds=reconstructed,
                    omitted_component_kinds=omitted,
                )
            )
        cursor = rows[-1].sequence - 1
        if cursor < 1:
            break
    if monotonic() >= deadline and continuation is None:
        continuation = cursor
    if catalog_count >= max_events and cursor is not None and cursor >= 1:
        continuation = cursor

    return ContextInputAuditDoctorReport(
        runtime_session_id=runtime_session_id,
        require_exact_audit=require_exact_audit,
        catalog_event_count=catalog_count,
        catalog_payload_bytes=catalog_bytes,
        exact_count=sum(item.status == "exact_audit" for item in entries),
        reconstructed_count=sum(
            item.status == "reconstructed_audit" for item in entries
        ),
        unavailable_count=sum(item.status == "audit_unavailable" for item in entries),
        integrity_failure_count=sum(
            item.status == "audit_integrity_failure" for item in entries
        ),
        entries=tuple(entries),
        continuation_through_sequence=continuation,
    )


__all__ = [
    "ContextInputAuditDoctorEntry",
    "ContextInputAuditDoctorReport",
    "inspect_context_input_audits",
]
