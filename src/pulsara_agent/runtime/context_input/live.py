"""Live collection of immutable context facts at one model-step boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Iterator

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    EventType,
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    ProjectionRequestedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.primitives.context import (
    CapabilityDescriptorRenderAttributionFact,
    ContextCompilePolicyFact,
    ContextCompileTimingFact,
    ContextContinuationReferenceFact,
    ContextEventReferenceFact,
    ContextInlineToolSchemaFact,
    ContextInputFailureReasonCode,
    ContextInputIdentityFact,
    ContextMaterializedToolSpecInput,
    ContextPlanSnapshotFact,
    PreparedContextCandidateSet,
    ContextProjectionReferenceFact,
    ContextRunEntryReferenceFact,
    ContextRuntimeEnvironmentFact,
    ContextStaticInstructionFact,
    ContextToolSpecFact,
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.tool_result import PreparedToolResultRenderInput
from pulsara_agent.primitives.run_boundary import InteractionResumeBoundaryFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventSlice,
    ContextEventSliceError,
    EventLogContextEventSliceReader,
)
from pulsara_agent.runtime.context_input.candidate import (
    ContextLifecycleCacheWriteCandidate,
    ContextCandidateCollectionInput,
    build_context_candidate_authorities,
    build_context_candidate_source_selections,
    collect_context_candidates,
)
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.context_input.snapshot import (
    ContextFactSnapshot,
    ContextSnapshotBuildInput,
    bind_context_invocation,
    build_context_snapshot,
    finalize_context_authority_slice_plan,
)
from pulsara_agent.runtime.context_input.transcript import (
    ContextTranscriptProjectionAuthority,
    NormalizedContextTranscript,
    TranscriptProjectionIdentity,
    ToolResultPairingError,
    project_context_transcript,
)
from pulsara_agent.runtime.context_input.render import (
    prepare_tool_result_render_input,
)
from pulsara_agent.runtime.run_entry import RunWorkingSet

if TYPE_CHECKING:
    from pulsara_agent.memory.foundation.protocols import ArtifactStore
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.state import LoopBudget


@dataclass(frozen=True, slots=True)
class PreparedLiveContextSnapshot:
    invocation: ContextFactSnapshot
    authority_slice: ContextEventSlice
    named_slices: tuple[ContextEventSlice, ...]
    normalized_transcript: NormalizedContextTranscript
    prepared_tool_results: PreparedToolResultRenderInput
    prepared_candidates: PreparedContextCandidateSet
    candidate_cache_writes: tuple[ContextLifecycleCacheWriteCandidate, ...]


@dataclass(frozen=True, slots=True)
class PreparedLiveTranscriptProjection:
    authority_slice: ContextEventSlice
    normalized_transcript: NormalizedContextTranscript
    prepared_tool_results: PreparedToolResultRenderInput


class ContextInputPreparationError(RuntimeError):
    def __init__(
        self,
        *,
        failure_stage: str,
        reason_code: ContextInputFailureReasonCode,
        snapshot_id: str | None,
        source_through_sequence: int | None,
        available_component_fingerprints: tuple[tuple[str, str], ...],
        cause: Exception,
    ) -> None:
        self.failure_stage = failure_stage
        self.reason_code = reason_code
        self.snapshot_id = snapshot_id
        self.source_through_sequence = source_through_sequence
        self.available_component_fingerprints = available_component_fingerprints
        self.cause = cause
        super().__init__(f"{failure_stage}: {type(cause).__name__}: {cause}")


@dataclass(slots=True)
class _ContextInputPreparationProgress:
    snapshot_id: str | None = None
    source_through_sequence: int | None = None
    component_fingerprints: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.component_fingerprints is None:
            self.component_fingerprints = {}


@contextmanager
def _preparation_stage(
    progress: _ContextInputPreparationProgress,
    stage: str,
    reason: ContextInputFailureReasonCode,
) -> Iterator[None]:
    try:
        yield
    except ContextInputPreparationError:
        raise
    except Exception as exc:
        actual_stage = (
            "tool_result_normalization"
            if isinstance(exc, ToolResultPairingError)
            else stage
        )
        actual_reason = (
            ContextInputFailureReasonCode.TOOL_RESULT_INVALID
            if isinstance(exc, ToolResultPairingError)
            else reason
        )
        raise ContextInputPreparationError(
            failure_stage=actual_stage,
            reason_code=actual_reason,
            snapshot_id=progress.snapshot_id,
            source_through_sequence=progress.source_through_sequence,
            available_component_fingerprints=tuple(
                sorted((progress.component_fingerprints or {}).items())
            ),
            cause=exc,
        ) from exc


def descriptor_render_attribution(
    *,
    descriptor: object,
    exposure_event_ref: ContextEventReferenceFact,
    exposure_fact: object,
) -> CapabilityDescriptorRenderAttributionFact:
    contract = getattr(descriptor, "result_render_contract", None)
    semantic = getattr(exposure_fact, "semantic", None)
    surface = getattr(semantic, "execution_surface", None)
    entries = tuple(getattr(surface, "entries", ()))
    descriptor_id = getattr(descriptor, "id", None)
    entry = next(
        (item for item in entries if item.descriptor_id == descriptor_id),
        None,
    )
    fingerprint = descriptor.fingerprint()
    if contract is None or entry is None or entry.descriptor_fingerprint != fingerprint:
        raise ContextEventSliceError(
            "descriptor is not an exact member of committed capability exposure"
        )
    payload = {
        "owner_runtime_session_id": exposure_event_ref.runtime_session_id,
        "exposure_id": exposure_fact.exposure_id,
        "exposure_fact_fingerprint": exposure_fact.exposure_fact_fingerprint,
        "descriptor_set_fingerprint": surface.descriptor_set_fingerprint,
        "descriptor_id": descriptor.id,
        "descriptor_fingerprint": fingerprint,
        "result_render_contract_fingerprint": contract.contract_fingerprint,
        "descriptor_source_event_id": exposure_event_ref.event_id,
        "descriptor_source_sequence": exposure_event_ref.sequence,
        "descriptor_source_payload_fingerprint": (
            exposure_event_ref.payload_fingerprint
        ),
    }
    return CapabilityDescriptorRenderAttributionFact(
        **payload,
        attribution_fingerprint=context_fingerprint(
            "capability-descriptor-render-attribution:v1", payload
        ),
    )


def build_context_tool_specs(
    *, working_set: RunWorkingSet
) -> tuple[
    tuple[ContextToolSpecFact, ...],
    tuple[ContextMaterializedToolSpecInput, ...],
]:
    exposure = working_set.effective_exposure_plan
    exposure_fact = working_set.effective_exposure_fact
    exposure_ref = working_set.effective_exposure_event_ref
    if not isinstance(exposure, CapabilityExposurePlan):
        raise ContextEventSliceError(
            "context tool specs require committed exposure plan"
        )
    if exposure_fact is None or exposure_ref is None:
        raise ContextEventSliceError(
            "context tool specs require committed exposure fact"
        )
    surface_entries = {
        item.capability_name: item
        for item in working_set.frozen_execution_surface.identity.entries
    }
    facts: list[ContextToolSpecFact] = []
    materialized: list[ContextMaterializedToolSpecInput] = []
    descriptors = (
        exposure.descriptors_by_name[name]
        for name in exposure.direct_names
        if name in exposure.callable_names
    )
    for descriptor in sorted(descriptors, key=lambda item: item.name):
        schema = freeze_json(descriptor.input_schema or {})
        if not isinstance(schema, FrozenJsonObjectFact):
            raise ContextEventSliceError("tool schema must be a JSON object")
        schema_fact = ContextInlineToolSchemaFact(
            schema=schema,
            schema_chars=len(canonical_json_bytes(schema).decode("utf-8")),
            schema_fingerprint=context_fingerprint("tool-schema:v1", schema),
        )
        attribution = descriptor_render_attribution(
            descriptor=descriptor,
            exposure_event_ref=exposure_ref,
            exposure_fact=exposure_fact,
        )
        surface_entry = surface_entries.get(descriptor.name)
        binding_fp = (
            surface_entry.binding_fingerprint if surface_entry is not None else None
        )
        if not binding_fp:
            raise ContextEventSliceError(
                f"callable tool {descriptor.name!r} lacks frozen binding fingerprint"
            )
        fact = ContextToolSpecFact(
            model_tool_name=descriptor.name,
            descriptor_id=descriptor.id,
            descriptor_fingerprint=descriptor.fingerprint(),
            descriptor_render_attribution=attribution,
            result_render_contract_fingerprint=(
                descriptor.result_render_contract.contract_fingerprint
            ),
            input_schema=schema_fact,
            description=descriptor.description,
            source_binding_fingerprint=binding_fp,
        )
        facts.append(fact)
        materialized.append(
            ContextMaterializedToolSpecInput(
                fact=fact,
                materialized_schema=schema,
            )
        )
    return tuple(facts), tuple(materialized)


def build_static_instruction(
    *,
    source_id: str,
    contract_version: str,
    content: str,
    archive: ArtifactStore,
    runtime_session_id: str,
    run_id: str,
    deadline_monotonic: float | None = None,
) -> ContextStaticInstructionFact:
    content_fp = sha256_fingerprint("context-static-instruction-content:v1", content)
    digest = sha256_fingerprint(
        "context-static-instruction-artifact:v1",
        [runtime_session_id, content_fp],
    ).removeprefix("sha256:")
    artifact_id = f"artifact:context-static-instruction:{digest}"
    archive.put_text_if_absent_or_confirm_identical(
        artifact_id,
        content,
        session_id=runtime_session_id,
        run_id=None,
        media_type="text/plain; charset=utf-8",
        semantic_metadata={
            "artifact_kind": "context_static_instruction",
            "source_id": source_id,
            "contract_version": contract_version,
            "content_fingerprint": content_fp,
        },
        deadline_monotonic=deadline_monotonic,
    )
    payload = {
        "source_id": source_id,
        "contract_version": contract_version,
        "content_artifact_id": artifact_id,
        "content_fingerprint": content_fp,
        "chars": len(content),
    }
    return ContextStaticInstructionFact(
        **payload,
        fact_fingerprint=context_fingerprint("context-static-instruction:v1", payload),
    )


def build_runtime_environment(
    *,
    workspace_identity_fingerprint: str,
    workspace_kind: str,
    model_visible_workspace_root: str,
    terminal_current_cwd: str,
    session_timezone: str | None,
    observed_at_utc: str,
) -> ContextRuntimeEnvironmentFact:
    payload = {
        "workspace_identity_fingerprint": workspace_identity_fingerprint,
        "workspace_kind": workspace_kind,
        "model_visible_workspace_root": model_visible_workspace_root,
        "terminal_current_cwd": terminal_current_cwd,
        "session_timezone": session_timezone,
        "observed_at_utc": observed_at_utc,
    }
    return ContextRuntimeEnvironmentFact(
        **payload,
        fact_fingerprint=context_fingerprint("context-runtime-environment:v1", payload),
    )


def _plan_snapshot(
    *, working_set: RunWorkingSet, event_slice: ContextEventSlice
) -> ContextPlanSnapshotFact:
    plan = working_set.plan_snapshot
    entered = None
    if plan.entered_event_id is not None:
        entered = event_slice.event_by_id(plan.entered_event_id).to_reference(
            event_slice.runtime_session_id
        )
        if entered.sequence != plan.entered_event_sequence:
            raise ContextEventSliceError("plan entered-event sequence mismatch")
    stored = plan.stored_default_permission
    stored_fp = context_fingerprint(
        "preset-permission-policy:v1", stored.model_dump(mode="json")
    )
    payload = {
        "workflow_id": plan.workflow_id,
        "active": plan.active,
        "revision": plan.revision,
        "entered_event": entered,
        "entry_run_id": plan.entry_run_id,
        "stored_default_permission_mode": stored.mode,
        "stored_default_permission_fingerprint": stored_fp,
        "accepted_plan_artifact_id": plan.accepted_plan_artifact_id,
    }
    return ContextPlanSnapshotFact(
        **payload,
        fact_fingerprint=context_fingerprint("context-plan-snapshot:v1", payload),
    )


def _run_and_continuation_refs(
    *, working_set: RunWorkingSet, event_slice: ContextEventSlice
) -> tuple[
    ContextRunEntryReferenceFact,
    tuple[ContextEventReferenceFact, ...],
    ContextContinuationReferenceFact | None,
]:
    start_stored = event_slice.event_by_id(working_set.run_start_event_id)
    start = start_stored.decode_owned()
    if not isinstance(start, RunStartEvent):
        raise ContextEventSliceError("working-set RunStart reference is not RunStart")
    if start.sequence != working_set.run_start_sequence:
        raise ContextEventSliceError("working-set RunStart sequence mismatch")
    entry = start.new_run_boundary or start.subagent_run_entry
    if entry is None:
        raise ContextEventSliceError("RunStart has no typed run entry")
    run_entry = ContextRunEntryReferenceFact(
        run_entry_kind="host" if start.new_run_boundary is not None else "subagent",
        run_start=start_stored.to_reference(event_slice.runtime_session_id),
        stable_terminal_event_id=start.terminal_run_end_event_id,
        run_entry=entry,
    )
    pairs: list[tuple[ContextEventReferenceFact, InteractionResumeBoundaryFact]] = []
    for frozen in event_slice.events:
        if frozen.event_type != EventType.RUN_INTERACTION_RESUME_BOUNDARY:
            continue
        decoded = frozen.decode_owned()
        if (
            isinstance(decoded, RunInteractionResumeBoundaryEvent)
            and decoded.run_id == start.run_id
        ):
            pairs.append(
                (frozen.to_reference(event_slice.runtime_session_id), decoded.boundary)
            )
    pairs.sort(key=lambda item: item[0].sequence)
    refs = tuple(item[0] for item in pairs)
    latest = None
    if pairs:
        ref, boundary = pairs[-1]
        latest = ContextContinuationReferenceFact(
            resume_boundary=ref,
            boundary=boundary,
            suspended_run_id=start.run_id,
            suspended_state_token_fingerprint=(
                boundary.suspended_state_token_fingerprint
            ),
        )
        if working_set.latest_committed_resume_boundary != boundary:
            raise ContextEventSliceError(
                "working-set continuation differs from durable latest boundary"
            )
        if working_set.latest_committed_resume_boundary_ref != ref:
            raise ContextEventSliceError(
                "working-set continuation reference differs from durable latest boundary"
            )
    elif working_set.latest_committed_resume_boundary is not None:
        raise ContextEventSliceError("working-set continuation is absent from ledger")
    elif working_set.latest_committed_resume_boundary_ref is not None:
        raise ContextEventSliceError(
            "working-set continuation reference is absent from ledger"
        )
    return run_entry, refs, latest


def collect_live_context_inputs(
    *,
    working_set: RunWorkingSet,
    resolved_call: ResolvedModelCall,
    event_slice: ContextEventSlice,
    identity: ContextInputIdentityFact,
    timing: ContextCompileTimingFact,
    compile_policy: ContextCompilePolicyFact,
    static_instructions: tuple[ContextStaticInstructionFact, ...],
    runtime_environment: ContextRuntimeEnvironmentFact,
    tool_specs: tuple[ContextToolSpecFact, ...],
    projections: tuple[ContextProjectionReferenceFact, ...] = (),
    candidate_sources: ContextCandidateCollectionInput,
    named_slices: tuple[ContextEventSlice, ...] = (),
    raw_suspended_state_token_for_validation: str | None = None,
) -> ContextSnapshotBuildInput:
    run_entry, continuation_refs, continuation = _run_and_continuation_refs(
        working_set=working_set,
        event_slice=event_slice,
    )
    start = event_slice.event_by_id(working_set.run_start_event_id).decode_owned()
    assert isinstance(start, RunStartEvent)
    permission = working_set.permission_snapshot.to_context_fact()
    if start.current_user_message.message_id != f"user-message:{start.run_id}":
        raise ContextEventSliceError("current user anchor does not match RunStart")
    if start.permission_snapshot_id != permission.snapshot_id:
        raise ContextEventSliceError("permission snapshot differs from RunStart")
    expected_permission = working_set.permission_snapshot.to_event_fields()
    if any(
        getattr(start, field_name) != expected_value
        for field_name, expected_value in expected_permission.items()
    ):
        raise ContextEventSliceError(
            "working-set permission contract differs from durable RunStart"
        )
    if start.model_target != resolved_call.target.fact:
        raise ContextEventSliceError("resolved call target differs from RunStart")
    if continuation is None:
        if raw_suspended_state_token_for_validation is not None:
            raise ContextEventSliceError(
                "non-continuation context cannot carry suspended state token"
            )
        effective_mcp_installation_id = start.mcp_installation_id
    else:
        actual_token_fingerprint = (
            sha256_fingerprint(
                "suspended-state-token:v1",
                raw_suspended_state_token_for_validation,
            )
            if raw_suspended_state_token_for_validation is not None
            else working_set.latest_validated_suspended_state_token_fingerprint
        )
        if actual_token_fingerprint is None:
            raise ContextEventSliceError(
                "continuation context lacks prior raw-token validation"
            )
        if actual_token_fingerprint != continuation.suspended_state_token_fingerprint:
            raise ContextEventSliceError("suspended state token fingerprint mismatch")
        effective_mcp_installation_id = continuation.boundary.mcp_installation_id
    capability = working_set.effective_exposure_fact
    capability_ref = working_set.effective_exposure_event_ref
    if capability is None or capability_ref is None:
        raise ContextEventSliceError(
            "live context requires committed capability exposure"
        )
    capability_event = event_slice.event_by_id(capability_ref.event_id).decode_owned()
    if not isinstance(capability_event, CapabilityExposureResolvedEvent):
        raise ContextEventSliceError("capability reference is not an exposure event")
    if capability_event.exposure != capability:
        raise ContextEventSliceError("working-set capability fact differs from ledger")
    if (
        capability.owner.runtime_session_id != identity.runtime_session_id
        or capability.owner.run_id != identity.run_id
    ):
        raise ContextEventSliceError("capability exposure owner differs from context")
    if capability.resolve_basis.permission_snapshot_id != permission.snapshot_id:
        raise ContextEventSliceError("capability permission basis differs from run")
    if capability.resolve_basis.mcp_installation_id != effective_mcp_installation_id:
        raise ContextEventSliceError("capability MCP basis differs from continuation")
    frozen_identity = working_set.frozen_execution_surface.identity
    if capability.resolve_basis.execution_surface_identity != frozen_identity:
        raise ContextEventSliceError(
            "capability exposure differs from frozen execution surface"
        )
    if frozen_identity.mcp_installation_id != effective_mcp_installation_id:
        raise ContextEventSliceError("frozen execution surface MCP identity mismatch")
    plan_snapshot = _plan_snapshot(
        working_set=working_set,
        event_slice=event_slice,
    )
    candidate_source_selections = build_context_candidate_source_selections(
        event_slice=event_slice,
        policy=compile_policy.candidate_collection,
    )
    subagent_result_ids = candidate_source_selections[0].selected_source_ids
    projections = collect_context_projection_references(
        event_slice=event_slice,
        capability_ref=capability_ref,
        capability=capability,
        explicit=projections,
        run_id=start.run_id,
        projection_token_budget=(
            compile_policy.candidate_collection.projection_token_budget
        ),
        subagent_result_ids=subagent_result_ids,
    )
    candidate_authorities = build_context_candidate_authorities(
        sources=candidate_sources,
        static_instructions=static_instructions,
        projections=projections,
        capability_snapshot=capability,
        plan_snapshot=plan_snapshot,
        event_slice=event_slice,
        run_id=identity.run_id,
        runtime_environment=runtime_environment,
        compile_timing=timing,
        source_selections=candidate_source_selections,
    )
    required_refs = (
        run_entry.run_start,
        capability_ref,
        *continuation_refs,
        *((plan_snapshot.entered_event,) if plan_snapshot.entered_event else ()),
        *(
            ref
            for authority in candidate_authorities
            for ref in authority.source_fact_refs
        ),
        *(
            ref
            for projection in projections
            for ref in projection.source_event_refs
            if ref.runtime_session_id == event_slice.runtime_session_id
        ),
    )
    latest_compaction = next(
        (
            frozen.to_reference(event_slice.runtime_session_id)
            for frozen in reversed(event_slice.events)
            if frozen.event_type == EventType.CONTEXT_COMPACTION_COMPLETED
        ),
        None,
    )
    authority_plan = finalize_context_authority_slice_plan(
        event_slice=event_slice,
        required_local_event_refs=required_refs,
        run_start_ref=run_entry.run_start,
        latest_compaction_terminal_ref=latest_compaction,
        prior_transcript_through_sequence=(
            run_entry.run_entry.transcript.source_through_sequence
            if hasattr(run_entry.run_entry, "transcript")
            else 0
        ),
        required_source_from_sequence=(
            candidate_source_selections[0].source_from_sequence
        ),
    )
    authority_slice = event_slice.subslice(
        from_sequence=authority_plan.authority_from_sequence
    )
    if identity.source_through_sequence != authority_slice.through_sequence:
        raise ContextEventSliceError("context identity high-water mismatch")
    return ContextSnapshotBuildInput(
        identity=identity,
        run_entry=run_entry,
        continuation=continuation,
        continuation_refs=continuation_refs,
        current_user_message=start.current_user_message,
        permission_snapshot=permission,
        resolved_model_call=resolved_call.fact,
        capability_snapshot=capability,
        plan_snapshot=plan_snapshot,
        mcp_installation_id=effective_mcp_installation_id,
        mcp_installation_owner_runtime_session_id=(
            start.mcp_installation_owner_runtime_session_id
        ),
        static_instructions=static_instructions,
        runtime_environment=runtime_environment,
        compile_policy=compile_policy,
        tool_specs=tool_specs,
        projections=projections,
        candidate_source_selections=candidate_source_selections,
        candidate_authorities=candidate_authorities,
        timing=timing,
        authority_slice_plan=authority_plan,
        primary_event_range=authority_slice.to_range_fact(),
        named_event_ranges=tuple(item.to_range_fact() for item in named_slices),
    )


def collect_context_projection_references(
    *,
    event_slice: ContextEventSlice,
    capability_ref: ContextEventReferenceFact,
    capability,
    explicit: tuple[ContextProjectionReferenceFact, ...],
    run_id: str,
    projection_token_budget: int,
    subagent_result_ids: tuple[str, ...],
) -> tuple[ContextProjectionReferenceFact, ...]:
    by_kind = {item.projection_kind: item for item in explicit}
    for projection_kind, projection in (
        ("capability_catalog", capability.semantic.catalog_projection),
        ("capability_active_skill", capability.semantic.active_skill_projection),
    ):
        artifacts = tuple(
            artifact_id
            for artifact_id in (projection.rendered_prompt_artifact_id,)
            if artifact_id is not None
        )
        by_kind.setdefault(
            projection_kind,
            ContextProjectionReferenceFact(
                projection_kind=projection_kind,
                owner_runtime_session_id=event_slice.runtime_session_id,
                source_event_refs=(capability_ref,),
                source_artifact_ids=artifacts,
                semantic_fingerprint=projection.projection_semantic_fingerprint,
            ),
        )
    memory_events = [
        (frozen, event)
        for frozen in event_slice.events
        if frozen.event_type
        in {
            EventType.PROJECTION_REQUESTED,
            EventType.PROJECTION_READY,
            EventType.PROJECTION_FAILED,
        }
        if isinstance(
            (event := frozen.decode_owned()),
            ProjectionRequestedEvent | ProjectionReadyEvent | ProjectionFailedEvent,
        )
        and event.run_id == run_id
    ]
    requests = [
        (frozen, event)
        for frozen, event in memory_events
        if isinstance(event, ProjectionRequestedEvent)
    ]
    if requests:
        request_frozen, request = requests[-1]
        if request.token_budget != projection_token_budget:
            raise ContextEventSliceError(
                "latest memory projection request budget differs from compile policy"
            )
        terminals = [
            (frozen, event)
            for frozen, event in memory_events
            if frozen.sequence > request_frozen.sequence
            and isinstance(event, ProjectionReadyEvent | ProjectionFailedEvent)
            and (
                event.projection_id,
                event.role,
                event.scope,
            )
            == (
                request.projection_id,
                request.role,
                request.scope,
            )
        ]
        if len(terminals) != 1:
            raise ContextEventSliceError(
                "latest memory projection request lacks one unique terminal outcome"
            )
        frozen, terminal = terminals[0]
        if terminal.token_budget != request.token_budget:
            raise ContextEventSliceError(
                "memory projection terminal differs from request budget"
            )
        if isinstance(terminal, ProjectionFailedEvent):
            by_kind.pop("memory", None)
        else:
            by_kind["memory"] = ContextProjectionReferenceFact(
                projection_kind="memory",
                owner_runtime_session_id=event_slice.runtime_session_id,
                source_event_refs=(
                    frozen.to_reference(event_slice.runtime_session_id),
                ),
                source_artifact_ids=(),
                semantic_fingerprint=context_fingerprint(
                    "memory-context-projection:v1",
                    {
                        "projection_id": terminal.projection_id,
                        "projection_kind": terminal.projection_kind,
                        "included_memory_ids": tuple(
                            terminal.included_memory_ids
                        ),
                        "filtered_memory_ids": tuple(
                            terminal.filtered_memory_ids
                        ),
                        "token_budget": terminal.token_budget,
                        "summary": terminal.summary,
                    },
                ),
            )
    elif memory_events or "memory" in by_kind:
        raise ContextEventSliceError(
            "memory projection authority exists without a durable request"
        )
    if subagent_result_ids:
        selected = set(subagent_result_ids)
        result_events = [
            (frozen, event)
            for frozen in event_slice.events
            if frozen.event_type == EventType.SUBAGENT_RUN_COMPLETED
            if isinstance((event := frozen.decode_owned()), SubagentRunCompletedEvent)
            and event.result_id in selected
        ]
        by_result = {
            event.result_id: (frozen, event) for frozen, event in result_events
        }
        if set(by_result) != selected or len(result_events) != len(selected):
            raise ContextEventSliceError(
                "selected subagent results lack unique durable completion facts"
            )
        ordered = tuple(sorted(by_result.values(), key=lambda item: item[0].sequence))
        refs = tuple(
            frozen.to_reference(event_slice.runtime_session_id)
            for frozen, _event in ordered
        )
        artifact_ids = tuple(
            artifact_id
            for _frozen, event in ordered
            for artifact_id in event.artifact_ids
        )
        by_kind["subagent_results"] = ContextProjectionReferenceFact(
            projection_kind="subagent_results",
            owner_runtime_session_id=event_slice.runtime_session_id,
            source_event_refs=refs,
            source_artifact_ids=artifact_ids,
            semantic_fingerprint=context_fingerprint(
                "subagent-results-context-projection:v1",
                {
                    "result_ids": subagent_result_ids,
                    "event_payload_fingerprints": tuple(
                        ref.payload_fingerprint for ref in refs
                    ),
                    "artifact_ids": artifact_ids,
                },
            ),
        )
    return tuple(by_kind[key] for key in sorted(by_kind))


async def prepare_live_context_snapshot(
    *,
    runtime_session: RuntimeSession,
    working_set: RunWorkingSet,
    resolved_call: ResolvedModelCall,
    budget: LoopBudget,
    system_prompt: str,
    context_id: str,
    model_call_index: int,
    compile_attempt_index: int,
    context_retry_index: int,
    compiled_at_utc: str,
    workspace_kind: str,
    terminal_current_cwd: str,
    session_timezone: str | None = None,
    compiled_local_date: str | None = None,
    candidate_sources: ContextCandidateCollectionInput,
    raw_suspended_state_token_for_validation: str | None = None,
) -> PreparedLiveContextSnapshot:
    progress = _ContextInputPreparationProgress()
    reader = EventLogContextEventSliceReader(
        event_log=runtime_session.event_log,
        runtime_session_id=runtime_session.runtime_session_id,
        reconciliation_required=lambda: runtime_session.reconciliation_required,
        io_service=runtime_session.context_input_io_service,
    )
    with _preparation_stage(
        progress,
        "event_slice",
        ContextInputFailureReasonCode.EVENT_SLICE_INVALID,
    ):
        full_slice = await reader.read_through_current_high_water(
            runtime_session_id=runtime_session.runtime_session_id,
            minimum_sequence=1,
        )
        progress.source_through_sequence = full_slice.through_sequence
        start = full_slice.event_by_id(working_set.run_start_event_id).decode_owned()
        if not isinstance(start, RunStartEvent):
            raise ContextEventSliceError("live snapshot RunStart is not durable")
        named_slices = await _child_named_context_slices(
            runtime_session=runtime_session,
            run_start=start,
        )
    identity = ContextInputIdentityFact(
        snapshot_id=f"context_snapshot:{context_id}:{compile_attempt_index}",
        compiler_contract_version="context-compiler-input:v1",
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=start.run_id,
        turn_id=start.turn_id,
        reply_id=start.reply_id,
        context_id=context_id,
        model_call_index=model_call_index,
        compile_attempt_index=compile_attempt_index,
        context_retry_index=context_retry_index,
        source_through_sequence=full_slice.through_sequence,
    )
    progress.snapshot_id = identity.snapshot_id
    timing = ContextCompileTimingFact(
        compiled_at_utc=compiled_at_utc,
        session_timezone=session_timezone,
        compiled_local_date=compiled_local_date,
        current_user_observed_at_utc=start.current_user_message.observed_at_utc,
    )
    with _preparation_stage(
        progress,
        "snapshot_build",
        ContextInputFailureReasonCode.SNAPSHOT_JOIN_MISMATCH,
    ):
        static_instructions: list[ContextStaticInstructionFact] = []
        instruction_sources = [
            (
                "base_system_instruction",
                "pulsara-system-prompt:v1",
                system_prompt,
            )
        ]
        if candidate_sources.memory_hook_prompt:
            instruction_sources.append(
                (
                    "memory_scope_instruction",
                    "pulsara-memory-scope-instruction:v1",
                    candidate_sources.memory_hook_prompt,
                )
            )
        for source_id, contract_version, content in instruction_sources:
            content_fingerprint = sha256_fingerprint(
                "context-static-instruction-content:v1",
                content,
            )
            cache_key = (source_id, contract_version, content_fingerprint)
            static = runtime_session.context_static_instruction_cache.get(cache_key)
            if static is None:
                static_deadline = monotonic() + 30.0
                static = await runtime_session.context_input_io_service.execute(
                    operation_name="context-static-instruction-write",
                    operation=lambda source_id=source_id,
                    contract_version=contract_version,
                    content=content,
                    static_deadline=static_deadline: build_static_instruction(
                        source_id=source_id,
                        contract_version=contract_version,
                        content=content,
                        archive=runtime_session.archive,
                        runtime_session_id=runtime_session.runtime_session_id,
                        run_id=start.run_id,
                        deadline_monotonic=static_deadline,
                    ),
                    deadline_monotonic=static_deadline,
                )
                runtime_session.context_static_instruction_cache[cache_key] = static
            static_instructions.append(static)
        environment = build_runtime_environment(
            workspace_identity_fingerprint=(
                working_set.capability_resolve_basis.fact.workspace_identity_fingerprint
            ),
            workspace_kind=workspace_kind,
            model_visible_workspace_root=str(runtime_session.workspace_root),
            terminal_current_cwd=terminal_current_cwd,
            session_timezone=session_timezone,
            observed_at_utc=compiled_at_utc,
        )
        tool_specs, materialized = build_context_tool_specs(working_set=working_set)
        if candidate_sources.system_prompt != system_prompt:
            raise ContextEventSliceError(
                "typed candidate system prompt differs from static instruction"
            )
        build_input = collect_live_context_inputs(
            working_set=working_set,
            resolved_call=resolved_call,
            event_slice=full_slice,
            identity=identity,
            timing=timing,
            compile_policy=resolve_context_compile_policy(budget),
            static_instructions=tuple(static_instructions),
            runtime_environment=environment,
            tool_specs=tool_specs,
            candidate_sources=candidate_sources,
            named_slices=named_slices,
            raw_suspended_state_token_for_validation=(
                raw_suspended_state_token_for_validation
            ),
        )
        fact = build_context_snapshot(build_input)
        assert progress.component_fingerprints is not None
        progress.component_fingerprints["snapshot_fact"] = (
            fact.snapshot_fact_fingerprint
        )
    authority_slice = full_slice.subslice(
        from_sequence=fact.authority_slice_plan.authority_from_sequence
    )
    summary_artifact_id = (
        fact.authority_slice_plan.transcript_window.compaction_summary_artifact_id
    )
    with _preparation_stage(
        progress,
        "transcript_normalization",
        ContextInputFailureReasonCode.TRANSCRIPT_INVALID,
    ):
        summary_text = None
        if summary_artifact_id is not None:
            summary_deadline = monotonic() + 30.0
            summary_text = await runtime_session.context_input_io_service.execute(
                operation_name="context-compaction-summary-read",
                operation=lambda: runtime_session.archive.get_text(
                    summary_artifact_id,
                    session_id=runtime_session.runtime_session_id,
                    deadline_monotonic=summary_deadline,
                ),
                deadline_monotonic=summary_deadline,
            )
        normalized = project_context_transcript(
            snapshot=fact,
            event_slice=authority_slice,
            compaction_summary_text=summary_text,
        )
        assert progress.component_fingerprints is not None
        progress.component_fingerprints["transcript"] = (
            normalized.transcript.transcript_fingerprint
        )
    with _preparation_stage(
        progress,
        "tool_result_policy_resolution",
        ContextInputFailureReasonCode.TOOL_RESULT_INVALID,
    ):
        prepared_tool_results = prepare_tool_result_render_input(
            units=normalized.tool_result_units,
            transcript=normalized.transcript,
            policy_basis=fact.compile_policy.tool_result_basis,
            cache=runtime_session.tool_result_render_cache,
        )
        assert progress.component_fingerprints is not None
        progress.component_fingerprints["tool_result_render_input"] = (
            prepared_tool_results.render_input_fingerprint
        )
    with _preparation_stage(
        progress,
        "candidate_collection",
        ContextInputFailureReasonCode.CANDIDATE_INVALID,
    ):
        prepared_candidates = collect_context_candidates(
            snapshot=fact,
            cache=runtime_session.context_candidate_lifecycle_cache,
        )
        for diagnostic in prepared_candidates.operational_diagnostics:
            runtime_session.record_context_input_cache_diagnostic(
                cache_kind="candidate_lifecycle",
                operation=diagnostic.operation,
                error=diagnostic.error,
            )
        assert progress.component_fingerprints is not None
        progress.component_fingerprints["prepared_candidate_set"] = (
            prepared_candidates.prepared.candidate_set_fingerprint
        )
    return PreparedLiveContextSnapshot(
        invocation=bind_context_invocation(
            fact=fact,
            resolved_call=resolved_call,
            materialized_tool_specs=materialized,
        ),
        authority_slice=authority_slice,
        named_slices=named_slices,
        normalized_transcript=normalized,
        prepared_tool_results=prepared_tool_results,
        prepared_candidates=prepared_candidates.prepared,
        candidate_cache_writes=prepared_candidates.cache_writes,
    )


async def prepare_live_transcript_projection(
    *,
    runtime_session: RuntimeSession,
    working_set: RunWorkingSet,
    budget: LoopBudget,
) -> PreparedLiveTranscriptProjection:
    """Freeze and normalize the current ledger without creating a model call.

    This is the mid-turn compaction seam. It shares the exact transcript/unit
    projector and render-policy resolver used by full context compilation, but
    deliberately has no resolved-call or context identity.
    """

    reader = EventLogContextEventSliceReader(
        event_log=runtime_session.event_log,
        runtime_session_id=runtime_session.runtime_session_id,
        reconciliation_required=lambda: runtime_session.reconciliation_required,
        io_service=runtime_session.context_input_io_service,
    )
    full_slice = await reader.read_through_current_high_water(
        runtime_session_id=runtime_session.runtime_session_id,
        minimum_sequence=1,
    )
    start = full_slice.event_by_id(working_set.run_start_event_id).decode_owned()
    if not isinstance(start, RunStartEvent):
        raise ContextEventSliceError("live transcript RunStart is not durable")
    run_entry, continuation_refs, _continuation = _run_and_continuation_refs(
        working_set=working_set,
        event_slice=full_slice,
    )
    capability_ref = working_set.effective_exposure_event_ref
    if capability_ref is None:
        raise ContextEventSliceError(
            "live transcript requires committed capability exposure"
        )
    latest_compaction = next(
        (
            frozen.to_reference(full_slice.runtime_session_id)
            for frozen in reversed(full_slice.events)
            if frozen.event_type == EventType.CONTEXT_COMPACTION_COMPLETED
        ),
        None,
    )
    authority_plan = finalize_context_authority_slice_plan(
        event_slice=full_slice,
        required_local_event_refs=(
            run_entry.run_start,
            capability_ref,
            *continuation_refs,
        ),
        run_start_ref=run_entry.run_start,
        latest_compaction_terminal_ref=latest_compaction,
        prior_transcript_through_sequence=(
            run_entry.run_entry.transcript.source_through_sequence
            if hasattr(run_entry.run_entry, "transcript")
            else 0
        ),
    )
    authority_slice = full_slice.subslice(
        from_sequence=authority_plan.authority_from_sequence
    )
    authority = ContextTranscriptProjectionAuthority(
        identity=TranscriptProjectionIdentity(
            runtime_session_id=runtime_session.runtime_session_id
        ),
        run_entry=run_entry,
        current_user_message=start.current_user_message,
        authority_slice_plan=authority_plan,
        primary_event_range=authority_slice.to_range_fact(),
    )
    summary_artifact_id = (
        authority_plan.transcript_window.compaction_summary_artifact_id
    )
    summary_text = None
    if summary_artifact_id is not None:
        summary_deadline = monotonic() + 30.0
        summary_text = await runtime_session.context_input_io_service.execute(
            operation_name="context-compaction-summary-read",
            operation=lambda: runtime_session.archive.get_text(
                summary_artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=summary_deadline,
            ),
            deadline_monotonic=summary_deadline,
        )
    normalized = project_context_transcript(
        snapshot=authority,
        event_slice=authority_slice,
        compaction_summary_text=summary_text,
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(budget).tool_result_basis,
        cache=runtime_session.tool_result_render_cache,
    )
    return PreparedLiveTranscriptProjection(
        authority_slice=authority_slice,
        normalized_transcript=normalized,
        prepared_tool_results=prepared,
    )


async def _child_named_context_slices(
    *,
    runtime_session: RuntimeSession,
    run_start: RunStartEvent,
) -> tuple[ContextEventSlice, ...]:
    entry = run_start.subagent_run_entry
    if entry is None:
        return ()
    locator = runtime_session.context_event_log_locator
    if locator is None:
        raise ContextEventSliceError("child context requires a parent EventLogLocator")
    parent_log = locator.event_log_for_runtime_session(entry.parent_runtime_session_id)
    deadline = monotonic() + 30.0
    read = await runtime_session.context_input_io_service.execute(
        operation_name="context-child-parent-event-slice-read",
        operation=lambda: parent_log.read_range_snapshot(
            minimum_sequence=1,
            deadline_monotonic=deadline,
        ),
        deadline_monotonic=deadline,
    )
    if read.through_sequence < 1:
        raise ContextEventSliceError("child parent ledger is empty")
    parent_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id=entry.parent_runtime_session_id,
        minimum_sequence=1,
        snapshot=read,
    )
    matching = tuple(
        event
        for frozen in parent_slice.events
        if isinstance((event := frozen.decode_owned()), SubagentRunStartedEvent)
        and event.subagent_run_id == entry.subagent_run_id
        and event.child_runtime_session_id == runtime_session.runtime_session_id
        and event.edge_id == entry.spawn_edge_id
        and event.parent_run_id == entry.parent_run_id
    )
    if len(matching) != 1:
        raise ContextEventSliceError(
            "child context parent slice lacks its unique spawn fact"
        )
    return (parent_slice,)


__all__ = [
    "ContextInputPreparationError",
    "PreparedLiveContextSnapshot",
    "PreparedLiveTranscriptProjection",
    "build_context_tool_specs",
    "build_runtime_environment",
    "build_static_instruction",
    "collect_live_context_inputs",
    "descriptor_render_attribution",
    "prepare_live_context_snapshot",
    "prepare_live_transcript_projection",
]
