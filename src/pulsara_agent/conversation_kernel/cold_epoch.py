"""Pure, provider-neutral assembly for the first open of a cold epoch.

This module deliberately owns no I/O and no execution authority.  Callers
freeze the canonical cut, Round 9 capability cut/views, context sources and
model binding before entering here.  Replay bodies are hydrated by the caller
between :meth:`prepare_semantic` and :meth:`finalize_wire`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from pulsara_agent.capability.contracts import (
    FrozenCapabilityDispatchCut,
    FrozenMcpRouteProjection,
    FrozenNativeToolProjectionSet,
    FrozenSkillCapabilityDispatchView,
    FrozenToolCapabilityDispatchView,
    FrozenToolCapabilityExposurePlan,
)
from pulsara_agent.conversation_kernel.context_sources import (
    FrozenNonTriggerContextSources,
)
from pulsara_agent.llm.provider_replay import ProviderReplayTargetCompatibilityFact
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    FrozenCompiledModelInput,
    ModelInputScopeKind,
    StructuredModelInputCompileRequest,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputEpochView,
    ProviderInputEpochCompatibility,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    FrozenDurableProviderReplayManifest,
    FrozenSelectedDurableProviderReplayHydration,
    select_compatible_provider_replay_manifests,
    selected_message_placements_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint


@dataclass(frozen=True, slots=True)
class CanonicalColdContinuationSeed:
    """Exact canonical continuation used by ordinary fresh/restart opens."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)


@dataclass(frozen=True, slots=True)
class CompactionContinuationSeed:
    """Exact canonical continuation installed by an active compaction."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    binding_rewrite_identity: str
    protected_tail_selection_fingerprint: str

    def __post_init__(self) -> None:
        if not self.binding_rewrite_identity or not self.protected_tail_selection_fingerprint.startswith(
            "sha256:"
        ):
            raise ValueError("compaction continuation seed is incomplete")


@dataclass(frozen=True, slots=True)
class SubagentInitialSeed:
    """Round 10 retained contract; Round 5B does not admit child work."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    objective_item_fingerprint: str
    parent_context_selection_fingerprint: str | None

    def __post_init__(self) -> None:
        identity = self.dispatch_read.compile_snapshot.canonical_input.identity
        if (
            identity.conversation_scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
            or identity.scope_subagent_task_id is None
            or not self.objective_item_fingerprint.startswith("sha256:")
            or (
                self.parent_context_selection_fingerprint is not None
                and not self.parent_context_selection_fingerprint.startswith("sha256:")
            )
        ):
            raise ValueError("subagent initial seed is invalid")


FrozenColdConversationSeed = (
    CanonicalColdContinuationSeed | CompactionContinuationSeed | SubagentInitialSeed
)


@dataclass(frozen=True, slots=True)
class SelectedDurableReplayHydrationRequest:
    """Metadata-only request for the existing bounded replay body reader."""

    source_dispatch_read_fingerprint: str
    replay_target: ProviderReplayTargetCompatibilityFact
    selected_manifests: tuple[FrozenDurableProviderReplayManifest, ...]
    selected_message_placements_fingerprint: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "pulsara:cold-epoch-selected-replay-hydration-request:v1",
            {
                "source": self.source_dispatch_read_fingerprint,
                "target": self.replay_target.replay_target_fingerprint,
                "manifests": tuple(
                    item.manifest_fingerprint for item in self.selected_manifests
                ),
                "placements": self.selected_message_placements_fingerprint,
            },
        )
        if not self.selected_manifests or self.request_fingerprint != expected:
            raise ValueError("selected replay hydration request is invalid")


@dataclass(frozen=True, slots=True)
class PreparedColdEpochSemanticAssembly:
    seed: FrozenColdConversationSeed = field(repr=False)
    planning: FrozenProviderInputAppendPlanningInput = field(repr=False)
    compatibility: ProviderInputEpochCompatibility
    compiled_result: FrozenProviderInputAppendCompileResult = field(repr=False)
    prepared_call_identity: str
    capability_dispatch_cut: FrozenCapabilityDispatchCut = field(repr=False)
    tool_view: FrozenToolCapabilityDispatchView = field(repr=False)
    skill_view: FrozenSkillCapabilityDispatchView = field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = field(repr=False)
    non_trigger_sources: FrozenNonTriggerContextSources = field(repr=False)
    replay_target: ProviderReplayTargetCompatibilityFact
    hydration_request: SelectedDurableReplayHydrationRequest | None = field(
        default=None, repr=False
    )


@dataclass(frozen=True, slots=True)
class ColdEpochContinuityCandidateInputs:
    planning: FrozenProviderInputAppendPlanningInput = field(repr=False)
    compatibility: ProviderInputEpochCompatibility
    compiled_result: FrozenProviderInputAppendCompileResult = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    capability_dispatch_cut_fingerprint: str
    direct_native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    mcp_route_projection: FrozenMcpRouteProjection = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.wire_input_plan.compiled_semantic_fingerprint
            != self.compiled_result.compiled_input.compiled_semantic_fingerprint
            or self.wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool
                for item in self.direct_native_projection_set.projections
            )
            or not self.capability_dispatch_cut_fingerprint.startswith("sha256:")
        ):
            raise ValueError("cold epoch continuity inputs do not exact-join")


@dataclass(frozen=True, slots=True)
class ColdEpochInputAssemblyResult:
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    continuity_candidate_inputs: ColdEpochContinuityCandidateInputs = field(
        repr=False
    )


class ColdEpochWirePlanner(Protocol):
    def __call__(
        self,
        *,
        compiled_input: FrozenCompiledModelInput,
        predecessor_view: FrozenProviderInputEpochView | None,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
    ) -> FrozenProviderWireInputPlan: ...


class KernelColdEpochInputAssembler:
    """The only pure cold/reset semantic and wire assembly implementation."""

    def __init__(self, compiler: StructuredModelInputCompiler) -> None:
        self._compiler = compiler

    def prepare_semantic(
        self,
        *,
        seed: FrozenColdConversationSeed,
        compile_request: StructuredModelInputCompileRequest,
        planning: FrozenProviderInputAppendPlanningInput,
        compatibility: ProviderInputEpochCompatibility,
        prepared_call_identity: str,
        capability_dispatch_cut: FrozenCapabilityDispatchCut,
        tool_view: FrozenToolCapabilityDispatchView,
        skill_view: FrozenSkillCapabilityDispatchView,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
        non_trigger_sources: FrozenNonTriggerContextSources,
        replay_target: ProviderReplayTargetCompatibilityFact,
        deadline_monotonic: float,
    ) -> PreparedColdEpochSemanticAssembly:
        self._validate_inputs(
            seed=seed,
            compile_request=compile_request,
            planning=planning,
            prepared_call_identity=prepared_call_identity,
            capability_dispatch_cut=capability_dispatch_cut,
            tool_view=tool_view,
            skill_view=skill_view,
            tool_exposure_plan=tool_exposure_plan,
            non_trigger_sources=non_trigger_sources,
            deadline_monotonic=deadline_monotonic,
        )
        compiled_result = self._compiler.compile_append(
            compile_request,
            planning=planning,
            compatibility=compatibility,
            deadline_monotonic=deadline_monotonic,
        )
        self._require_deadline(deadline_monotonic)
        dispatch_read = seed.dispatch_read
        selected, placements = select_compatible_provider_replay_manifests(
            manifest_cut=dispatch_read.replay_manifest_cut,
            compiled_input=compiled_result.compiled_input,
            replay_target=replay_target,
        )
        hydration_request = None
        if selected:
            placement_fingerprint = selected_message_placements_fingerprint(placements)
            values = {
                "source_dispatch_read_fingerprint": dispatch_read.composite_fingerprint,
                "replay_target": replay_target,
                "selected_manifests": selected,
                "selected_message_placements_fingerprint": placement_fingerprint,
            }
            hydration_request = SelectedDurableReplayHydrationRequest(
                **values,
                request_fingerprint=context_fingerprint(
                    "pulsara:cold-epoch-selected-replay-hydration-request:v1",
                    {
                        "source": dispatch_read.composite_fingerprint,
                        "target": replay_target.replay_target_fingerprint,
                        "manifests": tuple(
                            item.manifest_fingerprint for item in selected
                        ),
                        "placements": placement_fingerprint,
                    },
                ),
            )
        return PreparedColdEpochSemanticAssembly(
            seed=seed,
            planning=planning,
            compatibility=compatibility,
            compiled_result=compiled_result,
            prepared_call_identity=prepared_call_identity,
            capability_dispatch_cut=capability_dispatch_cut,
            tool_view=tool_view,
            skill_view=skill_view,
            tool_exposure_plan=tool_exposure_plan,
            non_trigger_sources=non_trigger_sources,
            replay_target=replay_target,
            hydration_request=hydration_request,
        )

    def finalize_wire(
        self,
        prepared: PreparedColdEpochSemanticAssembly,
        *,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
        wire_planner: ColdEpochWirePlanner,
        deadline_monotonic: float,
    ) -> ColdEpochInputAssemblyResult:
        self._require_deadline(deadline_monotonic)
        request = prepared.hydration_request
        if request is None:
            if replay_hydration is not None:
                raise ValueError("unexpected replay hydration for cold epoch")
        elif (
            replay_hydration is None
            or replay_hydration.source_manifest_cut_fingerprint
            != prepared.seed.dispatch_read.replay_manifest_cut.cut_fingerprint
            or replay_hydration.replay_target_fingerprint
            != request.replay_target.replay_target_fingerprint
            or replay_hydration.selected_message_placements_fingerprint
            != request.selected_message_placements_fingerprint
            or replay_hydration.selected_manifests != request.selected_manifests
        ):
            raise ValueError("cold epoch replay hydration does not exact-join")
        predecessor = prepared.planning.predecessor_view
        if prepared.compiled_result.reset_reason is not None:
            predecessor = None
        wire_plan = wire_planner(
            compiled_input=prepared.compiled_result.compiled_input,
            predecessor_view=predecessor,
            replay_hydration=replay_hydration,
        )
        self._require_deadline(deadline_monotonic)
        if (
            wire_plan.compiled_semantic_fingerprint
            != prepared.compiled_result.compiled_input.compiled_semantic_fingerprint
        ):
            raise ValueError("cold epoch wire plan changed semantic input")
        candidate_inputs = ColdEpochContinuityCandidateInputs(
            planning=prepared.planning,
            compatibility=prepared.compatibility,
            compiled_result=prepared.compiled_result,
            wire_input_plan=wire_plan,
            capability_dispatch_cut_fingerprint=(
                prepared.capability_dispatch_cut.dispatch_cut_fingerprint
            ),
            direct_native_projection_set=(
                prepared.tool_exposure_plan.direct_projection_set
            ),
            mcp_route_projection=(
                prepared.tool_exposure_plan.mcp_catalog_route_projection
            ),
        )
        return ColdEpochInputAssemblyResult(
            compiled_input=prepared.compiled_result.compiled_input,
            wire_input_plan=wire_plan,
            continuity_candidate_inputs=candidate_inputs,
        )

    @staticmethod
    def _require_deadline(deadline_monotonic: float) -> None:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("cold epoch assembly deadline expired")

    @classmethod
    def _validate_inputs(
        cls,
        *,
        seed: FrozenColdConversationSeed,
        compile_request: StructuredModelInputCompileRequest,
        planning: FrozenProviderInputAppendPlanningInput,
        prepared_call_identity: str,
        capability_dispatch_cut: FrozenCapabilityDispatchCut,
        tool_view: FrozenToolCapabilityDispatchView,
        skill_view: FrozenSkillCapabilityDispatchView,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
        non_trigger_sources: FrozenNonTriggerContextSources,
        deadline_monotonic: float,
    ) -> None:
        cls._require_deadline(deadline_monotonic)
        if type(seed) not in {
            CanonicalColdContinuationSeed,
            CompactionContinuationSeed,
            SubagentInitialSeed,
        }:
            raise TypeError("cold epoch seed union is not closed")
        canonical = seed.dispatch_read.compile_snapshot.canonical_input
        identity = canonical.identity
        if (
            compile_request.canonical_facts
            != seed.dispatch_read.compile_snapshot
            or compile_request.canonical_input != canonical
            or planning.scope.session_id != identity.session_id
            or planning.scope.scope_kind is not identity.conversation_scope_kind
            or planning.scope.scope_subagent_task_id
            != identity.scope_subagent_task_id
            or capability_dispatch_cut.conversation_scope_kind
            is not identity.conversation_scope_kind
            or capability_dispatch_cut.scope_subagent_task_id
            != identity.scope_subagent_task_id
            or tool_view.parent_dispatch_cut_fingerprint
            != capability_dispatch_cut.dispatch_cut_fingerprint
            or skill_view.parent_dispatch_cut_fingerprint
            != capability_dispatch_cut.dispatch_cut_fingerprint
            or tool_exposure_plan.dispatch_cut_fingerprint
            != capability_dispatch_cut.dispatch_cut_fingerprint
            or tool_exposure_plan.tool_dispatch_view_fingerprint
            != tool_view.view_fingerprint
            or non_trigger_sources.tool_exposure_plan != tool_exposure_plan
            or non_trigger_sources.skill_dispatch_view != skill_view
            or compile_request.compile_binding.tool_surface
            != tool_exposure_plan.direct_tool_surface
            or not prepared_call_identity.startswith("sha256:")
        ):
            raise ValueError("cold epoch inputs do not exact-join")


__all__ = [
    "CanonicalColdContinuationSeed",
    "ColdEpochContinuityCandidateInputs",
    "ColdEpochInputAssemblyResult",
    "CompactionContinuationSeed",
    "FrozenColdConversationSeed",
    "KernelColdEpochInputAssembler",
    "PreparedColdEpochSemanticAssembly",
    "SelectedDurableReplayHydrationRequest",
    "SubagentInitialSeed",
]
