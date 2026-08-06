"""Pure context snapshot construction and authority-window finalization."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import model_validator

from pulsara_agent.event.events import (
    ContextCompactionCompletedEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowOpenedEvent,
    RunStartEvent,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.context import (
    ContextAuthoritySlicePlan,
    ContextCandidateSourceSelectionFact,
    ContextCompilePolicyFact,
    ContextCompileTimingFact,
    ContextContinuationReferenceFact,
    ContextEventRangeFact,
    ContextEventReferenceFact,
    ContextFactSnapshotFact,
    ContextInputIdentityFact,
    LongHorizonContextAttributionFact,
    ContextMaterializedToolSpecInput,
    ContextPlanSnapshotFact,
    ContextProjectionReferenceFact,
    ContextRunEntryReferenceFact,
    ContextRuntimeEnvironmentFact,
    ContextSectionCandidate,
    ContextStaticInstructionFact,
    ContextToolSpecFact,
    FrozenContextFact,
    RunPermissionSnapshotFact,
    TranscriptProjectionWindowFact,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    CapabilityToolCatalogRootFact,
    ContextSourceDispositionFact,
    ResolvedContextSourcePhysicalInputPolicyFact,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.long_horizon import (
    SubagentGraphAccelerationFact,
    SubagentGraphSemanticSourceFact,
)
from pulsara_agent.primitives.run_entry import CurrentUserMessageFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    ContextEventSliceError,
    FrozenStoredEvent,
)

if TYPE_CHECKING:
    pass


class ContextCandidateSelectionMismatch(RuntimeError):
    """The snapshot selection differs from a valid ledger-derived selection."""


class ContextSnapshotBuildInput(FrozenContextFact):
    identity: ContextInputIdentityFact
    run_entry: ContextRunEntryReferenceFact
    continuation: ContextContinuationReferenceFact | None
    continuation_refs: tuple[ContextEventReferenceFact, ...]
    current_user_message: CurrentUserMessageFact
    permission_snapshot: RunPermissionSnapshotFact
    resolved_model_call: ResolvedModelCallFact
    capability_snapshot: CapabilityExposureSnapshotFact
    plan_snapshot: ContextPlanSnapshotFact
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str
    static_instructions: tuple[ContextStaticInstructionFact, ...]
    runtime_environment: ContextRuntimeEnvironmentFact
    compile_policy: ContextCompilePolicyFact
    tool_specs: tuple[ContextToolSpecFact, ...]
    projections: tuple[ContextProjectionReferenceFact, ...]
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    subagent_graph_acceleration: SubagentGraphAccelerationFact
    candidate_source_selections: tuple[ContextCandidateSourceSelectionFact, ...]
    context_source_candidates: tuple[ContextSectionCandidate, ...]
    context_source_dispositions: tuple[ContextSourceDispositionFact, ...]
    capability_tool_catalog_root: CapabilityToolCatalogRootFact
    context_source_physical_input_policy: ResolvedContextSourcePhysicalInputPolicyFact
    context_source_registry_fingerprint: str
    timing: ContextCompileTimingFact
    authority_slice_plan: ContextAuthoritySlicePlan
    primary_event_range: ContextEventRangeFact
    named_event_ranges: tuple[ContextEventRangeFact, ...]

    @model_validator(mode="after")
    def _input(self) -> "ContextSnapshotBuildInput":
        if (
            self.identity.source_through_sequence
            != self.primary_event_range.through_sequence
        ):
            raise ValueError("snapshot build input high-water mismatch")
        if (
            self.authority_slice_plan.through_sequence
            != self.primary_event_range.through_sequence
        ):
            raise ValueError("snapshot build input authority mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ContextFactSnapshot:
    fact: ContextFactSnapshotFact
    resolved_call: ResolvedModelCall
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...]

    def __post_init__(self) -> None:
        if self.resolved_call.fact != self.fact.resolved_model_call:
            raise ValueError("context invocation resolved call mismatch")
        facts = tuple(item.fact for item in self.materialized_tool_specs)
        if facts != self.fact.tool_specs:
            raise ValueError("context invocation tool specs mismatch")


@dataclass(frozen=True, slots=True)
class ContextFactSnapshotDraft:
    """Non-durable compile carrier used before final budget attribution exists."""

    fact: ContextSnapshotBuildInput
    resolved_call: ResolvedModelCall
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...]

    def __post_init__(self) -> None:
        if self.resolved_call.fact != self.fact.resolved_model_call:
            raise ValueError("context draft resolved call mismatch")
        facts = tuple(item.fact for item in self.materialized_tool_specs)
        if facts != self.fact.tool_specs:
            raise ValueError("context draft tool specs mismatch")


def finalize_context_authority_slice_plan(
    *,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    required_local_event_refs: tuple[ContextEventReferenceFact, ...],
    run_start_ref: ContextEventReferenceFact,
    latest_compaction_terminal_ref: ContextEventReferenceFact | None,
    prior_transcript_through_sequence: int | None = None,
) -> ContextAuthoritySlicePlan:
    run_start_stored = event_slice.event_by_id(run_start_ref.event_id)
    run_start = decode_raw_stored_event_envelope(
        run_start_stored, DEFAULT_EVENT_SCHEMA_REGISTRY
    )
    if not isinstance(run_start, RunStartEvent):
        raise ContextEventSliceError("run-start reference does not decode to RunStart")
    window_lifecycle = _active_window_compaction_lifecycle(
        event_slice=event_slice,
        run_id=run_start.run_id,
    )
    lifecycle_refs: tuple[ContextEventReferenceFact, ...] = ()
    if window_lifecycle is not None:
        open_stored, _opened, started_stored, _started, completed_stored, _completed = (
            window_lifecycle
        )
        lifecycle_refs = tuple(
            stored.to_reference(event_slice.runtime_session_id)
            for stored in (open_stored, started_stored, completed_stored)
        )
    refs = tuple(
        sorted(
            {
                ref.event_id: ref
                for ref in (
                    *required_local_event_refs,
                    run_start_ref,
                    *lifecycle_refs,
                )
            }.values(),
            key=lambda item: item.sequence,
        )
    )
    for ref in refs:
        stored = event_slice.event_by_id(ref.event_id)
        if (
            stored.sequence,
            stored.event_type,
            stored.payload_fingerprint,
        ) != (ref.sequence, ref.event_type, ref.payload_fingerprint):
            raise ContextEventSliceError(
                "required event reference does not match slice"
            )

    if prior_transcript_through_sequence is None:
        prior_transcript_through_sequence = run_start_ref.sequence - 1
    if prior_transcript_through_sequence < 0:
        raise ContextEventSliceError("prior transcript high-water cannot be negative")
    if prior_transcript_through_sequence >= run_start_ref.sequence:
        raise ContextEventSliceError(
            "prior transcript high-water must precede current RunStart"
        )

    retained_from: int | None
    retained_through: int | None
    if window_lifecycle is not None:
        (
            _open_stored,
            opened,
            started_stored,
            started,
            completed_stored,
            completed,
        ) = window_lifecycle
        plan = started.plan
        retained_from = None
        retained_through = None
        window_payload = {
            "window_kind": "window_compaction",
            "compaction_terminal_ref": completed_stored.to_reference(
                event_slice.runtime_session_id
            ),
            "compaction_summary_artifact_id": completed.summary_artifact_id,
            "compacted_through_sequence": plan.source_through_sequence,
            "keep_after_sequence": None,
            "window_compaction_started_ref": started_stored.to_reference(
                event_slice.runtime_session_id
            ),
            "window_compaction_source_document_artifact_id": (
                plan.source_document_artifact_id
            ),
            "window_compaction_source_document_fingerprint": (
                plan.source_document_fingerprint
            ),
            "summarized_message_ids": plan.summarized_message_ids,
            "retained_message_ids": plan.retained_message_ids,
            "retained_history_from_sequence": None,
            "retained_history_through_sequence": None,
            "protected_run_start_sequence": run_start_ref.sequence,
            "protected_run_through_sequence": event_slice.through_sequence,
        }
        if opened.window.source_summary_artifact_id != completed.summary_artifact_id:
            raise ContextEventSliceError("active compacted window summary drifted")
    elif latest_compaction_terminal_ref is None:
        retained_from = 1 if prior_transcript_through_sequence > 0 else None
        retained_through = (
            prior_transcript_through_sequence if retained_from is not None else None
        )
        window_payload = {
            "window_kind": "uncompacted",
            "compaction_terminal_ref": None,
            "compaction_summary_artifact_id": None,
            "compacted_through_sequence": None,
            "keep_after_sequence": None,
            "window_compaction_started_ref": None,
            "window_compaction_source_document_artifact_id": None,
            "window_compaction_source_document_fingerprint": None,
            "summarized_message_ids": (),
            "retained_message_ids": (),
            "retained_history_from_sequence": retained_from,
            "retained_history_through_sequence": retained_through,
            "protected_run_start_sequence": run_start_ref.sequence,
            "protected_run_through_sequence": event_slice.through_sequence,
        }
    else:
        terminal_stored = event_slice.event_by_id(
            latest_compaction_terminal_ref.event_id
        )
        if (
            terminal_stored.sequence != latest_compaction_terminal_ref.sequence
            or terminal_stored.payload_fingerprint
            != latest_compaction_terminal_ref.payload_fingerprint
        ):
            raise ContextEventSliceError("compaction terminal reference mismatch")
        terminal = decode_raw_stored_event_envelope(
            terminal_stored, DEFAULT_EVENT_SCHEMA_REGISTRY
        )
        if not isinstance(terminal, ContextCompactionCompletedEvent):
            raise ContextEventSliceError(
                "context window requires completed compaction terminal"
            )
        retained_from_value = terminal.keep_after_sequence + 1
        retained_through_value = min(
            prior_transcript_through_sequence,
            run_start_ref.sequence - 1,
        )
        if retained_from_value <= retained_through_value:
            retained_from = retained_from_value
            retained_through = retained_through_value
        else:
            retained_from = None
            retained_through = None
        window_payload = {
            "window_kind": (
                "preflight_compaction"
                if latest_compaction_terminal_ref.sequence < run_start_ref.sequence
                else "mid_turn_compaction"
            ),
            "compaction_terminal_ref": latest_compaction_terminal_ref,
            "compaction_summary_artifact_id": terminal.summary_artifact_id,
            "compacted_through_sequence": terminal.through_sequence,
            "keep_after_sequence": terminal.keep_after_sequence,
            "window_compaction_started_ref": None,
            "window_compaction_source_document_artifact_id": None,
            "window_compaction_source_document_fingerprint": None,
            "summarized_message_ids": (),
            "retained_message_ids": (),
            "retained_history_from_sequence": retained_from,
            "retained_history_through_sequence": retained_through,
            "protected_run_start_sequence": run_start_ref.sequence,
            "protected_run_through_sequence": event_slice.through_sequence,
        }
    window = TranscriptProjectionWindowFact(
        **window_payload,
        window_fingerprint=context_fingerprint(
            "transcript-projection-window:v1", window_payload
        ),
    )
    authority_candidates = [ref.sequence for ref in refs]
    if retained_from is not None:
        authority_candidates.append(retained_from)
    if window_lifecycle is not None:
        # Same-run compaction citations may point anywhere in the canonical
        # source document. Keep the ledger authority complete; only the
        # transcript projection is compacted.
        authority_candidates.append(event_slice.from_sequence)
    elif latest_compaction_terminal_ref is not None:
        authority_candidates.append(latest_compaction_terminal_ref.sequence)
    authority_from = min(authority_candidates)
    plan_payload = {
        "through_sequence": event_slice.through_sequence,
        "authority_from_sequence": authority_from,
        "required_local_event_refs": refs,
        "transcript_window": window,
    }
    return ContextAuthoritySlicePlan(
        **plan_payload,
        plan_fingerprint=context_fingerprint(
            "context-authority-slice-plan:v1", plan_payload
        ),
    )


def _active_window_compaction_lifecycle(
    *,
    event_slice: ContextEventSlice,
    run_id: str,
) -> (
    tuple[
        FrozenStoredEvent,
        ContextWindowOpenedEvent,
        FrozenStoredEvent,
        ContextWindowCompactionStartedEvent,
        FrozenStoredEvent,
        ContextWindowCompactionCompletedEvent,
    ]
    | None
):
    decoded = tuple(
        (
            stored,
            decode_raw_stored_event_envelope(stored, DEFAULT_EVENT_SCHEMA_REGISTRY),
        )
        for stored in event_slice.events
    )
    opened = tuple(
        (stored, event)
        for stored, event in decoded
        if isinstance(event, ContextWindowOpenedEvent) and event.run_id == run_id
    )
    closed_ids = {
        event.window_id
        for _stored, event in decoded
        if isinstance(event, ContextWindowClosedEvent) and event.run_id == run_id
    }
    active = tuple(
        item for item in opened if item[1].window.window_id not in closed_ids
    )
    if len(active) != 1:
        raise ContextEventSliceError("run requires exactly one active context window")
    open_stored, open_event = active[0]
    window = open_event.window
    if window.generation == 1:
        return None
    started_id = window.transcript_basis.source_compaction_started_event_id
    if started_id is None:
        raise ContextEventSliceError("compacted window lacks Started attribution")
    by_id = {stored.event_id: (stored, event) for stored, event in decoded}
    started_pair = by_id.get(started_id)
    if started_pair is None or not isinstance(
        started_pair[1], ContextWindowCompactionStartedEvent
    ):
        raise ContextEventSliceError("compacted window Started fact is unavailable")
    started_stored, started = started_pair
    completed_pair = by_id.get(started.plan.stable_completed_event_id)
    if completed_pair is None or not isinstance(
        completed_pair[1], ContextWindowCompactionCompletedEvent
    ):
        raise ContextEventSliceError("compacted window terminal fact is unavailable")
    completed_stored, completed = completed_pair
    if (
        started.plan.plan_fingerprint != completed.plan_fingerprint
        or completed.started_event_id != started.id
        or completed.target_window_open_event_id != open_event.id
        or started.plan.target_window_id != window.window_id
        or started.plan.target_window_generation != window.generation
        or window.transcript_basis.source_compaction_plan_fingerprint
        != started.plan.plan_fingerprint
        or window.source_compaction_id != started.plan.compaction_id
        or window.source_summary_fingerprint != completed.summary_fact_fingerprint
    ):
        raise ContextEventSliceError("compacted window lifecycle attribution drifted")
    return (
        open_stored,
        open_event,
        started_stored,
        started,
        completed_stored,
        completed,
    )


def build_context_snapshot(
    build_input: ContextSnapshotBuildInput,
    *,
    long_horizon_attribution: LongHorizonContextAttributionFact,
) -> ContextFactSnapshotFact:
    payload = build_input.model_dump(mode="python")
    payload["continuation_count"] = len(build_input.continuation_refs)
    payload["long_horizon_attribution"] = long_horizon_attribution
    semantic_payload = dict(payload)
    identity = dict(semantic_payload["identity"])
    identity.pop("snapshot_id", None)
    identity.pop("context_id", None)
    semantic_payload["identity"] = identity
    semantic_payload.pop("primary_event_range", None)
    semantic_payload.pop("named_event_ranges", None)
    semantic_payload.pop("subagent_graph_acceleration", None)
    semantic_fingerprint = context_fingerprint(
        "context-snapshot-semantic:v2", semantic_payload
    )
    fact_payload = {
        **payload,
        "snapshot_semantic_fingerprint": semantic_fingerprint,
    }
    return ContextFactSnapshotFact(
        **fact_payload,
        snapshot_fact_fingerprint=context_fingerprint(
            "context-snapshot-fact:v2", fact_payload
        ),
    )


def bind_context_draft(
    *,
    build_input: ContextSnapshotBuildInput,
    resolved_call: ResolvedModelCall,
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...],
) -> ContextFactSnapshotDraft:
    return ContextFactSnapshotDraft(
        fact=build_input,
        resolved_call=resolved_call,
        materialized_tool_specs=materialized_tool_specs,
    )


def bind_context_invocation(
    *,
    fact: ContextFactSnapshotFact,
    resolved_call: ResolvedModelCall,
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...],
) -> ContextFactSnapshot:
    return ContextFactSnapshot(
        fact=fact,
        resolved_call=resolved_call,
        materialized_tool_specs=materialized_tool_specs,
    )


__all__ = [
    "ContextFactSnapshot",
    "ContextFactSnapshotDraft",
    "ContextSnapshotBuildInput",
    "bind_context_invocation",
    "bind_context_draft",
    "build_context_snapshot",
    "finalize_context_authority_slice_plan",
]
