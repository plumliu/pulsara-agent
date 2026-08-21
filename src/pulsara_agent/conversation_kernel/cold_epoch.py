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
    build_subagent_context_source,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenDependencyResultContext,
    FrozenSubagentParentContextCallSubject,
    FrozenSubagentParentContextSelection,
    SubagentContextMode,
    SubagentProfileKind,
    build_parent_context_selection,
)
from pulsara_agent.llm.provider_replay import ProviderReplayTargetCompatibilityFact
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ContextSourceAbsentFact,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenCompiledModelInput,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    StructuredModelInputCompileRequest,
    provider_input_item_fingerprint,
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


_SUBAGENT_SEED_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class SubagentInitialSeed:
    """Exact sealed first-open carrier for one fixed worker leaf."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    task_id: str
    parent_turn_id: str
    profile_kind: SubagentProfileKind
    objective: str = field(repr=False)
    objective_item: FrozenProviderInputItem = field(repr=False)
    objective_item_fingerprint: str
    parent_call_subject: FrozenSubagentParentContextCallSubject = field(repr=False)
    parent_context_selection: FrozenSubagentParentContextSelection = field(repr=False)
    parent_context_source: ContextSourceCandidate | ContextSourceAbsentFact = field(
        repr=False
    )
    dependency_context: FrozenDependencyResultContext | None = field(repr=False)
    dependency_results_source: ContextSourceCandidate | ContextSourceAbsentFact = field(
        repr=False
    )
    seed_fingerprint: str
    _authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        identity = self.dispatch_read.compile_snapshot.canonical_input.identity
        canonical = self.dispatch_read.compile_snapshot.canonical_input
        objective_items = tuple(
            item
            for item in canonical.items
            if item.item_kind is FrozenProviderInputItemKind.USER
            and item.input_origin is CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
        )
        expected_objective_fingerprint = provider_input_item_fingerprint(
            self.objective_item
        )
        expected_selection = build_parent_context_selection(
            self.parent_call_subject,
            mode=self.parent_context_selection.mode,
            last_n_turns=self.parent_context_selection.last_n_turns,
        )
        expected_parent_source = build_subagent_context_source(
            kind=ContextSourceKind.PARENT_CONTEXT,
            text=expected_selection.rendered_body,
            domain_identity={
                "subject": self.parent_call_subject.subject_fingerprint,
                "selection": expected_selection.selection_fingerprint,
                "source": expected_selection.source_fingerprint,
            },
        )
        expected_dependency_source = build_subagent_context_source(
            kind=ContextSourceKind.DEPENDENCY_RESULTS,
            text=(
                None
                if self.dependency_context is None
                else self.dependency_context.rendered_body
            ),
            domain_identity=(
                None
                if self.dependency_context is None
                else self.dependency_context.context_fingerprint
            ),
        )
        parent_present = isinstance(self.parent_context_source, ContextSourceCandidate)
        dependency_present = isinstance(
            self.dependency_results_source, ContextSourceCandidate
        )
        if (
            self._authority is not _SUBAGENT_SEED_AUTHORITY
            or identity.conversation_scope_kind
            is not ModelInputScopeKind.SUBAGENT_TASK
            or identity.scope_subagent_task_id != self.task_id
            or not self.parent_turn_id
            or self.parent_call_subject.caller_turn_id != self.parent_turn_id
            or not isinstance(self.profile_kind, SubagentProfileKind)
            or len(objective_items) != 1
            or objective_items[0] != self.objective_item
            or self.objective_item.text != self.objective
            or self.objective_item.source_turn_id != identity.turn_id
            or self.objective_item_fingerprint != expected_objective_fingerprint
            or self.parent_call_subject.session_id != identity.session_id
            or self.parent_context_selection != expected_selection
            or self.parent_context_source != expected_parent_source
            or self.dependency_results_source != expected_dependency_source
            or (
                self.parent_context_selection.mode is SubagentContextMode.NONE
                and parent_present
            )
            or bool(self.parent_context_selection.selected_units) != parent_present
            or self.parent_context_source.source_kind is not ContextSourceKind.PARENT_CONTEXT
            or (self.dependency_context is None) == dependency_present
            or self.dependency_results_source.source_kind
            is not ContextSourceKind.DEPENDENCY_RESULTS
            or (
                self.dependency_context is not None
                and self.dependency_context.target_task_id != self.task_id
            )
        ):
            raise ValueError("subagent initial seed is invalid")
        expected = context_fingerprint(
            "pulsara:subagent-initial-seed:v4",
            {
                "dispatch_read": self.dispatch_read.composite_fingerprint,
                "task_id": self.task_id,
                "parent_turn_id": self.parent_turn_id,
                "profile": self.profile_kind.value,
                "objective_item": self.objective_item_fingerprint,
                "parent_subject": self.parent_call_subject.subject_fingerprint,
                "parent_selection": self.parent_context_selection.selection_fingerprint,
                "parent_source": self.parent_context_source.domain_semantic_fingerprint,
                "dependency_context": (
                    None
                    if self.dependency_context is None
                    else self.dependency_context.context_fingerprint
                ),
                "dependency_source": (
                    self.dependency_results_source.domain_semantic_fingerprint
                ),
            },
        )
        if self.seed_fingerprint != expected:
            raise ValueError("subagent initial seed fingerprint mismatch")

    @property
    def source_replacements(
        self,
    ) -> tuple[ContextSourceCandidate | ContextSourceAbsentFact, ...]:
        return self.parent_context_source, self.dependency_results_source


def build_subagent_initial_seed(
    *,
    dispatch_read: FrozenCanonicalProviderDispatchRead,
    task_id: str,
    parent_turn_id: str,
    profile_kind: SubagentProfileKind,
    objective: str,
    parent_call_subject: FrozenSubagentParentContextCallSubject,
    parent_context_selection: FrozenSubagentParentContextSelection,
    dependency_context: FrozenDependencyResultContext | None,
) -> SubagentInitialSeed:
    canonical = dispatch_read.compile_snapshot.canonical_input
    objective_items = tuple(
        item
        for item in canonical.items
        if item.item_kind is FrozenProviderInputItemKind.USER
        and item.input_origin is CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
    )
    if len(objective_items) != 1:
        raise ValueError("child canonical cut lacks one exact objective item")
    objective_item = objective_items[0]
    objective_fingerprint = provider_input_item_fingerprint(objective_item)
    parent_source = build_subagent_context_source(
        kind=ContextSourceKind.PARENT_CONTEXT,
        text=parent_context_selection.rendered_body,
        domain_identity={
            "subject": parent_call_subject.subject_fingerprint,
            "selection": parent_context_selection.selection_fingerprint,
            "source": parent_context_selection.source_fingerprint,
        },
    )
    dependency_source = build_subagent_context_source(
        kind=ContextSourceKind.DEPENDENCY_RESULTS,
        text=(None if dependency_context is None else dependency_context.rendered_body),
        domain_identity=(
            None
            if dependency_context is None
            else dependency_context.context_fingerprint
        ),
    )
    payload = {
        "dispatch_read": dispatch_read.composite_fingerprint,
        "task_id": task_id,
        "parent_turn_id": parent_turn_id,
        "profile": profile_kind.value,
        "objective_item": objective_fingerprint,
        "parent_subject": parent_call_subject.subject_fingerprint,
        "parent_selection": parent_context_selection.selection_fingerprint,
        "parent_source": parent_source.domain_semantic_fingerprint,
        "dependency_context": (
            None
            if dependency_context is None
            else dependency_context.context_fingerprint
        ),
        "dependency_source": dependency_source.domain_semantic_fingerprint,
    }
    return SubagentInitialSeed(
        dispatch_read,
        task_id,
        parent_turn_id,
        profile_kind,
        objective,
        objective_item,
        objective_fingerprint,
        parent_call_subject,
        parent_context_selection,
        parent_source,
        dependency_context,
        dependency_source,
        context_fingerprint("pulsara:subagent-initial-seed:v4", payload),
        _SUBAGENT_SEED_AUTHORITY,
    )


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
        if isinstance(seed, SubagentInitialSeed):
            observed = {
                item.source_kind: item
                for item in (*non_trigger_sources.candidates, *non_trigger_sources.absent_facts)
                if item.source_kind
                in {ContextSourceKind.PARENT_CONTEXT, ContextSourceKind.DEPENDENCY_RESULTS}
            }
            expected = {
                item.source_kind: item for item in seed.source_replacements
            }
            if observed != expected or planning.predecessor_view is not None:
                raise ValueError("subagent cold seed sources do not exact-join")


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
    "build_subagent_initial_seed",
]
