"""Deterministic public run output materialized only from committed facts."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

import time
from dataclasses import dataclass
from hashlib import sha256

from pulsara_agent.event import (
    EventType,
    ModelCallEndEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ports.run_execution import RunFinalOutputView, RunOwnerIdentity
from pulsara_agent.ports.run_terminalization import (
    RunFinalOutputMaterializationFull,
    RunFinalOutputMaterializationOutcome,
    RunFinalOutputMaterializationOwnerIdentity,
    RunFinalOutputMaterializationReconciliationRequired,
    RunFinalOutputMaterializationRetryableUnavailable,
    TerminalRunReceipt,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.model_call import ModelTokenUsageFact
from pulsara_agent.primitives.terminal_projection import (
    ModelTerminalProjectionPayloadFact,
    ModelTextBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalInlineContentFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    CanonicalRunFinalAssistantProjection,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.context_input.io_service import (
    ContextInputIoDeadlineExceeded,
    ContextInputIoService,
)


RUN_FINAL_OUTPUT_MATERIALIZER_CONTRACT_FINGERPRINT = context_fingerprint(
    "run-final-output-materializer-contract:v4",
    {
        "horizon": "matching_run_end_sequence",
        "message_authority": (
            "canonical_transcript_checkpoint_plus_bounded_delta_terminal_projection"
        ),
        "usage_authority": "streaming_bounded_paged_model_call_end_reported_usage",
        "tool_count_authority": "streaming_bounded_paged_tool_result_end_count",
        "final_text_policy": "latest_text_only_assistant_without_tool_call",
        "physical_io_owner": "runtime_session_context_input_io_service",
        "resident_state": "constant_scalar_usage_plus_one_projection",
    },
)

_LIFECYCLE_PAGE_SEQUENCE_SPAN = 512
_LIFECYCLE_PAGE_MAX_BYTES = 8 * 1024 * 1024
_FINAL_OUTPUT_USAGE_EVENT_TYPES = (
    EventType.MODEL_CALL_END.value,
    EventType.TOOL_RESULT_END.value,
)


@dataclass(frozen=True, slots=True)
class RunFinalOutputMaterializer:
    event_log: EventLog
    runtime_session_id: str
    io_service: ContextInputIoService
    transcript_projection: TranscriptProjectionStateStore
    archive: ArtifactStore

    async def materialize(
        self,
        *,
        owner_identity: RunOwnerIdentity,
        run_end_event_reference: ContextEventReferenceFact,
        deadline_monotonic: float,
    ) -> RunFinalOutputMaterializationOutcome:
        owner = _materialization_owner(
            owner_identity=owner_identity,
            run_end_event_reference=run_end_event_reference,
        )
        if time.monotonic() >= deadline_monotonic:
            return RunFinalOutputMaterializationRetryableUnavailable(
                owner=owner,
                diagnostic_code="deadline_exceeded",
            )
        try:
            return await self.io_service.execute(
                operation_name=f"run-final-output:{owner_identity.run_id}",
                operation=lambda: self._materialize_bounded(
                    owner_identity=owner_identity,
                    run_end_event_reference=run_end_event_reference,
                    owner=owner,
                    deadline_monotonic=deadline_monotonic,
                ),
                deadline_monotonic=deadline_monotonic,
            )
        except (TimeoutError, ContextInputIoDeadlineExceeded):
            return RunFinalOutputMaterializationRetryableUnavailable(
                owner=owner,
                diagnostic_code="deadline_exceeded",
            )
        except Exception:
            return RunFinalOutputMaterializationRetryableUnavailable(
                owner=owner,
                diagnostic_code="source_temporarily_unavailable",
            )

    def _materialize_bounded(
        self,
        *,
        owner_identity: RunOwnerIdentity,
        run_end_event_reference: ContextEventReferenceFact,
        owner: RunFinalOutputMaterializationOwnerIdentity,
        deadline_monotonic: float,
    ) -> RunFinalOutputMaterializationOutcome:
        exact_raw = self.event_log.read_raw_events_by_id(
            (
                owner_identity.run_start_event_id,
                run_end_event_reference.event_id,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        exact_events = tuple(
            decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
            for raw in exact_raw
        )
        starts = tuple(
            event for event in exact_events if isinstance(event, RunStartEvent)
        )
        run_ends = tuple(
            event for event in exact_events if isinstance(event, RunEndEvent)
        )
        if len(run_ends) != 1:
            return _reconciliation(
                owner,
                "run_end_authority_conflict",
                events=run_ends,
                runtime_session_id=self.runtime_session_id,
            )
        run_end = run_ends[0]
        if (
            run_end.id != run_end_event_reference.event_id
            or run_end.sequence != run_end_event_reference.sequence
            or run_end.run_id != owner_identity.run_id
            or event_reference_from_stored(
                run_end,
                runtime_session_id=self.runtime_session_id,
            )
            != run_end_event_reference
        ):
            return _reconciliation(
                owner,
                "run_end_authority_conflict",
                events=run_ends,
                runtime_session_id=self.runtime_session_id,
            )
        if (
            len(starts) != 1
            or starts[0].id != owner_identity.run_start_event_id
            or starts[0].sequence != owner_identity.run_start_sequence
            or starts[0].run_id != owner_identity.run_id
        ):
            return _reconciliation(
                owner,
                "transcript_authority_conflict",
                events=starts,
                runtime_session_id=self.runtime_session_id,
            )

        try:
            final_projection = self.transcript_projection.final_assistant_projection(
                run_id=owner_identity.run_id,
                through_sequence=run_end_event_reference.sequence,
            )
            final_text = self._read_final_projection_text(
                projection=final_projection,
                deadline_monotonic=deadline_monotonic,
            )
            usage, tool_call_count = self._fold_terminal_pages(
                owner_identity=owner_identity,
                through_sequence=run_end_event_reference.sequence,
                deadline_monotonic=deadline_monotonic,
            )
        except (KeyError, ValueError):
            return _reconciliation(
                owner,
                "transcript_authority_conflict",
                events=(*starts, *run_ends),
                runtime_session_id=self.runtime_session_id,
            )

        references = (
            event_reference_from_stored(
                starts[0],
                runtime_session_id=self.runtime_session_id,
            ),
            *(final_projection.entry.source_event_refs if final_projection else ()),
        )
        return _full_outcome(
            owner=owner,
            owner_identity=owner_identity,
            run_end_event_reference=run_end_event_reference,
            run_end=run_end,
            final_text=final_text,
            references=references,
            usage=usage,
            tool_call_count=tool_call_count,
        )

    def _fold_terminal_pages(
        self,
        *,
        owner_identity: RunOwnerIdentity,
        through_sequence: int,
        deadline_monotonic: float,
    ) -> tuple[ModelTokenUsageFact, int]:
        input_tokens = 0
        cached_input_tokens = 0
        output_tokens = 0
        reasoning_output_tokens = 0
        tool_call_ids: set[str] = set()
        minimum = owner_identity.run_start_sequence
        while minimum <= through_sequence:
            page_through = min(
                through_sequence,
                minimum + _LIFECYCLE_PAGE_SEQUENCE_SPAN - 1,
            )
            snapshot = self.event_log.read_raw_events_by_types(
                _FINAL_OUTPUT_USAGE_EVENT_TYPES,
                run_ids=(owner_identity.run_id,),
                minimum_sequence=minimum,
                through_sequence=page_through,
                max_events=_LIFECYCLE_PAGE_SEQUENCE_SPAN,
                max_payload_bytes=_LIFECYCLE_PAGE_MAX_BYTES,
                deadline_monotonic=deadline_monotonic,
            )
            for raw in snapshot.events:
                event = decode_raw_stored_event_envelope(
                    raw, DEFAULT_EVENT_SCHEMA_REGISTRY
                )
                if isinstance(event, ToolResultEndEvent):
                    tool_call_ids.add(event.tool_call_id)
                    continue
                if not isinstance(event, ModelCallEndEvent):
                    raise ValueError("terminal page contains an unsupported event")
                if event.usage is None:
                    continue
                input_tokens += event.usage.input_tokens
                cached_input_tokens += event.usage.cached_input_tokens or 0
                output_tokens += event.usage.output_tokens
                reasoning_output_tokens += event.usage.reasoning_output_tokens or 0
            minimum = page_through + 1
        return (
            ModelTokenUsageFact(
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            len(tool_call_ids),
        )

    def _read_final_projection_text(
        self,
        *,
        projection: CanonicalRunFinalAssistantProjection | None,
        deadline_monotonic: float,
    ) -> str:
        if projection is None:
            return ""
        payload = projection.document.payload
        if not isinstance(payload, ModelTerminalProjectionPayloadFact):
            raise ValueError("final assistant projection payload kind drifted")
        selected_orders = frozenset(projection.entry.content.selected_projection_orders)
        text: list[str] = []
        for item in payload.items:
            semantic = item.semantic_identity
            if semantic.projection_order not in selected_orders or not isinstance(
                semantic, ModelTextBlockSemanticFact
            ):
                continue
            content = item.content
            if isinstance(content, TerminalInlineContentFact):
                text.append(content.text)
                continue
            if not isinstance(content, TerminalArtifactContentReferenceFact):
                raise ValueError("final text projection lacks terminal content")
            hydrated = self.archive.get_text(
                content.artifact_id,
                session_id=self.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            encoded = hydrated.encode("utf-8")
            if (
                len(encoded) != content.artifact_bytes
                or f"sha256:{sha256(encoded).hexdigest()}" != content.artifact_sha256
            ):
                raise ValueError("final text artifact authority mismatch")
            text.append(hydrated)
        return "\n".join(text)


def _full_outcome(
    *,
    owner: RunFinalOutputMaterializationOwnerIdentity,
    owner_identity: RunOwnerIdentity,
    run_end_event_reference: ContextEventReferenceFact,
    run_end: RunEndEvent,
    final_text: str,
    references: tuple[ContextEventReferenceFact, ...],
    usage: ModelTokenUsageFact,
    tool_call_count: int,
) -> RunFinalOutputMaterializationFull:
    view_payload = {
        "schema_version": 1,
        "status": run_end.status,
        "stop_reason": run_end.stop_reason,
        "final_text": final_text or None,
        "ordered_message_references": references,
        "usage": usage,
        "tool_call_count": tool_call_count,
    }
    view = RunFinalOutputView(
        **view_payload,
        output_fingerprint=_model_fingerprint(
            RunFinalOutputView,
            domain="run-final-output-view:v1",
            payload=view_payload,
            fingerprint_field="output_fingerprint",
        ),
    )
    finalization_receipt_fingerprint = context_fingerprint(
        "run-finalization-receipt:v1",
        {
            "owner_fingerprint": owner.owner_fingerprint,
            "run_end_event_reference": run_end_event_reference.model_dump(mode="json"),
            "output_fingerprint": view.output_fingerprint,
        },
    )
    receipt_payload = {
        "schema_version": 1,
        "owner_identity": owner_identity,
        "run_end_event_reference": run_end_event_reference,
        "finalization_receipt_fingerprint": finalization_receipt_fingerprint,
        "output": view,
    }
    receipt = TerminalRunReceipt(
        **receipt_payload,
        receipt_fingerprint=_model_fingerprint(
            TerminalRunReceipt,
            domain="terminal-run-receipt:v1",
            payload=receipt_payload,
            fingerprint_field="receipt_fingerprint",
        ),
    )
    return RunFinalOutputMaterializationFull(owner=owner, receipt=receipt)


def _materialization_owner(
    *,
    owner_identity: RunOwnerIdentity,
    run_end_event_reference: ContextEventReferenceFact,
) -> RunFinalOutputMaterializationOwnerIdentity:
    payload = {
        "schema_version": 1,
        "owner_identity": owner_identity,
        "run_end_event_reference": run_end_event_reference,
        "materializer_contract_fingerprint": (
            RUN_FINAL_OUTPUT_MATERIALIZER_CONTRACT_FINGERPRINT
        ),
    }
    return RunFinalOutputMaterializationOwnerIdentity(
        **payload,
        owner_fingerprint=_model_fingerprint(
            RunFinalOutputMaterializationOwnerIdentity,
            domain="run-final-output-materialization-owner:v1",
            payload=payload,
            fingerprint_field="owner_fingerprint",
        ),
    )


def _model_fingerprint(
    model_type,
    *,
    domain: str,
    payload: dict[str, object],
    fingerprint_field: str,
) -> str:
    provisional = model_type.model_construct(
        **payload,
        **{fingerprint_field: "sha256:" + "0" * 64},
    )
    return context_fingerprint(
        domain,
        provisional.model_dump(mode="json", exclude={fingerprint_field}),
    )


def _reconciliation(
    owner: RunFinalOutputMaterializationOwnerIdentity,
    diagnostic_code: str,
    *,
    events: tuple[object, ...],
    runtime_session_id: str,
) -> RunFinalOutputMaterializationReconciliationRequired:
    refs = tuple(
        event_reference_from_stored(event, runtime_session_id=runtime_session_id)
        for event in events
        if getattr(event, "sequence", None) is not None
    )
    return RunFinalOutputMaterializationReconciliationRequired(
        owner=owner,
        diagnostic_code=diagnostic_code,
        conflicting_event_references=refs,
    )


__all__ = [
    "RUN_FINAL_OUTPUT_MATERIALIZER_CONTRACT_FINGERPRINT",
    "RunFinalOutputMaterializer",
]
