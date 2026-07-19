"""Composition-root builder for authorized ContextSource inputs.

This module is the only ContextSource component allowed to inspect the set of
already-frozen context facts.  Source bindings receive the narrow dataclasses
from ``sources.input`` and remain storage/network free.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from pulsara_agent.event import (
    EventType,
    PlanExitResolvedEvent,
    ProjectionReadyEvent,
    SubagentRunCompletedEvent,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.context import (
    ContextCandidateSourceSelectionFact,
    ContextCompileTimingFact,
    ContextPlanSnapshotFact,
    ContextProjectionReferenceFact,
    ContextRuntimeEnvironmentFact,
    ContextSectionCandidate,
    ContextStaticInstructionFact,
    ContextToolSpecFact,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    ActiveSkillPayloadFact,
    AppendOnceLifecycleFact,
    AppendRevisionLifecycleFact,
    ArtifactContextSourceContentSemanticFact,
    CapabilityCatalogPayloadFact,
    CapabilityToolCatalogRootFact,
    ContextArtifactReferenceFact,
    ContextCandidateLoweringIntentFact,
    ContextSourceAbsoluteTimingFact,
    ContextSourceContractFact,
    ContextSourceId,
    ContextSourceInputAuthorityFact,
    EventSourceRevisionFact,
    GenerationRootLifecycleFact,
    ImmutableSourceRevisionFact,
    InlineContextSourceContentSemanticFact,
    LedgerAuthorityHorizonFact,
    LedgerSequenceRangeFact,
    MemoryInstructionPayloadFact,
    MemoryProjectionPayloadFact,
    PlanRevisionPayloadFact,
    ResolvedContextSourcePhysicalInputPolicyFact,
    RolloutStatusPayloadFact,
    RuntimeClockProposalPayloadFact,
    RuntimeEnvironmentPayloadFact,
    SubagentResultPayloadFact,
    SystemInstructionPayloadFact,
    context_source_payload_content,
    raw_content_sha256,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.long_horizon import (
    LongHorizonRolloutStatusCandidateFact,
)
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    FrozenStoredEvent,
)
from pulsara_agent.runtime.context_input.sources.input import (
    ActiveSkillSourceInput,
    CapabilityCatalogSourceInput,
    ContextSourceCollectInput,
    MemoryInstructionSourceInput,
    MemoryProjectionSourceInput,
    PlanSourceInput,
    RolloutStatusSourceInput,
    RuntimeClockSourceInput,
    RuntimeEnvironmentSourceInput,
    SubagentResultSourceInput,
    SystemSourceInput,
)
from pulsara_agent.runtime.context_input.sources.registry import (
    CanonicalContextSource,
    ContextSourceBinding,
    ContextSourceRegistry,
    default_context_source_binding_policy,
)
from pulsara_agent.runtime.context_input.sources.input import (
    context_source_input_dependency_fingerprint,
)
from pulsara_agent.runtime.long_horizon.status import (
    render_rollout_status_candidate,
    rollout_status_candidate_is_required,
)
from pulsara_agent.runtime.plan import PLAN_ACTIVE_INSTRUCTION


@dataclass(frozen=True, slots=True)
class ContextSourceArtifactMetadata:
    reference: ContextArtifactReferenceFact
    expected_chars: int

    def __post_init__(self) -> None:
        if self.expected_chars < 0:
            raise ValueError("ContextSource artifact character count is invalid")


@dataclass(frozen=True, slots=True)
class HydratedContextSourceArtifact:
    reference: ContextArtifactReferenceFact
    expected_chars: int
    text: str

    def __post_init__(self) -> None:
        encoded = self.text.encode("utf-8")
        if (
            self.expected_chars != len(self.text)
            or self.reference.content_bytes != len(encoded)
            or self.reference.content_sha256 != raw_content_sha256(encoded)
        ):
            raise ValueError("hydrated ContextSource artifact identity mismatch")


@dataclass(frozen=True, slots=True)
class ContextSourceBuildResult:
    candidates: tuple[ContextSectionCandidate, ...]
    tool_catalog_root: CapabilityToolCatalogRootFact
    physical_input_policy: ResolvedContextSourcePhysicalInputPolicyFact
    registry_fingerprint: str


def hydrate_context_source_content_sidecar(
    *,
    candidates: tuple[ContextSectionCandidate, ...],
    hydrated_artifacts: Mapping[str, HydratedContextSourceArtifact],
) -> tuple[tuple[str, str], ...]:
    """Bind artifact semantic content to this compile's already-bounded reads."""

    resolved: dict[str, str] = {}
    for candidate in candidates:
        content = context_source_payload_content(candidate.attribution.semantic.payload)
        if not isinstance(content, ArtifactContextSourceContentSemanticFact):
            continue
        matches = tuple(
            artifact.text
            for artifact in hydrated_artifacts.values()
            if artifact.reference.content_sha256 == content.content_sha256
            and artifact.reference.content_bytes == content.expected_utf8_bytes
        )
        if not matches or len(set(matches)) != 1:
            raise ValueError(
                "artifact ContextSource content cannot be uniquely hydrated"
            )
        text = matches[0]
        if len(text) != content.expected_chars:
            raise ValueError("artifact ContextSource character count drifted")
        resolved[content.semantic_fingerprint] = text
    return tuple(sorted(resolved.items()))


def resolved_context_source_physical_policy(
    resolved_call: ResolvedModelCallFact,
) -> ResolvedContextSourcePhysicalInputPolicyFact:
    input_limit = resolved_call.target.context_budget.input_budget_tokens
    # Every provider unit has non-zero framing cost under the frozen estimator.
    # Deriving this from the resolved input budget avoids a smaller, unrelated
    # item-count window for models whose token window exceeds 8K units.
    max_units = input_limit
    utf8_bytes = input_limit * 16
    canonical_bytes = utf8_bytes * 2 + max_units * 512
    return build_frozen_fact(
        ResolvedContextSourcePhysicalInputPolicyFact,
        schema_version="resolved_context_source_physical_input_policy.v1",
        resolved_model_input_token_limit=input_limit,
        resolved_max_provider_input_units=max_units,
        tokenizer_or_estimator_contract_fingerprint=(
            resolved_call.target.token_estimator.estimator_fingerprint
        ),
        canonical_codec_contract_fingerprint=context_fingerprint(
            "context-source-canonical-codec-contract:v1", "utf8-canonical-json"
        ),
        conservative_utf8_bytes_per_token_numerator=16,
        conservative_utf8_bytes_per_token_denominator=1,
        canonical_encoding_expansion_numerator=2,
        canonical_encoding_expansion_denominator=1,
        structural_overhead_bytes_per_unit=512,
        max_token_budget_admissible_utf8_bytes=utf8_bytes,
        max_canonical_materialization_bytes=canonical_bytes,
        max_inline_item_utf8_bytes=min(65_536, utf8_bytes),
        max_hydrated_working_set_bytes=canonical_bytes,
        max_source_entries=max_units,
        artifact_page_bytes=65_536,
    )


@lru_cache(maxsize=1)
def default_context_source_registry() -> ContextSourceRegistry:
    bindings: list[ContextSourceBinding] = []
    for source_id in ContextSourceId:
        policy = default_context_source_binding_policy(source_id)
        contract = build_frozen_fact(
            ContextSourceContractFact,
            schema_version="context_source_contract.v1",
            source_id=source_id,
            source_version="2",
            binding_policy_fingerprint=policy.policy_fingerprint,
            lifecycle_contract_fingerprint=context_fingerprint(
                "context-source-lifecycle-contract:v1", source_id.value
            ),
            selection_contract_fingerprint=context_fingerprint(
                "context-source-selection-contract:v1", source_id.value
            ),
            lowering_intent_contract_fingerprint=context_fingerprint(
                "context-source-lowering-contract:v2",
                {
                    "source_id": source_id.value,
                    "append_revision_envelope": "context-source-revision:v1",
                    "dynamic_system_policy": "lower-as-leading-context:v1",
                },
            ),
        )
        source = CanonicalContextSource(contract, policy)
        bindings.append(
            ContextSourceBinding(
                contract=contract,
                policy=policy,
                implementation_build_fingerprint=None,
                source=source,
            )
        )
    return ContextSourceRegistry(tuple(bindings))


def build_context_sources(
    *,
    registry: ContextSourceRegistry,
    static_instructions: tuple[ContextStaticInstructionFact, ...],
    artifact_metadata: Mapping[str, ContextSourceArtifactMetadata],
    projections: tuple[ContextProjectionReferenceFact, ...],
    capability_snapshot: CapabilityExposureSnapshotFact,
    plan_snapshot: ContextPlanSnapshotFact,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    runtime_environment: ContextRuntimeEnvironmentFact,
    compile_timing: ContextCompileTimingFact,
    resolved_model_call: ResolvedModelCallFact,
    source_selections: tuple[ContextCandidateSourceSelectionFact, ...],
    rollout_status: LongHorizonRolloutStatusCandidateFact | None,
    external_authority_events: Mapping[str, FrozenStoredEvent],
    tool_specs: tuple[ContextToolSpecFact, ...],
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
) -> ContextSourceBuildResult:
    physical_policy = resolved_context_source_physical_policy(resolved_model_call)
    horizons = authority_horizons
    owners = tuple(item.runtime_session_id for item in horizons)
    if owners != tuple(sorted(set(owners))):
        raise ValueError("ContextSource canonical horizons are not ordered/unique")
    if event_slice.runtime_session_id not in set(owners):
        raise ValueError("ContextSource primary ledger horizon is absent")
    static_by_id = {item.source_id: item for item in static_instructions}
    projection_by_kind = {item.projection_kind: item for item in projections}
    inputs: list[ContextSourceCollectInput] = []

    system = static_by_id.get("base_system_instruction")
    if system is None:
        raise ValueError("ContextSource system instruction is absent")
    system_artifact = _require_artifact(artifact_metadata, system.content_artifact_id)
    system_content = _artifact_content(system_artifact)
    system_payload = build_frozen_fact(
        SystemInstructionPayloadFact,
        schema_version="system_instruction_payload.v1",
        instruction_source_id=system.source_id,
        instruction_contract_version=system.contract_version,
        content=system_content,
    )
    inputs.append(
        _source_input(
            registry=registry,
            input_type=SystemSourceInput,
            source_id=ContextSourceId.SYSTEM,
            source_instance_id="system:prompt",
            candidate_key="base_system_instruction",
            payload=system_payload,
            revision=_immutable_revision(
                "system:base", system_payload.semantic_fingerprint
            ),
            lifecycle=_generation_root_lifecycle(),
            priority=0,
            required=True,
            intent=_intent("system_instruction", "system"),
            refs=(),
            artifacts=(system_artifact.reference,),
            timing=None,
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )

    memory_instruction = static_by_id.get("memory_scope_instruction")
    if memory_instruction is not None:
        artifact = _require_artifact(
            artifact_metadata, memory_instruction.content_artifact_id
        )
        content = _artifact_content(artifact)
        payload = build_frozen_fact(
            MemoryInstructionPayloadFact,
            schema_version="memory_instruction_payload.v1",
            instruction_contract_version=memory_instruction.contract_version,
            memory_scope_policy_fingerprint=memory_instruction.fact_fingerprint,
            content=content,
        )
        inputs.append(
            _source_input(
                registry=registry,
                input_type=MemoryInstructionSourceInput,
                source_id=ContextSourceId.MEMORY_INSTRUCTION,
                source_instance_id="memory:instruction",
                candidate_key="memory_scope_instruction",
                payload=payload,
                revision=_immutable_revision(
                    "memory:instruction", payload.semantic_fingerprint
                ),
                lifecycle=_generation_root_lifecycle(),
                priority=1,
                required=False,
                intent=_intent("system_instruction", "system"),
                refs=(),
                artifacts=(artifact.reference,),
                timing=None,
                horizons=horizons,
                physical_policy=physical_policy,
            )
        )

    environment_payload = build_frozen_fact(
        RuntimeEnvironmentPayloadFact,
        schema_version="runtime_environment_payload.v1",
        workspace_kind=runtime_environment.workspace_kind,
        model_visible_workspace_root=runtime_environment.model_visible_workspace_root,
        terminal_current_cwd=runtime_environment.terminal_current_cwd,
        session_timezone=runtime_environment.session_timezone,
        rendering_contract_fingerprint=context_fingerprint(
            "runtime-environment-rendering-contract:v1", "stable-no-clock"
        ),
    )
    inputs.append(
        _source_input(
            registry=registry,
            input_type=RuntimeEnvironmentSourceInput,
            source_id=ContextSourceId.RUNTIME_ENVIRONMENT,
            source_instance_id="runtime:environment",
            candidate_key="workspace_environment",
            payload=environment_payload,
            revision=_immutable_revision(
                "runtime:environment", environment_payload.semantic_fingerprint
            ),
            lifecycle=_generation_root_lifecycle(),
            priority=20,
            required=False,
            intent=_intent("leading_context", "user"),
            refs=(),
            artifacts=(),
            timing=_absolute_host_timing(runtime_environment),
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )

    clock_payload = build_frozen_fact(
        RuntimeClockProposalPayloadFact,
        schema_version="runtime_clock_proposal_payload.v1",
        observed_at_utc=compile_timing.compiled_at_utc,
        timezone_name=compile_timing.session_timezone or "UTC",
        local_date=(
            compile_timing.compiled_local_date or compile_timing.compiled_at_utc[:10]
        ),
        proposal_reason="compile",
    )
    inputs.append(
        _source_input(
            registry=registry,
            input_type=RuntimeClockSourceInput,
            source_id=ContextSourceId.RUNTIME_CLOCK,
            source_instance_id="runtime:clock",
            candidate_key=clock_payload.observed_at_utc,
            payload=clock_payload,
            revision=_event_revision(
                f"runtime-clock:{clock_payload.observed_at_utc}",
                clock_payload.semantic_fingerprint,
            ),
            lifecycle=_append_revision_lifecycle("complete_snapshot"),
            priority=89,
            required=False,
            intent=_intent("status_observation", "runtime"),
            refs=(),
            artifacts=(),
            timing=_absolute_host_timing(runtime_environment),
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )

    _append_capability_inputs(
        inputs=inputs,
        registry=registry,
        capability_snapshot=capability_snapshot,
        artifact_metadata=artifact_metadata,
        projections=projection_by_kind,
        horizons=horizons,
        physical_policy=physical_policy,
    )
    _append_memory_projection_input(
        inputs=inputs,
        registry=registry,
        projection=projection_by_kind.get("memory"),
        event_slice=event_slice,
        horizons=horizons,
        physical_policy=physical_policy,
    )
    _append_plan_inputs(
        inputs=inputs,
        registry=registry,
        plan_snapshot=plan_snapshot,
        event_slice=event_slice,
        horizons=horizons,
        physical_policy=physical_policy,
    )
    _append_subagent_inputs(
        inputs=inputs,
        registry=registry,
        selection=next(iter(source_selections), None),
        event_slice=event_slice,
        external_authority_events=external_authority_events,
        horizons=horizons,
        physical_policy=physical_policy,
    )
    if rollout_status is not None:
        content = _inline_content(render_rollout_status_candidate(rollout_status))
        payload = build_frozen_fact(
            RolloutStatusPayloadFact,
            schema_version="rollout_status_payload.v1",
            rollout_account_semantic_fingerprint=rollout_status.semantic_fingerprint,
            phase=(
                "exhausted"
                if rollout_status.rollout_phase.value
                in {"exhausted", "emergency_hard_stop"}
                else "finalization"
                if rollout_status.rollout_phase.value == "finalization_only"
                else "exploration"
            ),
            completed_model_calls=rollout_status.settled_model_call_count,
            completed_tool_invocations=rollout_status.settled_tool_call_count,
            status_policy_fingerprint=context_fingerprint(
                "rollout-status-policy-attribution:v1",
                (
                    rollout_status.exploration_consumption_ratio_ppm,
                    rollout_status.remaining_exploration_milliunits,
                    rollout_status.finalization_reserve_milliunits,
                    rollout_status.allowed_action_classes,
                ),
            ),
            content=content,
        )
        inputs.append(
            _source_input(
                registry=registry,
                input_type=RolloutStatusSourceInput,
                source_id=ContextSourceId.ROLLOUT_STATUS,
                source_instance_id="rollout:status",
                candidate_key=rollout_status.account_id,
                payload=payload,
                revision=_event_revision(
                    f"rollout-status:{rollout_status.semantic_fingerprint}",
                    rollout_status.semantic_fingerprint,
                ),
                lifecycle=_append_revision_lifecycle("complete_snapshot"),
                priority=90,
                required=rollout_status_candidate_is_required(rollout_status),
                intent=_intent("status_observation", "runtime"),
                refs=rollout_status.source_event_refs,
                artifacts=(),
                timing=_absolute_event_timing(
                    event_slice=event_slice,
                    external_authority_events=external_authority_events,
                    refs=rollout_status.source_event_refs,
                ),
                horizons=horizons,
                physical_policy=physical_policy,
            )
        )

    candidates = registry.collect(tuple(inputs))
    tool_root = build_frozen_fact(
        CapabilityToolCatalogRootFact,
        schema_version="capability_tool_catalog_root.v1",
        capability_snapshot_semantic_fingerprint=(
            capability_snapshot.semantic.exposure_semantic_fingerprint
        ),
        ordered_descriptor_fingerprints=tuple(
            item.descriptor_fingerprint for item in tool_specs
        ),
        ordered_tool_spec_fingerprints=tuple(
            context_fingerprint("context-tool-spec:v1", item) for item in tool_specs
        ),
        tool_catalog_contract_fingerprint=context_fingerprint(
            "capability-tool-catalog-contract:v1", "context-tool-spec.v1"
        ),
        authority_horizons=horizons,
    )
    return ContextSourceBuildResult(
        candidates=candidates,
        tool_catalog_root=tool_root,
        physical_input_policy=physical_policy,
        registry_fingerprint=registry.registry_fingerprint,
    )


def _append_capability_inputs(
    *,
    inputs: list[ContextSourceCollectInput],
    registry: ContextSourceRegistry,
    capability_snapshot: CapabilityExposureSnapshotFact,
    artifact_metadata: Mapping[str, ContextSourceArtifactMetadata],
    projections: Mapping[str, ContextProjectionReferenceFact],
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> None:
    for source_id, source_instance_id, projection_kind, input_type, payload_type in (
        (
            ContextSourceId.CAPABILITY_CATALOG,
            "capability:catalog",
            "capability_catalog",
            CapabilityCatalogSourceInput,
            CapabilityCatalogPayloadFact,
        ),
        (
            ContextSourceId.ACTIVE_SKILL,
            "capability:active_skill",
            "capability_active_skill",
            ActiveSkillSourceInput,
            ActiveSkillPayloadFact,
        ),
    ):
        projection_ref = projections.get(projection_kind)
        projection = (
            capability_snapshot.semantic.catalog_projection
            if source_id is ContextSourceId.CAPABILITY_CATALOG
            else capability_snapshot.semantic.active_skill_projection
        )
        artifact_id = projection.rendered_prompt_artifact_id
        if projection_ref is None or artifact_id is None:
            continue
        artifact = _require_artifact(artifact_metadata, artifact_id)
        content = _artifact_content(artifact)
        ordered = tuple(
            item.content_fingerprint for item in projection.visible_source_entries
        )
        if payload_type is CapabilityCatalogPayloadFact:
            payload = build_frozen_fact(
                CapabilityCatalogPayloadFact,
                schema_version="capability_catalog_payload.v1",
                prose_projection_semantic_fingerprint=(
                    projection.projection_semantic_fingerprint
                ),
                ordered_projection_entry_semantic_fingerprints=ordered,
                projection_contract_fingerprint=context_fingerprint(
                    "capability-prose-projection-contract:v1", projection_kind
                ),
                prose_content=content,
            )
            priority = 30
            intent = _intent("leading_context", "user")
        else:
            payload = build_frozen_fact(
                ActiveSkillPayloadFact,
                schema_version="active_skill_payload.v1",
                skill_projection_semantic_fingerprint=(
                    projection.projection_semantic_fingerprint
                ),
                ordered_active_skill_semantic_fingerprints=ordered,
                projection_contract_fingerprint=context_fingerprint(
                    "capability-prose-projection-contract:v1", projection_kind
                ),
                content=content,
            )
            priority = 31
            # A changing source cannot be inserted into the immutable system
            # prefix without invalidating provider continuation. Lower the
            # typed, revision-wrapped skill snapshot as runtime-owned context.
            intent = _intent("leading_context", "user")
        inputs.append(
            _source_input(
                registry=registry,
                input_type=input_type,
                source_id=source_id,
                source_instance_id=source_instance_id,
                candidate_key=projection_kind,
                payload=payload,
                revision=_event_revision(
                    f"{projection_kind}:{projection.projection_semantic_fingerprint}",
                    projection.projection_semantic_fingerprint,
                ),
                lifecycle=_append_revision_lifecycle("complete_snapshot"),
                priority=priority,
                required=False,
                intent=intent,
                refs=projection_ref.source_event_refs,
                artifacts=(artifact.reference,),
                timing=_absolute_event_timing(
                    event_slice=None,
                    external_authority_events={},
                    refs=projection_ref.source_event_refs,
                    allow_unknown=True,
                ),
                horizons=horizons,
                physical_policy=physical_policy,
            )
        )


def _append_memory_projection_input(
    *,
    inputs: list[ContextSourceCollectInput],
    registry: ContextSourceRegistry,
    projection: ContextProjectionReferenceFact | None,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> None:
    if projection is None:
        return
    if len(projection.source_event_refs) != 1:
        raise ValueError("memory projection requires one terminal event")
    stored = event_slice.event_by_id(projection.source_event_refs[0].event_id)
    event = stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(event, ProjectionReadyEvent):
        raise ValueError("memory projection source is not ProjectionReadyEvent")
    if event.projection_kind in {"working_context", "mixed"}:
        heading = (
            "Recalled Memory and Recent Working Context "
            "(source=fenced_memory_context; do not write it back as new memory):\n"
            "Recent Working Context is independent from canonical memory search. "
            "An empty memory_search result does not invalidate recent activity shown here."
        )
    else:
        heading = "Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory):"
    content = _inline_content("\n\n".join((heading, event.summary)))
    payload = build_frozen_fact(
        MemoryProjectionPayloadFact,
        schema_version="memory_projection_payload.v1",
        projection_semantic_fingerprint=projection.semantic_fingerprint,
        ordered_memory_semantic_fingerprints=(stored.payload_fingerprint,),
        selection_contract_fingerprint=context_fingerprint(
            "memory-context-selection-contract:v1", event.projection_kind
        ),
        content=content,
    )
    inputs.append(
        _source_input(
            registry=registry,
            input_type=MemoryProjectionSourceInput,
            source_id=ContextSourceId.MEMORY_PROJECTION,
            source_instance_id="memory:projection",
            candidate_key=event.projection_kind,
            payload=payload,
            revision=_event_revision(event.id, stored.payload_fingerprint),
            lifecycle=_append_revision_lifecycle("complete_snapshot"),
            priority=40,
            required=False,
            intent=_intent("leading_context", "user"),
            refs=projection.source_event_refs,
            artifacts=(),
            timing=_absolute_from_stored((stored,)),
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )


def _append_plan_inputs(
    *,
    inputs: list[ContextSourceCollectInput],
    registry: ContextSourceRegistry,
    plan_snapshot: ContextPlanSnapshotFact,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> None:
    if not plan_snapshot.active:
        return
    content = _inline_content(PLAN_ACTIVE_INSTRUCTION)
    payload = build_frozen_fact(
        PlanRevisionPayloadFact,
        schema_version="plan_revision_payload.v1",
        workflow_id=plan_snapshot.workflow_id,
        active=True,
        canonical_plan_revision=plan_snapshot.revision,
        plan_decision="continue" if plan_snapshot.revision else "enter",
        plan_semantic_fingerprint=plan_snapshot.fact_fingerprint,
        content=content,
    )
    refs = (plan_snapshot.entered_event,) if plan_snapshot.entered_event else ()
    inputs.append(
        _source_input(
            registry=registry,
            input_type=PlanSourceInput,
            source_id=ContextSourceId.PLAN,
            source_instance_id="plan:workflow",
            candidate_key=str(plan_snapshot.workflow_id),
            payload=payload,
            revision=_event_revision(
                f"plan:{plan_snapshot.workflow_id}:{plan_snapshot.revision}",
                plan_snapshot.fact_fingerprint,
            ),
            lifecycle=_append_revision_lifecycle("complete_snapshot"),
            priority=10,
            required=True,
            intent=_intent("leading_context", "user"),
            refs=refs,
            artifacts=(),
            timing=_absolute_event_timing(
                event_slice=event_slice,
                external_authority_events={},
                refs=refs,
                allow_unknown=True,
            ),
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )
    decisions = tuple(
        (stored, event)
        for stored in event_slice.events
        if stored.event_type == EventType.PLAN_EXIT_RESOLVED
        if isinstance(
            (event := stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
            PlanExitResolvedEvent,
        )
        and event.decision == "revise"
    )
    if not decisions:
        return
    stored, event = decisions[-1]
    feedback = (
        event.user_feedback.strip() or "(no additional feedback text was provided)"
    )
    revision_text = (
        "Plan revision is still pending. The user requested a revision with this feedback:\n"
        f"{feedback}\n\n"
        "You must now present the revised plan by calling exit_plan. Do not provide a plain-text "
        "final answer or implementation summary. Only call ask_plan_question if a new material "
        "ambiguity genuinely blocks the revised plan."
    )
    revision_content = _inline_content(revision_text)
    revision_payload = build_frozen_fact(
        PlanRevisionPayloadFact,
        schema_version="plan_revision_payload.v1",
        workflow_id=plan_snapshot.workflow_id,
        active=True,
        canonical_plan_revision=plan_snapshot.revision,
        plan_decision="revise",
        plan_semantic_fingerprint=stored.payload_fingerprint,
        content=revision_content,
    )
    ref = stored.to_reference(event_slice.runtime_session_id)
    inputs.append(
        _source_input(
            registry=registry,
            input_type=PlanSourceInput,
            source_id=ContextSourceId.PLAN,
            source_instance_id="plan:revision",
            candidate_key=event.id,
            payload=revision_payload,
            revision=_event_revision(event.id, stored.payload_fingerprint),
            lifecycle=_append_once_lifecycle(),
            priority=11,
            required=True,
            intent=_intent("leading_context", "user"),
            refs=(ref,),
            artifacts=(),
            timing=_absolute_from_stored((stored,)),
            horizons=horizons,
            physical_policy=physical_policy,
        )
    )


def _append_subagent_inputs(
    *,
    inputs: list[ContextSourceCollectInput],
    registry: ContextSourceRegistry,
    selection: ContextCandidateSourceSelectionFact | None,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    external_authority_events: Mapping[str, FrozenStoredEvent],
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> None:
    if selection is None or not selection.selected_source_ids:
        return
    selected = set(selection.selected_source_ids)
    matches: list[tuple[FrozenStoredEvent, SubagentRunCompletedEvent]] = []
    for stored in (*event_slice.events, *external_authority_events.values()):
        if stored.event_type != EventType.SUBAGENT_RUN_COMPLETED:
            continue
        event = stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if isinstance(event, SubagentRunCompletedEvent) and event.result_id in selected:
            matches.append((stored, event))
    matches.sort(
        key=lambda item: selection.selected_source_ids.index(item[1].result_id)
    )
    if tuple(item[1].result_id for item in matches) != selection.selected_source_ids:
        raise ValueError("subagent source selection lacks exact completion authority")
    for stored, event in matches:
        text = "\n".join(
            (
                "Completed child agent result that has not been explicitly collected with wait_agent:",
                f"- subagent_run_id: {event.subagent_run_id}",
                f"  result_id: {event.result_id}",
                "  status: completed",
                f"  summary: {event.summary}",
                f"  result_artifact_id: {event.result_artifact_id or 'none'}",
            )
        )
        content = _inline_content(text)
        payload = build_frozen_fact(
            SubagentResultPayloadFact,
            schema_version="subagent_result_payload.v1",
            child_runtime_session_id=event.child_runtime_session_id,
            completion_semantic_fingerprint=stored.payload_fingerprint,
            delivery_semantic_fingerprint=context_fingerprint(
                "subagent-result-delivery:v1", event.result_id
            ),
            result_state="success",
            content=content,
        )
        ref = stored.to_reference(stored.runtime_session_id)
        inputs.append(
            _source_input(
                registry=registry,
                input_type=SubagentResultSourceInput,
                source_id=ContextSourceId.SUBAGENT_RESULT,
                source_instance_id=f"subagent:result:{event.result_id}",
                candidate_key=event.result_id,
                payload=payload,
                revision=_event_revision(event.id, stored.payload_fingerprint),
                lifecycle=_append_once_lifecycle(),
                priority=60,
                required=False,
                intent=_intent("leading_context", "user"),
                refs=(ref,),
                artifacts=(),
                timing=_absolute_from_stored((stored,)),
                horizons=horizons,
                physical_policy=physical_policy,
            )
        )


def _source_input(
    *,
    registry: ContextSourceRegistry,
    input_type,
    source_id: ContextSourceId,
    source_instance_id: str,
    candidate_key: str,
    payload,
    revision,
    lifecycle,
    priority: int,
    required: bool,
    intent,
    refs: tuple,
    artifacts: tuple[ContextArtifactReferenceFact, ...],
    timing,
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    physical_policy: ResolvedContextSourcePhysicalInputPolicyFact,
) -> ContextSourceCollectInput:
    contract = registry.resolve(source_id=source_id).contract
    dependency = context_source_input_dependency_fingerprint(
        source_instance_id=source_instance_id,
        candidate_key=candidate_key,
        source_revision=revision,
        payload=payload,
        lifecycle=lifecycle,
        priority=priority,
        required=required,
        lowering_intent=intent,
        source_event_refs=refs,
        source_artifact_refs=artifacts,
        source_absolute_timing=timing,
        source_contract_fingerprint=contract.contract_fingerprint,
    )
    authority = build_frozen_fact(
        ContextSourceInputAuthorityFact,
        schema_version="context_source_input_authority.v1",
        source_id=source_id,
        source_contract_id=source_id.value,
        source_contract_version=contract.source_version,
        source_contract_fingerprint=contract.contract_fingerprint,
        authority_horizons=horizons,
        physical_input_policy_fingerprint=physical_policy.policy_fingerprint,
        input_dependency_fingerprint=dependency,
    )
    return input_type(
        authority=authority,
        source_instance_id=source_instance_id,
        candidate_key=candidate_key,
        source_revision=revision,
        payload=payload,
        lifecycle=lifecycle,
        priority=priority,
        required=required,
        lowering_intent=intent,
        source_event_refs=refs,
        source_artifact_refs=artifacts,
        source_absolute_timing=timing,
    )


def _inline_content(
    text: str,
    media_type: str = "text/markdown",
) -> InlineContextSourceContentSemanticFact:
    return build_frozen_fact(
        InlineContextSourceContentSemanticFact,
        schema_version="inline_context_source_content_semantic.v1",
        text=text,
        chars=len(text),
        utf8_bytes=len(text.encode("utf-8")),
        media_type=media_type,
    )


def _artifact_content(
    artifact: ContextSourceArtifactMetadata,
) -> ArtifactContextSourceContentSemanticFact:
    media_type = _normalized_context_source_media_type(artifact.reference.media_type)
    return build_frozen_fact(
        ArtifactContextSourceContentSemanticFact,
        schema_version="artifact_context_source_content_semantic.v1",
        content_sha256=artifact.reference.content_sha256,
        expected_chars=artifact.expected_chars,
        expected_utf8_bytes=artifact.reference.content_bytes,
        media_type=media_type,
        codec_contract_fingerprint=context_fingerprint(
            "context-source-artifact-codec-contract:v1",
            {"media_type": media_type, "codec": "utf-8"},
        ),
    )


def _normalized_context_source_media_type(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    if normalized not in {"text/plain", "text/markdown", "application/json"}:
        raise ValueError("artifact ContextSource content has unsupported media type")
    return normalized


def _immutable_revision(source_revision_id: str, state_fingerprint: str):
    return build_frozen_fact(
        ImmutableSourceRevisionFact,
        schema_version="immutable_source_revision.v1",
        source_revision_id=source_revision_id,
        source_state_semantic_fingerprint=state_fingerprint,
    )


def _event_revision(source_revision_id: str, event_fingerprint: str):
    return build_frozen_fact(
        EventSourceRevisionFact,
        schema_version="event_source_revision.v1",
        source_revision_id=source_revision_id,
        producer_event_semantic_fingerprint=event_fingerprint,
    )


@lru_cache(maxsize=1)
def _generation_root_lifecycle():
    return build_frozen_fact(
        GenerationRootLifecycleFact,
        schema_version="generation_root_lifecycle.v1",
        lifecycle_kind="generation_root",
        on_semantic_change="rollover",
    )


@lru_cache(maxsize=1)
def _append_once_lifecycle():
    return build_frozen_fact(
        AppendOnceLifecycleFact,
        schema_version="append_once_lifecycle.v1",
        lifecycle_kind="append_once",
        duplicate_semantic_identity="no_op",
        conflicting_same_key="contract_mismatch",
    )


@lru_cache(maxsize=2)
def _append_revision_lifecycle(continuity_kind: str):
    return build_frozen_fact(
        AppendRevisionLifecycleFact,
        schema_version="append_revision_lifecycle.v1",
        lifecycle_kind="append_revision",
        supersession_semantics="latest_revision_wins",
        continuity_kind=continuity_kind,
        source_revision_contract_fingerprint=context_fingerprint(
            "source-revision-contract:v1", continuity_kind
        ),
    )


@lru_cache(maxsize=8)
def _intent(intent_kind: str, role_constraint: str | None):
    return build_frozen_fact(
        ContextCandidateLoweringIntentFact,
        schema_version="context_candidate_lowering_intent.v1",
        intent_kind=intent_kind,
        role_constraint=role_constraint,
        pairing_constraint="none",
        intent_contract_fingerprint=context_fingerprint(
            "context-candidate-lowering-intent-contract:v1",
            [intent_kind, role_constraint, "none"],
        ),
    )


def _absolute_host_timing(
    environment: ContextRuntimeEnvironmentFact,
) -> ContextSourceAbsoluteTimingFact:
    return build_frozen_fact(
        ContextSourceAbsoluteTimingFact,
        schema_version="context_source_absolute_timing.v1",
        observed_at_utc=environment.observed_at_utc,
        source_started_at_utc=None,
        source_ended_at_utc=None,
        source_sequence_ranges=(),
        clock_source="host_clock",
        freshness_kind="current_turn",
        timing_contract_fingerprint=context_fingerprint(
            "context-source-absolute-timing-contract:v1", "host-clock"
        ),
    )


def _absolute_event_timing(
    *,
    event_slice: ContextEventSlice | ContextEventAuthorityView | None,
    external_authority_events: Mapping[str, FrozenStoredEvent],
    refs: tuple,
    allow_unknown: bool = False,
) -> ContextSourceAbsoluteTimingFact | None:
    stored: list[FrozenStoredEvent] = []
    for ref in refs:
        item = external_authority_events.get(ref.event_id)
        if item is None and event_slice is not None:
            try:
                item = event_slice.event_by_id(ref.event_id)
            except Exception:
                item = None
        if item is None:
            if allow_unknown:
                return None
            raise ValueError("ContextSource timing lacks referenced event")
        stored.append(item)
    return _absolute_from_stored(tuple(stored)) if stored else None


def _absolute_from_stored(
    stored: tuple[FrozenStoredEvent, ...],
) -> ContextSourceAbsoluteTimingFact:
    ranges = tuple(
        build_frozen_fact(
            LedgerSequenceRangeFact,
            schema_version="ledger_sequence_range.v1",
            runtime_session_id=item.runtime_session_id,
            first_sequence=item.sequence,
            last_sequence=item.sequence,
        )
        for item in stored
    )
    return build_frozen_fact(
        ContextSourceAbsoluteTimingFact,
        schema_version="context_source_absolute_timing.v1",
        observed_at_utc=stored[-1].created_at_utc,
        source_started_at_utc=stored[0].created_at_utc,
        source_ended_at_utc=stored[-1].created_at_utc,
        source_sequence_ranges=ranges,
        clock_source="event_created_at",
        freshness_kind="current_run_tail",
        timing_contract_fingerprint=context_fingerprint(
            "context-source-absolute-timing-contract:v1", "event-created-at"
        ),
    )


def _require_artifact(
    artifacts: Mapping[str, ContextSourceArtifactMetadata],
    artifact_id: str,
) -> ContextSourceArtifactMetadata:
    try:
        return artifacts[artifact_id]
    except KeyError as exc:
        raise ValueError(
            f"ContextSource artifact was not hydrated: {artifact_id}"
        ) from exc


__all__ = [
    "ContextSourceArtifactMetadata",
    "ContextSourceBuildResult",
    "HydratedContextSourceArtifact",
    "build_context_sources",
    "default_context_source_registry",
    "hydrate_context_source_content_sidecar",
    "resolved_context_source_physical_policy",
]
