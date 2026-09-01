"""Pure, provider-neutral assembly for the first open of a cold epoch.

This module deliberately owns no I/O and no execution authority.  Callers
freeze the canonical cut, Round 9 capability cut/views, context sources and
model binding before entering here. Replay bodies are hydrated and materialized
once by the shared wire planner; this assembler only binds its already-admitted
plan to the cold continuity candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

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
    dependency_result_context_identity_digest,
    parent_context_call_subject_identity_digest,
    parent_context_selection_identity_digest,
    parent_context_source_identity_digest,
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
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    ProviderInputEpochCompatibility,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.direct_model import (
        PreparedKernelModelCall,
        PreparedKernelSemanticModelCall,
    )


@dataclass(frozen=True, slots=True)
class CanonicalColdContinuationSeed:
    """Exact canonical continuation used by ordinary fresh/restart opens."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)


@dataclass(frozen=True, slots=True)
class CompactionContinuationSeed:
    """Exact cold base used to prove every compaction candidate."""

    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    binding_rewrite_identity: str
    protected_tail_selection_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.binding_rewrite_identity
            or not self.protected_tail_selection_fingerprint.startswith("sha256:")
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
    parent_call_subject: FrozenSubagentParentContextCallSubject = field(repr=False)
    parent_context_selection: FrozenSubagentParentContextSelection = field(repr=False)
    parent_context_source: ContextSourceCandidate | ContextSourceAbsentFact = field(
        repr=False
    )
    dependency_context: FrozenDependencyResultContext | None = field(repr=False)
    dependency_results_source: ContextSourceCandidate | ContextSourceAbsentFact = field(
        repr=False
    )
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
        expected_selection = build_parent_context_selection(
            self.parent_call_subject,
            mode=self.parent_context_selection.mode,
            last_n_turns=self.parent_context_selection.last_n_turns,
        )
        expected_parent_source = build_subagent_context_source(
            kind=ContextSourceKind.PARENT_CONTEXT,
            text=expected_selection.rendered_body,
            domain_identity={
                "subject": parent_context_call_subject_identity_digest(
                    self.parent_call_subject
                ),
                "selection": parent_context_selection_identity_digest(
                    self.parent_call_subject, expected_selection
                ),
                "source": parent_context_source_identity_digest(
                    self.parent_call_subject, expected_selection
                ),
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
                else dependency_result_context_identity_digest(self.dependency_context)
            ),
        )
        parent_present = isinstance(self.parent_context_source, ContextSourceCandidate)
        dependency_present = isinstance(
            self.dependency_results_source, ContextSourceCandidate
        )
        if (
            self._authority is not _SUBAGENT_SEED_AUTHORITY
            or identity.conversation_scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
            or identity.scope_subagent_task_id != self.task_id
            or not self.parent_turn_id
            or self.parent_call_subject.caller_turn_id != self.parent_turn_id
            or not isinstance(self.profile_kind, SubagentProfileKind)
            or len(objective_items) != 1
            or objective_items[0] != self.objective_item
            or self.objective_item.text != self.objective
            or self.objective_item.source_turn_id != identity.turn_id
            or self.parent_call_subject.session_id != identity.session_id
            or self.parent_context_selection != expected_selection
            or self.parent_context_source != expected_parent_source
            or self.dependency_results_source != expected_dependency_source
            or (
                self.parent_context_selection.mode is SubagentContextMode.NONE
                and parent_present
            )
            or bool(self.parent_context_selection.selected_units) != parent_present
            or self.parent_context_source.source_kind
            is not ContextSourceKind.PARENT_CONTEXT
            or (self.dependency_context is None) == dependency_present
            or self.dependency_results_source.source_kind
            is not ContextSourceKind.DEPENDENCY_RESULTS
            or (
                self.dependency_context is not None
                and self.dependency_context.target_task_id != self.task_id
            )
        ):
            raise ValueError("subagent initial seed is invalid")

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
    parent_source = build_subagent_context_source(
        kind=ContextSourceKind.PARENT_CONTEXT,
        text=parent_context_selection.rendered_body,
        domain_identity={
            "subject": parent_context_call_subject_identity_digest(parent_call_subject),
            "selection": parent_context_selection_identity_digest(
                parent_call_subject, parent_context_selection
            ),
            "source": parent_context_source_identity_digest(
                parent_call_subject, parent_context_selection
            ),
        },
    )
    dependency_source = build_subagent_context_source(
        kind=ContextSourceKind.DEPENDENCY_RESULTS,
        text=(None if dependency_context is None else dependency_context.rendered_body),
        domain_identity=(
            None
            if dependency_context is None
            else dependency_result_context_identity_digest(dependency_context)
        ),
    )
    return SubagentInitialSeed(
        dispatch_read,
        task_id,
        parent_turn_id,
        profile_kind,
        objective,
        objective_item,
        parent_call_subject,
        parent_context_selection,
        parent_source,
        dependency_context,
        dependency_source,
        _SUBAGENT_SEED_AUTHORITY,
    )


FrozenColdConversationSeed = (
    CanonicalColdContinuationSeed | CompactionContinuationSeed | SubagentInitialSeed
)


@dataclass(frozen=True, slots=True)
class PreparedColdEpochSemanticAssembly:
    seed: FrozenColdConversationSeed = field(repr=False)
    planning: FrozenProviderInputAppendPlanningInput = field(repr=False)
    compatibility: ProviderInputEpochCompatibility
    compiled_result: FrozenProviderInputAppendCompileResult = field(repr=False)
    prepared_call: "PreparedKernelModelCall | PreparedKernelSemanticModelCall" = field(
        repr=False
    )
    capability_dispatch_cut: FrozenCapabilityDispatchCut = field(repr=False)
    tool_view: FrozenToolCapabilityDispatchView = field(repr=False)
    skill_view: FrozenSkillCapabilityDispatchView = field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = field(repr=False)
    non_trigger_sources: FrozenNonTriggerContextSources = field(repr=False)
    replay_target: ProviderReplayTargetCompatibilityFact


@dataclass(frozen=True, slots=True)
class ColdEpochContinuityCandidateInputs:
    planning: FrozenProviderInputAppendPlanningInput = field(repr=False)
    compatibility: ProviderInputEpochCompatibility
    compiled_result: FrozenProviderInputAppendCompileResult = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.wire_input_plan.compiled_semantic_fingerprint
            != self.compiled_result.compiled_input.compiled_semantic_fingerprint
            or self.wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool
                for item in self.tool_exposure_plan.direct_projection_set.projections
            )
        ):
            raise ValueError("cold epoch continuity inputs do not exact-join")

    @property
    def capability_dispatch_cut(self) -> FrozenCapabilityDispatchCut:
        return self.tool_exposure_plan.dispatch_view.parent_dispatch_cut

    @property
    def direct_native_projection_set(self) -> FrozenNativeToolProjectionSet:
        return self.tool_exposure_plan.direct_projection_set

    @property
    def mcp_route_projection(self) -> FrozenMcpRouteProjection:
        return self.tool_exposure_plan.mcp_catalog_route_projection


@dataclass(frozen=True, slots=True)
class ColdEpochInputAssemblyResult:
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    continuity_candidate_inputs: ColdEpochContinuityCandidateInputs = field(repr=False)


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
        prepared_call: "PreparedKernelModelCall | PreparedKernelSemanticModelCall",
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
            prepared_call=prepared_call,
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
        return PreparedColdEpochSemanticAssembly(
            seed=seed,
            planning=planning,
            compatibility=compatibility,
            compiled_result=compiled_result,
            prepared_call=prepared_call,
            capability_dispatch_cut=capability_dispatch_cut,
            tool_view=tool_view,
            skill_view=skill_view,
            tool_exposure_plan=tool_exposure_plan,
            non_trigger_sources=non_trigger_sources,
            replay_target=replay_target,
        )

    def bind_prepared_wire(
        self,
        prepared: PreparedColdEpochSemanticAssembly,
        *,
        wire_input_plan: FrozenProviderWireInputPlan,
        deadline_monotonic: float,
    ) -> ColdEpochInputAssemblyResult:
        """Build continuity inputs from an already-materialized wire plan."""

        self._require_deadline(deadline_monotonic)
        if (
            wire_input_plan.compiled_semantic_fingerprint
            != prepared.compiled_result.compiled_input.compiled_semantic_fingerprint
        ):
            raise ValueError("cold epoch wire plan changed semantic input")
        candidate_inputs = ColdEpochContinuityCandidateInputs(
            planning=prepared.planning,
            compatibility=prepared.compatibility,
            compiled_result=prepared.compiled_result,
            wire_input_plan=wire_input_plan,
            tool_exposure_plan=prepared.tool_exposure_plan,
        )
        return ColdEpochInputAssemblyResult(
            compiled_input=prepared.compiled_result.compiled_input,
            wire_input_plan=wire_input_plan,
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
        prepared_call: "PreparedKernelModelCall | PreparedKernelSemanticModelCall",
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
            compile_request.canonical_facts != seed.dispatch_read.compile_snapshot
            or compile_request.canonical_input != canonical
            or planning.scope.session_id != identity.session_id
            or planning.scope.scope_kind is not identity.conversation_scope_kind
            or planning.scope.scope_subagent_task_id != identity.scope_subagent_task_id
            or capability_dispatch_cut.conversation_scope_kind
            is not identity.conversation_scope_kind
            or capability_dispatch_cut.scope_subagent_task_id
            != identity.scope_subagent_task_id
            or tool_view.parent_dispatch_cut is not capability_dispatch_cut
            or skill_view.parent_dispatch_cut is not capability_dispatch_cut
            or tool_exposure_plan.dispatch_view is not tool_view
            or non_trigger_sources.tool_exposure_plan != tool_exposure_plan
            or non_trigger_sources.skill_dispatch_view != skill_view
            or compile_request.compile_binding.tool_surface
            != tool_exposure_plan.direct_tool_surface
            or prepared_call.compile_binding is not compile_request.compile_binding
            or prepared_call.native_projection_set
            is not tool_exposure_plan.direct_projection_set
            or prepared_call.session_id != identity.session_id
            or prepared_call.turn_id != identity.turn_id
        ):
            raise ValueError("cold epoch inputs do not exact-join")
        if isinstance(seed, SubagentInitialSeed):
            observed = {
                item.source_kind: item
                for item in (
                    *non_trigger_sources.candidates,
                    *non_trigger_sources.absent_facts,
                )
                if item.source_kind
                in {
                    ContextSourceKind.PARENT_CONTEXT,
                    ContextSourceKind.DEPENDENCY_RESULTS,
                }
            }
            expected = {item.source_kind: item for item in seed.source_replacements}
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
    "SubagentInitialSeed",
    "build_subagent_initial_seed",
]
