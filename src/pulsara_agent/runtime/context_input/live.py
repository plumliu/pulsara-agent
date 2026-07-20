"""Live collection of immutable context facts at one model-step boundary."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterable
from dataclasses import dataclass
import json
from time import monotonic
from typing import TYPE_CHECKING, Iterator

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextWindowCompactionCompletedEvent,
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
from pulsara_agent.event_log.protocol import (
    RawContextAuthorityBundleRequest,
    RawEventLogReadSnapshot,
    RawEventSelectionBounds,
    RawStoredEventEnvelope,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
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
    WindowCompactionSourceDocumentFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.context_source import (
    ContextArtifactReferenceFact,
    LedgerAuthorityHorizonFact,
    ResolvedContextSourcePhysicalInputPolicyFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    LongHorizonRolloutStatusCandidateFact,
    RolloutBudgetStateFact,
    SubagentGraphAccelerationFact,
    SubagentGraphSemanticSourceFact,
)
from pulsara_agent.primitives.tool_result import PreparedToolResultRenderInput
from pulsara_agent.primitives.run_boundary import InteractionResumeBoundaryFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    ContextEventSliceError,
    FrozenStoredEvent,
    SparseAuthorityCursor,
)
from pulsara_agent.runtime.context_input.candidate import (
    ContextLifecycleCacheWriteCandidate,
    build_context_candidate_source_selections,
    collect_context_candidates,
)
from pulsara_agent.runtime.context_input.sources.builder import (
    ContextSourceArtifactMetadata,
    HydratedContextSourceArtifact,
    build_context_sources,
    default_context_source_registry,
    hydrate_context_source_content_sidecar,
)
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.context_input.snapshot import (
    ContextFactSnapshotDraft,
    ContextSnapshotBuildInput,
    bind_context_draft,
    finalize_context_authority_slice_plan,
)
from pulsara_agent.runtime.context_input.transcript import (
    NormalizedContextTranscript,
    ToolResultPairingError,
)
from pulsara_agent.runtime.context_input.stable_transcript import (
    project_stable_context_transcript,
    required_terminal_content_artifacts,
)
from pulsara_agent.runtime.context_input.transcript_authority import (
    PreparedNamedFactArtifact,
    prepare_named_fact_artifact,
)
from pulsara_agent.runtime.context_input.render import (
    prepare_tool_result_render_input,
)
from pulsara_agent.runtime.authority_materialization.checkpoint_service import (
    PreparedTranscriptProjectionEvidence,
)
from pulsara_agent.runtime.long_horizon.status import (
    derive_rollout_status_candidate,
    derive_rollout_status_candidate_from_state,
)
from pulsara_agent.runtime.run_entry import RunWorkingSet

if TYPE_CHECKING:
    from pulsara_agent.memory.foundation.protocols import ArtifactStore
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.state import LoopBudget
    from pulsara_agent.runtime.subagent.facts import SubagentGraphState


@dataclass(frozen=True, slots=True)
class PreparedLiveContextSnapshot:
    invocation: ContextFactSnapshotDraft
    snapshot_build_input: ContextSnapshotBuildInput
    authority_slice: ContextEventSlice | ContextEventAuthorityView
    named_slices: tuple[ContextEventSlice, ...]
    exact_named_authority_events: tuple[FrozenStoredEvent, ...]
    normalized_transcript: NormalizedContextTranscript
    prepared_tool_results: PreparedToolResultRenderInput
    prepared_candidates: PreparedContextCandidateSet
    context_source_hydrated_contents: tuple[tuple[str, str], ...]
    transcript_projection_evidence: PreparedTranscriptProjectionEvidence
    prepared_named_fact_artifacts: tuple[PreparedNamedFactArtifact, ...]
    candidate_cache_writes: tuple[ContextLifecycleCacheWriteCandidate, ...]
    active_window: ContextWindowFact
    projection_state: ContextWindowProjectionState
    rollout_state: RolloutBudgetStateFact


@dataclass(frozen=True, slots=True)
class PreparedLiveTranscriptProjection:
    authority_slice: ContextEventSlice | ContextEventAuthorityView
    normalized_transcript: NormalizedContextTranscript
    prepared_tool_results: PreparedToolResultRenderInput
    transcript_projection_evidence: PreparedTranscriptProjectionEvidence


@dataclass(frozen=True, slots=True)
class _LiveAuthorityRead:
    primary_slice: ContextEventSlice
    local_named_slices: tuple[ContextEventSlice, ...]
    run_start: RunStartEvent
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]

    @property
    def view(self) -> ContextEventSlice | ContextEventAuthorityView:
        if not self.local_named_slices:
            return self.primary_slice
        return ContextEventAuthorityView(
            primary_slice=self.primary_slice,
            named_slices=self.local_named_slices,
        )


@dataclass(frozen=True, slots=True)
class _ChildAuthorityRead:
    slices: tuple[ContextEventSlice, ...]
    rollout_state: RolloutBudgetStateFact | None
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]


def _ledger_authority_horizon(
    *,
    runtime_session_id: str,
    prefix: RawTranscriptDomainPrefixFact,
) -> LedgerAuthorityHorizonFact:
    """Bind one ledger's canonical prefix proof to a model-visible authority read."""

    return build_frozen_fact(
        LedgerAuthorityHorizonFact,
        schema_version="ledger_authority_horizon.v1",
        runtime_session_id=runtime_session_id,
        through_sequence=prefix.through_sequence,
        ledger_event_count_through=prefix.through_sequence,
        ledger_continuity_accumulator_through=(prefix.ledger_continuity_accumulator),
    )


def _merge_authority_horizons(
    *groups: tuple[LedgerAuthorityHorizonFact, ...],
) -> tuple[LedgerAuthorityHorizonFact, ...]:
    by_ledger: dict[str, LedgerAuthorityHorizonFact] = {}
    for horizon in (item for group in groups for item in group):
        existing = by_ledger.get(horizon.runtime_session_id)
        if existing is not None and existing != horizon:
            raise ContextEventSliceError(
                "one context snapshot observed conflicting ledger authority horizons"
            )
        by_ledger[horizon.runtime_session_id] = horizon
    return tuple(by_ledger[key] for key in sorted(by_ledger))


_MAX_LIVE_AUTHORITY_EVENTS = 16_384
_MAX_LIVE_AUTHORITY_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_LIVE_SPARSE_AUTHORITY_EVENTS = 4_096
_MAX_LIVE_SPARSE_AUTHORITY_PAYLOAD_BYTES = 4 * 1024 * 1024

_LIVE_RUN_SPARSE_EVENT_TYPES = (
    EventType.RUN_START.value,
    EventType.RUN_INTERACTION_RESUME_BOUNDARY.value,
    EventType.CAPABILITY_EXPOSURE_RESOLVED.value,
    EventType.PROJECTION_REQUESTED.value,
    EventType.PROJECTION_READY.value,
    EventType.PROJECTION_FAILED.value,
    EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED.value,
    EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED.value,
    EventType.ROLLOUT_BUDGET_RESERVATION_CREATED.value,
    EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED.value,
    EventType.ROLLOUT_PHASE_TRANSITIONED.value,
    EventType.CONTEXT_COMPACTION_COMPLETED.value,
    EventType.CONTEXT_WINDOW_OPENED.value,
    EventType.CONTEXT_WINDOW_CLOSED.value,
    EventType.CONTEXT_WINDOW_COMPACTION_STARTED.value,
    EventType.CONTEXT_WINDOW_COMPACTION_COMPLETED.value,
)

_LIVE_SESSION_SPARSE_EVENT_TYPES = (
    EventType.PLAN_MODE_ENTERED.value,
    EventType.PLAN_EXIT_RESOLVED.value,
    EventType.PLAN_MODE_EXITED.value,
)


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
    *,
    working_set: RunWorkingSet,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
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
    *,
    working_set: RunWorkingSet,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
) -> tuple[
    ContextRunEntryReferenceFact,
    tuple[ContextEventReferenceFact, ...],
    ContextContinuationReferenceFact | None,
]:
    start_stored = event_slice.event_by_id(working_set.run_start_event_id)
    start = start_stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
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
        decoded = frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
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
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    identity: ContextInputIdentityFact,
    timing: ContextCompileTimingFact,
    compile_policy: ContextCompilePolicyFact,
    static_instructions: tuple[ContextStaticInstructionFact, ...],
    runtime_environment: ContextRuntimeEnvironmentFact,
    tool_specs: tuple[ContextToolSpecFact, ...],
    subagent_graph: "SubagentGraphState",
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact,
    subagent_graph_acceleration: SubagentGraphAccelerationFact,
    subagent_authority_events: tuple[FrozenStoredEvent, ...],
    source_artifact_metadata: dict[str, ContextSourceArtifactMetadata],
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    projections: tuple[ContextProjectionReferenceFact, ...] = (),
    named_slices: tuple[ContextEventSlice, ...] = (),
    raw_suspended_state_token_for_validation: str | None = None,
    rollout_status_override: LongHorizonRolloutStatusCandidateFact | None = None,
    derive_rollout_status_from_events: bool = True,
) -> ContextSnapshotBuildInput:
    run_entry, continuation_refs, continuation = _run_and_continuation_refs(
        working_set=working_set,
        event_slice=event_slice,
    )
    start = event_slice.event_by_id(working_set.run_start_event_id).decode_owned(
        DEFAULT_EVENT_SCHEMA_REGISTRY
    )
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
    capability_event = event_slice.event_by_id(capability_ref.event_id).decode_owned(
        DEFAULT_EVENT_SCHEMA_REGISTRY
    )
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
        subagent_graph=subagent_graph,
        semantic_source=subagent_graph_semantic_source,
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
        subagent_authority_events=subagent_authority_events,
    )
    rollout_owner_runtime_session_id = (
        start.long_horizon.rollout_account_owner_runtime_session_id
    )
    rollout_slices = tuple(
        item
        for item in (
            *(
                (event_slice.primary_slice, *event_slice.named_slices)
                if isinstance(event_slice, ContextEventAuthorityView)
                else (event_slice,)
            ),
            *named_slices,
        )
        if item.runtime_session_id == rollout_owner_runtime_session_id
    )
    if not rollout_slices:
        raise ContextEventSliceError(
            "context input requires frozen rollout-account authority"
        )
    rollout_event_slice: ContextEventSlice | ContextEventAuthorityView
    if len(rollout_slices) == 1:
        rollout_event_slice = rollout_slices[0]
    else:
        primary_rollout_slice = max(
            rollout_slices,
            key=lambda item: item.through_sequence,
        )
        rollout_event_slice = ContextEventAuthorityView(
            primary_slice=primary_rollout_slice,
            named_slices=tuple(
                item for item in rollout_slices if item is not primary_rollout_slice
            ),
        )
    rollout_status = (
        derive_rollout_status_candidate(
            event_slice=rollout_event_slice,
            account_id=start.long_horizon.rollout_account_id,
            policy=start.long_horizon.rollout_status_hint_policy,
        )
        if derive_rollout_status_from_events
        else rollout_status_override
    )
    source_build = build_context_sources(
        registry=default_context_source_registry(),
        static_instructions=static_instructions,
        artifact_metadata=source_artifact_metadata,
        projections=projections,
        capability_snapshot=capability,
        plan_snapshot=plan_snapshot,
        event_slice=event_slice,
        runtime_environment=runtime_environment,
        compile_timing=timing,
        resolved_model_call=resolved_call.fact,
        source_selections=candidate_source_selections,
        external_authority_events={
            event.event_id: event for event in subagent_authority_events
        },
        rollout_status=rollout_status,
        tool_specs=tool_specs,
        authority_horizons=authority_horizons,
    )
    external_authority_ids = {event.event_id for event in subagent_authority_events}
    required_refs = (
        run_entry.run_start,
        capability_ref,
        *continuation_refs,
        *((plan_snapshot.entered_event,) if plan_snapshot.entered_event else ()),
        *(
            ref
            for candidate in source_build.candidates
            for ref in candidate.attribution.source_event_refs
            if ref.event_id not in external_authority_ids
        ),
        *(
            ref
            for disposition in source_build.source_dispositions
            for ref in disposition.source_event_refs
            if ref.event_id not in external_authority_ids
        ),
        *(
            ref
            for projection in projections
            for ref in projection.source_event_refs
            if ref.runtime_session_id == event_slice.runtime_session_id
            and ref.event_id not in external_authority_ids
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
            start.run_transcript_seed_reference.source_ledger_through_sequence
        ),
    )
    if isinstance(event_slice, ContextEventAuthorityView):
        authority_slice = event_slice.primary_slice
    elif authority_plan.authority_from_sequence < event_slice.from_sequence:
        authority_slice = event_slice
    else:
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
        subagent_graph_semantic_source=subagent_graph_semantic_source,
        subagent_graph_acceleration=subagent_graph_acceleration,
        candidate_source_selections=candidate_source_selections,
        context_source_candidates=source_build.candidates,
        context_source_dispositions=source_build.source_dispositions,
        capability_tool_catalog_root=source_build.tool_catalog_root,
        context_source_physical_input_policy=source_build.physical_input_policy,
        context_source_registry_fingerprint=source_build.registry_fingerprint,
        timing=timing,
        authority_slice_plan=authority_plan,
        primary_event_range=authority_slice.to_range_fact(),
        named_event_ranges=(
            *(
                event_slice.named_range_facts()
                if isinstance(event_slice, ContextEventAuthorityView)
                else ()
            ),
            *(item.to_range_fact() for item in named_slices),
        ),
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
    subagent_authority_events: tuple[FrozenStoredEvent, ...],
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
            (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
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
                        "included_memory_ids": tuple(terminal.included_memory_ids),
                        "filtered_memory_ids": tuple(terminal.filtered_memory_ids),
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
            for frozen in subagent_authority_events
            if isinstance(
                (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
                SubagentRunCompletedEvent,
            )
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


def _rollout_status_from_store(
    *,
    runtime_session: RuntimeSession,
    start: RunStartEvent,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    state: RolloutBudgetStateFact,
) -> LongHorizonRolloutStatusCandidateFact | None:
    contract = start.long_horizon
    owner_runtime_session_id = contract.rollout_account_owner_runtime_session_id
    owner_store = (
        runtime_session.long_horizon_state_store
        if owner_runtime_session_id == runtime_session.runtime_session_id
        else runtime_session.rollout_account_owner_state_store
    )
    if owner_store is None:
        raise ContextEventSliceError(
            "live context lacks its rollout-account owner state store"
        )
    account = owner_store.rollout_account(contract.rollout_account_id)
    if account is None:
        raise ContextEventSliceError(
            "session-owned rollout status is absent from the incremental store"
        )
    return derive_rollout_status_candidate_from_state(
        event_slice=event_slice,
        account=account,
        state=state,
        policy=contract.rollout_status_hint_policy,
    )


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
    memory_scope_instruction: str | None = None,
    raw_suspended_state_token_for_validation: str | None = None,
) -> PreparedLiveContextSnapshot:
    progress = _ContextInputPreparationProgress()
    with _preparation_stage(
        progress,
        "event_slice",
        ContextInputFailureReasonCode.EVENT_SLICE_INVALID,
    ):
        authority_read = await _read_live_primary_event_slice(
            runtime_session=runtime_session,
            working_set=working_set,
        )
        full_slice = authority_read.view
        start = authority_read.run_start
        progress.source_through_sequence = full_slice.through_sequence
        live_store = runtime_session.long_horizon_state_store
        live_window_chain = live_store.window_state(start.run_id)
        if (
            live_window_chain is None
            or not live_window_chain.consistent
            or live_window_chain.active_window_id is None
            or live_window_chain.through_sequence != full_slice.through_sequence
        ):
            raise ContextEventSliceError(
                "live context window reducer differs from the frozen authority"
            )
        active_window = live_window_chain.windows[live_window_chain.active_window_id]
        projection_state = live_store.projection_state(active_window.window_id)
        if projection_state is None:
            raise ContextEventSliceError(
                "live context lacks its active projection reducer state"
            )
        reducer_binding = (
            runtime_session.subagent_graph_checkpoint_service.reducer_binding
        )
        if start.subagent_graph_reducer_contract != reducer_binding.contract:
            raise ContextEventSliceError(
                "live snapshot reducer binding differs from RunStart contract"
            )
        child_authority = await _child_named_context_slices(
            runtime_session=runtime_session,
            run_start=start,
        )
        child_named_slices = child_authority.slices
        rollout_state = (
            child_authority.rollout_state
            if child_named_slices
            else live_store.rollout_state_at(
                start.long_horizon.rollout_account_id,
                through_sequence=full_slice.through_sequence,
            )
        )
        if rollout_state is None:
            raise ContextEventSliceError(
                "live context lacks its frozen rollout reducer state"
            )
        named_slices = (
            *authority_read.local_named_slices,
            *child_named_slices,
        )
        checkpoint_snapshot = await runtime_session.subagent_graph_checkpoint_service.restore_for_selection(
            requested_through_sequence=full_slice.through_sequence
        )
        from pulsara_agent.runtime.long_horizon.checkpoint import (
            restore_subagent_graph_from_checkpoint,
        )

        (
            subagent_graph,
            subagent_graph_semantic_source,
            subagent_graph_acceleration,
        ) = restore_subagent_graph_from_checkpoint(
            snapshot=checkpoint_snapshot,
            reducer_binding=reducer_binding,
        )
        compile_policy = resolve_context_compile_policy(budget)
        source_selection = build_context_candidate_source_selections(
            subagent_graph=subagent_graph,
            semantic_source=subagent_graph_semantic_source,
            policy=compile_policy.candidate_collection,
        )[0]
        selected_results = tuple(
            subagent_graph.results[result_id]
            for result_id in source_selection.selected_source_ids
        )
        terminal_event_ids = tuple(
            result.provenance.terminal_event_id or "" for result in selected_results
        )
        if any(not event_id for event_id in terminal_event_ids):
            raise ContextEventSliceError(
                "selected subagent result lacks terminal event attribution"
            )
        result_deadline = monotonic() + 30.0
        raw_result_events = await runtime_session.context_input_io_service.execute(
            operation_name="subagent-result-authority-read",
            operation=lambda: runtime_session.event_log.read_raw_events_by_id(
                terminal_event_ids,
                deadline_monotonic=result_deadline,
            ),
            deadline_monotonic=result_deadline,
        )
        if len(raw_result_events) != len(terminal_event_ids):
            raise ContextEventSliceError(
                "selected subagent result terminal event is unavailable"
            )
        subagent_authority_events = tuple(
            FrozenStoredEvent.from_raw_envelope(raw) for raw in raw_result_events
        )
        for result, frozen in zip(
            selected_results, subagent_authority_events, strict=True
        ):
            event = frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if (
                frozen.sequence > full_slice.through_sequence
                or not isinstance(event, SubagentRunCompletedEvent)
                or event.result_id != result.result_id
                or event.subagent_run_id != result.subagent_run_id
                or event.summary != result.summary
                or event.result_artifact_id != result.final_message_artifact_id
                or tuple(event.artifact_ids) != result.artifact_ids
            ):
                raise ContextEventSliceError(
                    "selected subagent result differs from restored graph"
                )
    identity = ContextInputIdentityFact(
        snapshot_id=f"context_snapshot:{context_id}:{compile_attempt_index}",
        compiler_contract_version="context-compiler-input:v2",
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
        if memory_scope_instruction:
            instruction_sources.append(
                (
                    "memory_scope_instruction",
                    "pulsara-memory-scope-instruction:v1",
                    memory_scope_instruction,
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
                    operation=lambda source_id=source_id, contract_version=contract_version, content=content, static_deadline=static_deadline: (
                        build_static_instruction(
                            source_id=source_id,
                            contract_version=contract_version,
                            content=content,
                            archive=runtime_session.archive,
                            runtime_session_id=runtime_session.runtime_session_id,
                            run_id=start.run_id,
                            deadline_monotonic=static_deadline,
                        )
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
        source_artifact_expected_chars = {
            item.content_artifact_id: item.chars for item in static_instructions
        }
        for projection in (
            working_set.effective_exposure_fact.semantic.catalog_projection,
            working_set.effective_exposure_fact.semantic.active_skill_projection,
        ):
            if projection.rendered_prompt_artifact_id is not None:
                artifact_id = projection.rendered_prompt_artifact_id
                existing_chars = source_artifact_expected_chars.get(artifact_id)
                if (
                    existing_chars is not None
                    and existing_chars != projection.rendered_prompt_chars
                ):
                    raise ContextEventSliceError(
                        "ContextSource artifact character attribution conflicts"
                    )
                source_artifact_expected_chars[artifact_id] = (
                    projection.rendered_prompt_chars
                )
        source_artifact_metadata = await _read_context_source_artifact_metadata(
            runtime_session=runtime_session,
            expected_chars_by_artifact_id=source_artifact_expected_chars,
        )
        build_input = collect_live_context_inputs(
            working_set=working_set,
            resolved_call=resolved_call,
            event_slice=full_slice,
            identity=identity,
            timing=timing,
            compile_policy=compile_policy,
            static_instructions=tuple(static_instructions),
            runtime_environment=environment,
            tool_specs=tool_specs,
            subagent_graph=subagent_graph,
            subagent_graph_semantic_source=subagent_graph_semantic_source,
            subagent_graph_acceleration=subagent_graph_acceleration,
            subagent_authority_events=subagent_authority_events,
            source_artifact_metadata=source_artifact_metadata,
            authority_horizons=_merge_authority_horizons(
                authority_read.authority_horizons,
                child_authority.authority_horizons,
            ),
            named_slices=child_named_slices,
            raw_suspended_state_token_for_validation=(
                raw_suspended_state_token_for_validation
            ),
            rollout_status_override=_rollout_status_from_store(
                runtime_session=runtime_session,
                start=start,
                event_slice=full_slice,
                state=rollout_state,
            ),
            derive_rollout_status_from_events=False,
        )
        assert progress.component_fingerprints is not None
        progress.component_fingerprints["snapshot_draft"] = context_fingerprint(
            "context-snapshot-draft:v1", build_input
        )
    authority_slice = full_slice
    with _preparation_stage(
        progress,
        "transcript_normalization",
        ContextInputFailureReasonCode.TRANSCRIPT_INVALID,
    ):
        summary_text, window_source_document = await _read_compaction_inputs(
            runtime_session=runtime_session,
            transcript_window=build_input.authority_slice_plan.transcript_window,
        )
        projection_evidence = await runtime_session.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=full_slice.through_sequence
        )
        stable_entries = projection_evidence.stable_entries
        terminal_content_refs = required_terminal_content_artifacts(
            stable_entries=stable_entries,
            projection_window=build_input.authority_slice_plan.transcript_window,
            documents=projection_evidence.document_registry,
        )
        terminal_content_texts = await _read_terminal_content_artifacts(
            runtime_session=runtime_session,
            references=terminal_content_refs,
        )
        terminal_ref = (
            build_input.authority_slice_plan.transcript_window.compaction_terminal_ref
        )
        compaction_terminal = None
        if terminal_ref is not None:
            compaction_terminal = authority_slice.event_by_id(
                terminal_ref.event_id
            ).decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(
                compaction_terminal,
                ContextCompactionCompletedEvent | ContextWindowCompactionCompletedEvent,
            ):
                raise ContextEventSliceError(
                    "transcript compaction terminal has wrong event type"
                )
        normalized = project_stable_context_transcript(
            runtime_session_id=runtime_session.runtime_session_id,
            through_sequence=full_slice.through_sequence,
            current_user_anchor=start.current_user_message.message_id,
            projection_window=build_input.authority_slice_plan.transcript_window,
            stable_entries=stable_entries,
            documents=projection_evidence.document_registry,
            hydrated_message_contents=(projection_evidence.hydrated_message_contents),
            terminal_content_text_by_artifact_id=terminal_content_texts,
            compaction_summary_text=summary_text,
            compaction_terminal_event=compaction_terminal,
            window_compaction_source_document=window_source_document,
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
            policy_basis=build_input.compile_policy.tool_result_basis,
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
            snapshot=build_input,
            cache=runtime_session.context_candidate_lifecycle_cache,
        )
        selected_artifact_metadata = {
            artifact_id: source_artifact_metadata[artifact_id]
            for entry in prepared_candidates.prepared.entries
            for artifact_id in entry.candidate.source_artifact_ids
        }
        hydrated_source_artifacts = await _hydrate_context_source_artifacts(
            runtime_session=runtime_session,
            artifact_metadata=selected_artifact_metadata,
            physical_policy=build_input.context_source_physical_input_policy,
        )
        context_source_hydrated_contents = hydrate_context_source_content_sidecar(
            candidates=tuple(
                entry.candidate for entry in prepared_candidates.prepared.entries
            ),
            hydrated_artifacts=hydrated_source_artifacts,
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
        prepared_named_fact_artifacts = await _prepare_named_fact_artifacts(
            runtime_session=runtime_session,
            prepared_candidates=prepared_candidates.prepared,
        )
    return PreparedLiveContextSnapshot(
        invocation=bind_context_draft(
            build_input=build_input,
            resolved_call=resolved_call,
            materialized_tool_specs=materialized,
        ),
        snapshot_build_input=build_input,
        authority_slice=authority_slice,
        named_slices=named_slices,
        exact_named_authority_events=subagent_authority_events,
        normalized_transcript=normalized,
        prepared_tool_results=prepared_tool_results,
        prepared_candidates=prepared_candidates.prepared,
        context_source_hydrated_contents=context_source_hydrated_contents,
        transcript_projection_evidence=projection_evidence,
        prepared_named_fact_artifacts=prepared_named_fact_artifacts,
        candidate_cache_writes=prepared_candidates.cache_writes,
        active_window=active_window,
        projection_state=projection_state,
        rollout_state=rollout_state,
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

    authority_read = await _read_live_primary_event_slice(
        runtime_session=runtime_session,
        working_set=working_set,
    )
    full_slice = authority_read.view
    start = authority_read.run_start
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
            start.run_transcript_seed_reference.source_ledger_through_sequence
        ),
    )
    authority_slice = full_slice
    summary_text, window_source_document = await _read_compaction_inputs(
        runtime_session=runtime_session,
        transcript_window=authority_plan.transcript_window,
    )
    projection_evidence = await runtime_session.transcript_projection_checkpoint_service.prepare_projection_evidence(
        requested_through_sequence=full_slice.through_sequence
    )
    stable_entries = projection_evidence.stable_entries
    terminal_content_refs = required_terminal_content_artifacts(
        stable_entries=stable_entries,
        projection_window=authority_plan.transcript_window,
        documents=projection_evidence.document_registry,
    )
    terminal_content_texts = await _read_terminal_content_artifacts(
        runtime_session=runtime_session,
        references=terminal_content_refs,
    )
    terminal_ref = authority_plan.transcript_window.compaction_terminal_ref
    compaction_terminal = None
    if terminal_ref is not None:
        compaction_terminal = authority_slice.event_by_id(
            terminal_ref.event_id
        ).decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(
            compaction_terminal,
            ContextCompactionCompletedEvent | ContextWindowCompactionCompletedEvent,
        ):
            raise ContextEventSliceError(
                "transcript compaction terminal has wrong event type"
            )
    normalized = project_stable_context_transcript(
        runtime_session_id=runtime_session.runtime_session_id,
        through_sequence=full_slice.through_sequence,
        current_user_anchor=start.current_user_message.message_id,
        projection_window=authority_plan.transcript_window,
        stable_entries=stable_entries,
        documents=projection_evidence.document_registry,
        hydrated_message_contents=(projection_evidence.hydrated_message_contents),
        terminal_content_text_by_artifact_id=terminal_content_texts,
        compaction_summary_text=summary_text,
        compaction_terminal_event=compaction_terminal,
        window_compaction_source_document=window_source_document,
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
        transcript_projection_evidence=projection_evidence,
    )


async def _prepare_named_fact_artifacts(
    *,
    runtime_session: RuntimeSession,
    prepared_candidates: PreparedContextCandidateSet,
) -> tuple[PreparedNamedFactArtifact, ...]:
    artifact_ids = tuple(
        sorted(
            {
                artifact_id
                for entry in prepared_candidates.entries
                for artifact_id in entry.candidate.source_artifact_ids
            }
        )
    )
    if not artifact_ids:
        return ()
    deadline = monotonic() + 30.0

    def read() -> tuple[PreparedNamedFactArtifact, ...]:
        prepared: list[PreparedNamedFactArtifact] = []
        total_bytes = 0
        for artifact_id in artifact_ids:
            record = runtime_session.archive.get_info(
                artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            )
            if record.media_type.startswith("text/") or record.media_type in {
                "application/json",
                "application/xml",
            }:
                canonical_bytes = runtime_session.archive.get_text(
                    artifact_id,
                    session_id=runtime_session.runtime_session_id,
                    deadline_monotonic=deadline,
                ).encode("utf-8")
            else:
                canonical_bytes = runtime_session.archive.get_bytes(
                    artifact_id,
                    session_id=runtime_session.runtime_session_id,
                )
            total_bytes += len(canonical_bytes)
            if total_bytes > _MAX_LIVE_SPARSE_AUTHORITY_PAYLOAD_BYTES:
                raise ContextEventSliceError(
                    "named-fact artifact authority exceeds bounded preparation bytes"
                )
            prepared.append(
                prepare_named_fact_artifact(
                    record=record,
                    canonical_bytes=canonical_bytes,
                )
            )
        return tuple(prepared)

    return await runtime_session.context_input_io_service.execute(
        operation_name="context-named-fact-artifact-read",
        operation=read,
        deadline_monotonic=deadline,
    )


async def _read_context_source_artifact_metadata(
    *,
    runtime_session: RuntimeSession,
    expected_chars_by_artifact_id: dict[str, int],
) -> dict[str, ContextSourceArtifactMetadata]:
    if not expected_chars_by_artifact_id:
        return {}
    deadline = monotonic() + 30.0

    def read() -> dict[str, ContextSourceArtifactMetadata]:
        metadata: dict[str, ContextSourceArtifactMetadata] = {}
        for artifact_id in sorted(expected_chars_by_artifact_id):
            record = runtime_session.archive.get_info(
                artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            )
            if not (
                record.media_type.startswith("text/")
                or record.media_type in {"application/json", "application/xml"}
            ):
                raise ContextEventSliceError(
                    "ContextSource prose artifact is not text material"
                )
            reference = build_frozen_fact(
                ContextArtifactReferenceFact,
                schema_version="context_artifact_reference.v1",
                artifact_id=record.id,
                media_type=record.media_type,
                content_sha256=record.digest,
                content_bytes=record.size_bytes,
                artifact_contract_fingerprint=context_fingerprint(
                    "context-source-artifact-contract:v1", record.media_type
                ),
            )
            metadata[artifact_id] = ContextSourceArtifactMetadata(
                reference=reference,
                expected_chars=expected_chars_by_artifact_id[artifact_id],
            )
        return metadata

    return await runtime_session.context_input_io_service.execute(
        operation_name="context-source-artifact-metadata-read",
        operation=read,
        deadline_monotonic=deadline,
    )


async def _hydrate_context_source_artifacts(
    *,
    runtime_session: RuntimeSession,
    artifact_metadata: dict[str, ContextSourceArtifactMetadata],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> dict[str, HydratedContextSourceArtifact]:
    if not artifact_metadata:
        return {}
    selected_bytes = sum(
        item.reference.content_bytes for item in artifact_metadata.values()
    )
    if selected_bytes > physical_policy.max_hydrated_working_set_bytes:
        raise ContextEventSliceError(
            "selected ContextSource artifacts exceed resolved physical policy"
        )
    deadline = monotonic() + 30.0

    def read() -> dict[str, HydratedContextSourceArtifact]:
        hydrated: dict[str, HydratedContextSourceArtifact] = {}
        page_chars = max(1, physical_policy.artifact_page_bytes // 4)
        for artifact_id in sorted(artifact_metadata):
            expected = artifact_metadata[artifact_id]
            offset = 0
            parts: list[str] = []
            while True:
                if monotonic() >= deadline:
                    raise TimeoutError("ContextSource artifact hydration timed out")
                page = runtime_session.archive.read_text(
                    artifact_id,
                    session_id=runtime_session.runtime_session_id,
                    offset_chars=offset,
                    max_chars=page_chars,
                )
                if (
                    page.artifact.id != expected.reference.artifact_id
                    or page.artifact.digest != expected.reference.content_sha256
                    or page.artifact.size_bytes != expected.reference.content_bytes
                ):
                    raise ContextEventSliceError(
                        "ContextSource artifact changed during paged hydration"
                    )
                if page.offset_chars != offset or page.returned_chars != len(page.text):
                    raise ContextEventSliceError(
                        "ContextSource artifact page cursor drifted"
                    )
                if not page.text and page.has_more:
                    raise ContextEventSliceError(
                        "ContextSource artifact page made no progress"
                    )
                parts.append(page.text)
                offset += page.returned_chars
                if not page.has_more:
                    if page.total_chars not in {None, offset}:
                        raise ContextEventSliceError(
                            "ContextSource artifact total character count drifted"
                        )
                    break
            hydrated[artifact_id] = HydratedContextSourceArtifact(
                reference=expected.reference,
                expected_chars=expected.expected_chars,
                text="".join(parts),
            )
        return hydrated

    return await runtime_session.context_input_io_service.execute(
        operation_name="context-source-selected-artifact-read",
        operation=read,
        deadline_monotonic=deadline,
    )


async def _read_compaction_inputs(
    *,
    runtime_session: RuntimeSession,
    transcript_window,
) -> tuple[str | None, WindowCompactionSourceDocumentFact | None]:
    async def read_text(artifact_id: str, *, operation_name: str) -> str:
        deadline = monotonic() + 30.0
        return await runtime_session.context_input_io_service.execute(
            operation_name=operation_name,
            operation=lambda: runtime_session.archive.get_text(
                artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    summary_text = None
    if transcript_window.compaction_summary_artifact_id is not None:
        summary_text = await read_text(
            transcript_window.compaction_summary_artifact_id,
            operation_name="context-compaction-summary-read",
        )
    source_document = None
    source_id = transcript_window.window_compaction_source_document_artifact_id
    if source_id is not None:
        source_text = await read_text(
            source_id,
            operation_name="window-compaction-source-document-read",
        )
        source_document = WindowCompactionSourceDocumentFact.model_validate(
            json.loads(source_text)
        )
        if (
            source_document.document_fingerprint
            != transcript_window.window_compaction_source_document_fingerprint
        ):
            raise ValueError("window compaction source artifact fingerprint mismatch")
    return summary_text, source_document


async def _read_terminal_content_artifacts(
    *,
    runtime_session: RuntimeSession,
    references,
) -> dict[str, str]:
    if not references:
        return {}
    deadline = monotonic() + 30.0

    def read() -> dict[str, str]:
        prepared: dict[str, str] = {}
        for reference in references:
            info = runtime_session.archive.get_info(
                reference.artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            )
            if (
                info.digest != reference.artifact_sha256
                or info.size_bytes != reference.artifact_bytes
                or info.media_type != reference.media_type
            ):
                raise ContextEventSliceError(
                    "terminal content artifact identity drifted"
                )
            prepared[reference.artifact_id] = runtime_session.archive.get_text(
                reference.artifact_id,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            )
        return prepared

    return await runtime_session.context_input_io_service.execute(
        operation_name="terminal-projection-content-read",
        operation=read,
        deadline_monotonic=deadline,
    )


async def _read_live_primary_event_slice(
    *,
    runtime_session: RuntimeSession,
    working_set: RunWorkingSet,
) -> _LiveAuthorityRead:
    """Read one bounded active-window delta plus exact historical authority."""

    if runtime_session.reconciliation_required:
        raise ContextEventSliceError(
            "event ledger requires reconciliation before context read"
        )
    projection_delta_minimum_sequence = await runtime_session.transcript_projection_checkpoint_service.projection_delta_minimum_sequence()
    deadline = monotonic() + 30.0

    def read() -> _LiveAuthorityRead:
        start = runtime_session.long_horizon_state_store.run_start_by_event_id(
            working_set.run_start_event_id
        )
        if start is None:
            run_start_rows = runtime_session.event_log.read_raw_events_by_id(
                (working_set.run_start_event_id,),
                deadline_monotonic=deadline,
            )
            if len(run_start_rows) != 1:
                raise ContextEventSliceError("live snapshot RunStart is not durable")
            decoded_start = run_start_rows[0].decode_owned(
                DEFAULT_EVENT_SCHEMA_REGISTRY
            )
            start = decoded_start if isinstance(decoded_start, RunStartEvent) else None
        if (
            not isinstance(start, RunStartEvent)
            or start.id != working_set.run_start_event_id
            or start.sequence != working_set.run_start_sequence
        ):
            raise ContextEventSliceError("live snapshot RunStart identity drifted")
        minimum_sequence = projection_delta_minimum_sequence
        if minimum_sequence < 1:
            raise ContextEventSliceError(
                "transcript projection delta minimum sequence is invalid"
            )
        compacted_window = False
        compacted_source_through: int | None = None
        window_state = runtime_session.long_horizon_state_store.window_state(
            start.run_id
        )
        if window_state is not None:
            if not window_state.consistent:
                raise ContextEventSliceError(
                    "active context window state is inconsistent"
                )
            active_window = (
                window_state.windows.get(window_state.active_window_id)
                if window_state.active_window_id is not None
                else None
            )
            if (
                active_window is not None
                and active_window.transcript_basis.basis_kind == "window_compaction"
            ):
                source_through = (
                    active_window.transcript_basis.source_through_sequence_at_compaction
                )
                if source_through is None:
                    raise ContextEventSliceError(
                        "compacted window has no bounded post-compaction delta"
                    )
                minimum_sequence = max(minimum_sequence, source_through + 1)
                compacted_window = True
                compacted_source_through = source_through
        boundary = start.new_run_boundary
        checkpoint_terminal_id: str | None = None
        if (
            not compacted_window
            and boundary is not None
            and boundary.transcript.source_through_sequence > 0
        ):
            terminal_id = boundary.transcript.checkpoint_terminal_event_id
            if terminal_id is not None:
                checkpoint_terminal_id = terminal_id
                keep_after = boundary.transcript.checkpoint_keep_after_sequence
                terminal_sequence = boundary.transcript.checkpoint_terminal_sequence
                if keep_after is None or terminal_sequence is None:
                    raise ContextEventSliceError(
                        "transcript checkpoint boundary is incomplete"
                    )
        cache_key = (
            runtime_session.runtime_session_id,
            working_set.run_start_event_id,
            minimum_sequence,
        )
        cached = runtime_session.context_authority_slice_cache.get(cache_key)
        remaining_events = (
            _MAX_LIVE_AUTHORITY_EVENTS - len(cached.events)
            if cached is not None
            else _MAX_LIVE_AUTHORITY_EVENTS
        )
        remaining_bytes = (
            _MAX_LIVE_AUTHORITY_PAYLOAD_BYTES - cached.payload_byte_count
            if cached is not None
            else _MAX_LIVE_AUTHORITY_PAYLOAD_BYTES
        )
        exact_ids = {
            working_set.run_start_event_id,
            *((checkpoint_terminal_id,) if checkpoint_terminal_id is not None else ()),
            *(
                (working_set.effective_exposure_event_ref.event_id,)
                if working_set.effective_exposure_event_ref is not None
                else ()
            ),
            *(
                (working_set.plan_snapshot.entered_event_id,)
                if working_set.plan_snapshot.entered_event_id is not None
                else ()
            ),
            *(
                (working_set.latest_committed_resume_boundary_ref.event_id,)
                if working_set.latest_committed_resume_boundary_ref is not None
                else ()
            ),
        }
        bundle_request = RawContextAuthorityBundleRequest(
            primary_minimum_sequence=(
                cached.through_sequence + 1 if cached is not None else minimum_sequence
            ),
            run_id=start.run_id,
            run_sparse_event_types=_LIVE_RUN_SPARSE_EVENT_TYPES,
            session_sparse_event_types=_LIVE_SESSION_SPARSE_EVENT_TYPES,
            exact_event_ids=tuple(sorted(exact_ids)),
            primary_bounds=RawEventSelectionBounds(
                max_events=max(1, remaining_events),
                max_payload_bytes=max(1, remaining_bytes),
            ),
            run_sparse_bounds=RawEventSelectionBounds(
                max_events=_MAX_LIVE_SPARSE_AUTHORITY_EVENTS,
                max_payload_bytes=_MAX_LIVE_SPARSE_AUTHORITY_PAYLOAD_BYTES,
            ),
            session_sparse_bounds=RawEventSelectionBounds(
                max_events=_MAX_LIVE_SPARSE_AUTHORITY_EVENTS,
                max_payload_bytes=_MAX_LIVE_SPARSE_AUTHORITY_PAYLOAD_BYTES,
            ),
            exact_bounds=RawEventSelectionBounds(
                max_events=max(1, len(exact_ids)),
                max_payload_bytes=_MAX_LIVE_SPARSE_AUTHORITY_PAYLOAD_BYTES,
            ),
        )
        bundle = runtime_session.event_log.read_context_authority_bundle(
            bundle_request,
            deadline_monotonic=deadline,
        )
        high_water = bundle.through_sequence
        if compacted_window and (
            compacted_source_through is None or compacted_source_through >= high_water
        ):
            raise ContextEventSliceError(
                "compacted window has no bounded post-compaction delta"
            )
        if cached is not None:
            if cached.through_sequence > high_water:
                raise ContextEventSliceError(
                    "authority slice cache exceeds canonical ledger high-water"
                )
            if cached.through_sequence == high_water:
                authority_slice = cached
            else:
                if remaining_events < 1 or remaining_bytes < 1:
                    raise ContextEventSliceError(
                        "authority slice cache reached its hard read bound"
                    )
                delta = _raw_event_snapshot(
                    through_sequence=high_water,
                    events=bundle.primary_events,
                )
                authority_slice = cached.extend_snapshot(delta)
        else:
            if not bundle.primary_events:
                raise ContextEventSliceError("authority bundle primary range is empty")
            snapshot = _raw_event_snapshot(
                through_sequence=high_water,
                events=bundle.primary_events,
            )
            authority_slice = ContextEventSlice.from_read_snapshot(
                runtime_session_id=runtime_session.runtime_session_id,
                minimum_sequence=minimum_sequence,
                snapshot=snapshot,
            )
        runtime_session.context_authority_slice_cache.put(cache_key, authority_slice)
        local_named_slices: tuple[ContextEventSlice, ...] = ()
        bundle_by_id = {
            item.event_id: item
            for item in (
                *bundle.run_sparse_events,
                *bundle.session_sparse_events,
                *bundle.exact_events,
            )
        }
        if exact_ids - set(bundle_by_id):
            raise ContextEventSliceError(
                "compacted window exact authority is unavailable"
                if compacted_window
                else "live context exact authority is unavailable"
            )
        if checkpoint_terminal_id is not None:
            terminal = bundle_by_id[checkpoint_terminal_id].decode_owned(
                DEFAULT_EVENT_SCHEMA_REGISTRY
            )
            if not isinstance(terminal, ContextCompactionCompletedEvent):
                raise ContextEventSliceError(
                    "transcript checkpoint reference is not a completed fact"
                )
            if (
                terminal.sequence != boundary.transcript.checkpoint_terminal_sequence
                or terminal.compaction_id
                != boundary.transcript.checkpoint_compaction_id
                or terminal.keep_after_sequence
                != boundary.transcript.checkpoint_keep_after_sequence
                or terminal.window_id != boundary.transcript.compacted_window_id
            ):
                raise ContextEventSliceError(
                    "transcript checkpoint basis drifted from RunStart"
                )
        sparse_by_id = {
            item.event_id: item
            for item in (
                *bundle.run_sparse_events,
                *bundle.session_sparse_events,
                *bundle.exact_events,
            )
        }
        frozen = tuple(
            FrozenStoredEvent.from_raw_envelope(item)
            for item in sorted(sparse_by_id.values(), key=lambda item: item.sequence)
            if item.sequence < authority_slice.from_sequence
        )
        local_named_slices = _contiguous_exact_slices(
            runtime_session_id=runtime_session.runtime_session_id,
            events=frozen,
        )
        return _LiveAuthorityRead(
            primary_slice=authority_slice,
            local_named_slices=local_named_slices,
            run_start=start,
            authority_horizons=(
                _ledger_authority_horizon(
                    runtime_session_id=runtime_session.runtime_session_id,
                    prefix=bundle.ledger_prefix,
                ),
            ),
        )

    authority = await runtime_session.context_input_io_service.execute(
        operation_name="context-live-authority-read",
        operation=read,
        deadline_monotonic=deadline,
    )
    if runtime_session.reconciliation_required:
        raise ContextEventSliceError(
            "event ledger requires reconciliation after context read"
        )
    return authority


def _contiguous_exact_slices(
    *,
    runtime_session_id: str,
    events: tuple[FrozenStoredEvent, ...],
) -> tuple[ContextEventSlice, ...]:
    if not events:
        return ()
    groups: list[list[FrozenStoredEvent]] = []
    for event in events:
        if groups and event.sequence == groups[-1][-1].sequence + 1:
            groups[-1].append(event)
        else:
            groups.append([event])
    return tuple(
        ContextEventSlice.from_frozen_events(
            runtime_session_id=runtime_session_id,
            events=group,
        )
        for group in groups
    )


async def _child_named_context_slices(
    *,
    runtime_session: RuntimeSession,
    run_start: RunStartEvent,
) -> _ChildAuthorityRead:
    entry = run_start.subagent_run_entry
    if entry is None:
        return _ChildAuthorityRead(
            slices=(),
            rollout_state=None,
            authority_horizons=(),
        )
    locator = runtime_session.context_event_log_locator
    if locator is None:
        raise ContextEventSliceError("child context requires a parent EventLogLocator")
    parent_log = locator.event_log_for_runtime_session(entry.parent_runtime_session_id)
    deadline = monotonic() + 30.0
    owner_store = runtime_session.rollout_account_owner_state_store
    if owner_store is None:
        raise ContextEventSliceError(
            "child context lacks its parent rollout state store"
        )
    owner_through_sequence, rollout_state = owner_store.rollout_state_snapshot(
        run_start.long_horizon.rollout_account_id
    )
    if rollout_state is None:
        raise ContextEventSliceError(
            "child context parent rollout account is unavailable"
        )

    def read_parent_authority() -> _ChildAuthorityRead:
        parent_prefix = parent_log.read_raw_ledger_prefix(
            through_sequence=owner_through_sequence,
            deadline_monotonic=deadline,
        )
        sparse_key = (
            entry.parent_runtime_session_id,
            f"parent-run:{entry.parent_run_id}:child:{entry.subagent_run_id}",
        )
        cursor = runtime_session.context_authority_slice_cache.get_sparse_cursor(
            sparse_key
        )
        relevant = parent_log.read_raw_events_by_types(
            (
                EventType.RUN_START.value,
                EventType.SUBAGENT_RUN_STARTED.value,
                EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED.value,
                EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED.value,
                EventType.ROLLOUT_BUDGET_RESERVATION_CREATED.value,
                EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED.value,
                EventType.ROLLOUT_PHASE_TRANSITIONED.value,
            ),
            run_ids=(entry.parent_run_id,),
            minimum_sequence=(
                cursor.observed_ledger_high_water + 1 if cursor is not None else 1
            ),
            through_sequence=owner_through_sequence,
            max_events=_MAX_LIVE_AUTHORITY_EVENTS,
            max_payload_bytes=_MAX_LIVE_AUTHORITY_PAYLOAD_BYTES,
            deadline_monotonic=deadline,
        )
        if cursor is None:
            starts = tuple(
                event
                for event in relevant.events
                if event.event_type == EventType.RUN_START
            )
            if len(starts) != 1:
                raise ContextEventSliceError(
                    "child parent ledger lacks its unique RunStart"
                )
            spawn = tuple(
                event
                for event in relevant.events
                if event.event_type == EventType.SUBAGENT_RUN_STARTED
                and event.event_id == f"subagent_run_started:{entry.subagent_run_id}"
            )
            if len(spawn) != 1:
                # The fallback validates the complete typed spawn identity; it
                # does not widen the sparse authority selection.
                spawn = tuple(
                    raw
                    for raw in relevant.events
                    if raw.event_type == EventType.SUBAGENT_RUN_STARTED
                    and isinstance(
                        decoded := raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
                        SubagentRunStartedEvent,
                    )
                    and decoded.subagent_run_id == entry.subagent_run_id
                    and decoded.edge_id == entry.spawn_edge_id
                )
            if len(spawn) != 1:
                raise ContextEventSliceError(
                    "child parent ledger lacks its unique spawn fact"
                )
            run_start_event = FrozenStoredEvent.from_raw_envelope(starts[0])
            spawn_event = FrozenStoredEvent.from_raw_envelope(spawn[0])
            relevant_through_sequence = max(
                starts[0].sequence,
                spawn[0].sequence,
                *(event.sequence for event in relevant.events),
            )
        else:
            run_start_event = cursor.run_start_event
            spawn_event = cursor.spawn_event
            relevant_through_sequence = _advance_sparse_relevant_through_sequence(
                cursor.relevant_through_sequence,
                relevant.events,
            )
        relevant_events_by_id = (
            {item.event_id: item for item in cursor.relevant_events}
            if cursor is not None
            else {}
        )
        relevant_events_by_id.update(
            (
                item.event_id,
                FrozenStoredEvent.from_raw_envelope(item),
            )
            for item in relevant.events
        )
        relevant_events_by_id[run_start_event.event_id] = run_start_event
        relevant_events_by_id[spawn_event.event_id] = spawn_event
        frozen_relevant = tuple(
            sorted(relevant_events_by_id.values(), key=lambda item: item.sequence)
        )
        runtime_session.context_authority_slice_cache.put_sparse_cursor(
            sparse_key,
            SparseAuthorityCursor(
                observed_ledger_high_water=relevant.through_sequence,
                relevant_through_sequence=relevant_through_sequence,
                run_start_event=run_start_event,
                spawn_event=spawn_event,
                relevant_events=frozen_relevant,
            ),
        )
        return _ChildAuthorityRead(
            slices=_contiguous_exact_slices(
                runtime_session_id=entry.parent_runtime_session_id,
                events=frozen_relevant,
            ),
            rollout_state=rollout_state,
            authority_horizons=(
                _ledger_authority_horizon(
                    runtime_session_id=entry.parent_runtime_session_id,
                    prefix=parent_prefix,
                ),
            ),
        )

    read = await runtime_session.context_input_io_service.execute(
        operation_name="context-child-parent-event-slice-read",
        operation=read_parent_authority,
        deadline_monotonic=deadline,
    )
    if not read.slices:
        raise ContextEventSliceError("child parent ledger is empty")
    parent_slices = read.slices
    matching = tuple(
        event
        for parent_slice in parent_slices
        for frozen in parent_slice.events
        if isinstance(
            (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
            SubagentRunStartedEvent,
        )
        and event.subagent_run_id == entry.subagent_run_id
        and event.child_runtime_session_id == runtime_session.runtime_session_id
        and event.edge_id == entry.spawn_edge_id
        and event.parent_run_id == entry.parent_run_id
    )
    if len(matching) != 1:
        raise ContextEventSliceError(
            "child context parent slice lacks its unique spawn fact"
        )
    return read


def _advance_sparse_relevant_through_sequence(
    current: int,
    events: Iterable[RawStoredEventEnvelope],
) -> int:
    return max((event.sequence for event in events), default=current)


def _raw_event_snapshot(
    *,
    through_sequence: int,
    events: tuple[RawStoredEventEnvelope, ...],
) -> RawEventLogReadSnapshot:
    frozen = tuple(events)
    return RawEventLogReadSnapshot(
        through_sequence=through_sequence,
        events=frozen,
        snapshot_fingerprint=context_fingerprint(
            "raw-event-log-read-snapshot:v1",
            {
                "through_sequence": through_sequence,
                "envelopes": tuple(item.envelope_fingerprint for item in frozen),
            },
        ),
    )


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
