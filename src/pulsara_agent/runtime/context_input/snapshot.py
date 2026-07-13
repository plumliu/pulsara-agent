"""Pure context snapshot construction and authority-window finalization."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import model_validator

from pulsara_agent.event.events import (
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.primitives.context import (
    ContextAuthoritySlicePlan,
    ContextCandidateAuthorityFact,
    ContextCandidateSourceSelectionFact,
    ContextCompileInputManifestFact,
    ContextCompilePolicyFact,
    ContextCompileTimingFact,
    ContextContinuationReferenceFact,
    ContextEventRangeFact,
    ContextEventReferenceFact,
    ContextFactSnapshotFact,
    ContextInputIdentityFact,
    ContextMaterializedToolSpecInput,
    ContextPlanSnapshotFact,
    ContextProjectionReferenceFact,
    ContextRunEntryReferenceFact,
    ContextRuntimeEnvironmentFact,
    ContextStaticInstructionFact,
    ContextToolSpecFact,
    FrozenContextFact,
    RunPermissionSnapshotFact,
    TranscriptProjectionWindowFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.run_entry import CurrentUserMessageFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventSlice,
    ContextEventSliceError,
)
from pulsara_agent.runtime.context_input.candidate import (
    build_context_candidate_source_selections,
)


class ContextCandidateSelectionMismatch(RuntimeError):
    """The manifest selection differs from a valid ledger-derived selection."""


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
    candidate_source_selections: tuple[ContextCandidateSourceSelectionFact, ...]
    candidate_authorities: tuple[ContextCandidateAuthorityFact, ...]
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


def finalize_context_authority_slice_plan(
    *,
    event_slice: ContextEventSlice,
    required_local_event_refs: tuple[ContextEventReferenceFact, ...],
    run_start_ref: ContextEventReferenceFact,
    latest_compaction_terminal_ref: ContextEventReferenceFact | None,
    prior_transcript_through_sequence: int | None = None,
    required_source_from_sequence: int | None = None,
) -> ContextAuthoritySlicePlan:
    refs = tuple(
        sorted(
            {
                ref.event_id: ref for ref in (*required_local_event_refs, run_start_ref)
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
    if latest_compaction_terminal_ref is None:
        retained_from = (
            event_slice.from_sequence
            if event_slice.from_sequence <= prior_transcript_through_sequence
            else None
        )
        retained_through = prior_transcript_through_sequence if retained_from else None
        window_payload = {
            "window_kind": "uncompacted",
            "compaction_terminal_ref": None,
            "compaction_summary_artifact_id": None,
            "compacted_through_sequence": None,
            "keep_after_sequence": None,
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
        terminal = terminal_stored.decode_owned()
        if not isinstance(terminal, ContextCompactionCompletedEvent):
            raise ContextEventSliceError(
                "context window requires completed compaction terminal"
            )
        retained_from_value = terminal.keep_after_sequence + 1
        retained_through_value = min(
            terminal.through_sequence,
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
    if required_source_from_sequence is not None:
        if not (
            event_slice.from_sequence
            <= required_source_from_sequence
            <= event_slice.through_sequence
        ):
            raise ContextEventSliceError(
                "required source range start is outside the event slice"
            )
        authority_candidates.append(required_source_from_sequence)
    if retained_from is not None:
        authority_candidates.append(retained_from)
    if latest_compaction_terminal_ref is not None:
        authority_candidates.append(latest_compaction_terminal_ref.sequence)
    authority_from = min(authority_candidates)
    plan_payload = {
        "through_sequence": event_slice.through_sequence,
        "authority_from_sequence": authority_from,
        "required_local_event_refs": refs,
        "required_source_from_sequence": required_source_from_sequence,
        "transcript_window": window,
    }
    return ContextAuthoritySlicePlan(
        **plan_payload,
        plan_fingerprint=context_fingerprint(
            "context-authority-slice-plan:v1", plan_payload
        ),
    )


def build_context_snapshot(
    build_input: ContextSnapshotBuildInput,
) -> ContextFactSnapshotFact:
    payload = build_input.model_dump(mode="python")
    payload["continuation_count"] = len(build_input.continuation_refs)
    semantic_payload = dict(payload)
    identity = dict(semantic_payload["identity"])
    identity.pop("snapshot_id", None)
    identity.pop("context_id", None)
    semantic_payload["identity"] = identity
    semantic_payload.pop("primary_event_range", None)
    semantic_payload.pop("named_event_ranges", None)
    semantic_fingerprint = context_fingerprint(
        "context-snapshot-semantic:v1", semantic_payload
    )
    fact_payload = {
        **payload,
        "snapshot_semantic_fingerprint": semantic_fingerprint,
    }
    return ContextFactSnapshotFact(
        **fact_payload,
        snapshot_fact_fingerprint=context_fingerprint(
            "context-snapshot-fact:v1", fact_payload
        ),
    )


def collect_replay_context_inputs(
    *,
    input_manifest: ContextCompileInputManifestFact,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...],
) -> ContextSnapshotBuildInput:
    snapshot = input_manifest.snapshot
    if event_slice.to_range_fact() != snapshot.primary_event_range:
        raise ContextEventSliceError("replay primary event slice mismatch")
    named_ranges = tuple(item.to_range_fact() for item in named_slices)
    if named_ranges != snapshot.named_event_ranges:
        raise ContextEventSliceError("replay named event slices mismatch")
    _validate_replay_durable_joins(snapshot=snapshot, event_slice=event_slice)
    payload = snapshot.model_dump(
        mode="python",
        exclude={
            "continuation_count",
            "snapshot_semantic_fingerprint",
            "snapshot_fact_fingerprint",
        },
    )
    return ContextSnapshotBuildInput.model_validate(payload)


def _validate_replay_durable_joins(
    *,
    snapshot: ContextFactSnapshotFact,
    event_slice: ContextEventSlice,
) -> None:
    start_stored = event_slice.event_by_id(snapshot.run_entry.run_start.event_id)
    start_ref = start_stored.to_reference(event_slice.runtime_session_id)
    if start_ref != snapshot.run_entry.run_start:
        raise ContextEventSliceError("replay RunStart reference mismatch")
    start = start_stored.decode_owned()
    if not isinstance(start, RunStartEvent):
        raise ContextEventSliceError("replay run entry is not RunStart")
    entry = start.new_run_boundary or start.subagent_run_entry
    if (
        entry != snapshot.run_entry.run_entry
        or start.current_user_message != snapshot.current_user_message
        or start.terminal_run_end_event_id
        != snapshot.run_entry.stable_terminal_event_id
    ):
        raise ContextEventSliceError("replay RunStart payload differs from snapshot")
    permission = snapshot.permission_snapshot
    if (
        start.permission_snapshot_id != permission.snapshot_id
        or start.permission_mode != permission.mode.value
        or freeze_json(start.permission_policy) != permission.expanded_policy
        or start.permission_snapshot_source != permission.source
        or start.model_target != snapshot.resolved_model_call.target
    ):
        raise ContextEventSliceError("replay RunStart contract differs from snapshot")

    durable_continuations: list[tuple[ContextEventReferenceFact, object]] = []
    exposures: list[CapabilityExposureResolvedEvent] = []
    for frozen in event_slice.events:
        event = frozen.decode_owned()
        if (
            isinstance(event, RunInteractionResumeBoundaryEvent)
            and event.run_id == start.run_id
        ):
            durable_continuations.append(
                (frozen.to_reference(event_slice.runtime_session_id), event.boundary)
            )
        elif (
            isinstance(event, CapabilityExposureResolvedEvent)
            and event.run_id == start.run_id
            and event.exposure == snapshot.capability_snapshot
        ):
            exposures.append(event)
    durable_continuations.sort(key=lambda item: item[0].sequence)
    if tuple(item[0] for item in durable_continuations) != snapshot.continuation_refs:
        raise ContextEventSliceError(
            "replay continuation history differs from snapshot"
        )
    if snapshot.continuation is not None:
        latest_ref, latest_boundary = durable_continuations[-1]
        if (
            latest_ref != snapshot.continuation.resume_boundary
            or latest_boundary != snapshot.continuation.boundary
        ):
            raise ContextEventSliceError(
                "replay latest continuation differs from snapshot"
            )
    if len(exposures) != 1:
        raise ContextEventSliceError(
            "replay snapshot requires one exact capability exposure fact"
        )
    try:
        replayed_selections = build_context_candidate_source_selections(
            event_slice=event_slice,
            policy=snapshot.compile_policy.candidate_collection,
        )
    except ValueError as exc:
        raise ContextEventSliceError(
            "replay candidate source selection cannot be derived"
        ) from exc
    if replayed_selections != snapshot.candidate_source_selections:
        raise ContextCandidateSelectionMismatch(
            "replayed candidate source selection differs from manifest"
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
    "ContextSnapshotBuildInput",
    "bind_context_invocation",
    "build_context_snapshot",
    "collect_replay_context_inputs",
    "finalize_context_authority_slice_plan",
]
