"""Exact canonical-cut to provider-open planning coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Protocol
from uuid import uuid4


from pulsara_agent.conversation_kernel.blob import (
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceCollectorPort,
    build_compaction_context_source,
    build_memory_context_source,
    replace_compaction_context_sources,
    replace_frozen_compaction_context_sources,
    replace_frozen_subagent_context_sources,
    replace_memory_context_sources,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionCanonicalRead,
    FrozenCompactionHeadroomPreflight,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.retained_skill import (
    FrozenRetainedSkillContextSelection,
    freeze_retained_skill_context,
    remove_full_tail_duplicates,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    CanonicalColdContinuationSeed,
    CompactionContinuationSeed,
    FrozenColdConversationSeed,
    KernelColdEpochInputAssembler,
    PreparedColdEpochSemanticAssembly,
    SubagentInitialSeed,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SubagentProfileKind,
)
from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
    KernelModelTargetPreparationRequest,
    PreparedKernelModelCall,
    PreparedKernelModelExecution,
    PreparedKernelSemanticModelCall,
    PreparedKernelModelTarget,
)
from pulsara_agent.capability.contracts import (
    EmptyCapabilityEpochPredecessor,
    FrozenCapabilityDispatchCut,
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilitySet,
    FrozenToolCapabilityExposurePlan,
    InstalledCapabilityEpochPredecessor,
)
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.capability.registry import (
    freeze_capability_dispatch_cut_and_views,
    freeze_tool_planning_input,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    freeze_capability_registry_from_owner_snapshots,
)
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.llm.request import (
    FrozenProviderWireInputPlan,
)
from pulsara_agent.llm.provider_replay import (
    build_provider_replay_target_compatibility,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.contracts import (
    WriterLease,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenModelCallMemoryContext,
    MemoryUsePolicy,
    strongest_memory_use_policy,
)
from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolSurfacePlanningPort,
)
from pulsara_agent.conversation_kernel.subagents.runtime_port import (
    SubagentRuntimePort,
)
from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    ProviderSafePointCoordinator,
)
from pulsara_agent.conversation_kernel.steer import (
    MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES,
    AcceptedSteerDispatchBatch,
    PendingPromptSteerFact,
    PreparedSteerSuffixAdmissionPlan,
    build_accepted_steer_dispatch_batch,
    build_prepared_steer_suffix_plan,
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
    build_steer_resource_rejection,
    build_steer_suffix_quote,
    prepared_steer_suffix_plan_identity_fingerprint,
    steer_consumption_candidate_identity_fingerprint,
)
from pulsara_agent.conversation_kernel.steer_consumption import (
    PreparedSteerPlanStale,
    SteerConsumptionCoordinator,
)
from pulsara_agent.model_input.compiler import (
    COMPILER_CONTRACT_VERSION,
    StructuredModelInputCompiler,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
    CapabilityActivationSubjectKind,
    CanonicalInputOriginKind,
    CollectedContextSources,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenCanonicalCompileSnapshot,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    FrozenModelToolSurface,
    PreparedProviderInputCut,
    FrozenCompiledModelInput,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    ToolResultProviderRenderMode,
    canonical_compile_snapshot_fingerprint,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    compiled_message_placements_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    NewTriggerAnchor,
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputEpochView,
    NoNewTriggerAnchor,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
    PreparedProviderInputAppendCandidate,
    ProcessLocalCanonicalFrontier,
    ProcessLocalProviderInputInstallPermit,
    ProviderInputContinuityScope,
    ProviderInputEpochCompatibility,
    provider_input_logical_utf8_bytes,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    FrozenSelectedDurableProviderReplayHydration,
    ProviderReplayHydrationError,
    ProviderReplayHydrationFailureKind,
)

from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.context import (
    context_fingerprint,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentParentContextCallSubject,
    build_parent_context_call_subject,
    build_root_context_unit,
)


class KernelModelPort(Protocol):
    def prepare_target(
        self, request: KernelModelTargetPreparationRequest
    ) -> PreparedKernelModelTarget: ...

    def freeze_native_tool_eligibility(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        tool_facts: tuple,
        retained_direct_inputs: tuple = (),
        deadline_monotonic: float | None = None,
    ) -> FrozenNativeToolWireEligibilitySet: ...

    def materialize_native_tool_projection_set(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        tool_versions: tuple,
        tool_specs: tuple,
        eligibility: FrozenNativeToolWireEligibilitySet,
        deadline_monotonic: float | None = None,
    ) -> FrozenNativeToolProjectionSet: ...

    def bind_tool_surface(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        tool_surface: PreparedKernelToolSurface,
        native_projection_set,
    ) -> PreparedKernelModelCall: ...

    def bind_semantic_tool_surface(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        tool_surface: FrozenModelToolSurface,
        native_projection_set,
    ) -> PreparedKernelSemanticModelCall: ...

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        append_candidate: PreparedProviderInputAppendCandidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> PreparedKernelModelExecution: ...

    def plan_wire_input(
        self,
        *,
        prepared_call: PreparedKernelModelCall,
        compiled_input: FrozenCompiledModelInput,
        predecessor_view: FrozenProviderInputEpochView | None,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None = None,
    ) -> FrozenProviderWireInputPlan: ...

    def resolve_compaction_summary_call(
        self,
        *,
        active_prepared_call: (
            PreparedKernelModelCall | PreparedKernelSemanticModelCall | None
        ) = None,
    ): ...

    def replay_target_for_resolved_call(self, call): ...


@dataclass(frozen=True, slots=True)
class PreparedProviderDispatch:
    handle: PreparedProviderInputHandle
    canonical_read: FrozenCanonicalProviderDispatchRead
    canonical_facts: FrozenCanonicalCompileSnapshot
    planning: FrozenProviderInputAppendPlanningInput
    prepared_call: PreparedKernelModelCall | PreparedKernelSemanticModelCall
    capability_dispatch_cut: FrozenCapabilityDispatchCut
    tool_exposure_plan: FrozenToolCapabilityExposurePlan
    surface_borrow: ProcessLocalToolSurfaceBorrow | None
    sources: CollectedContextSources
    append_result: FrozenProviderInputAppendCompileResult
    memory_context: FrozenModelCallMemoryContext
    compaction_headroom_preflight: FrozenCompactionHeadroomPreflight | None = (
        dataclass_field(default=None, repr=False)
    )
    accepted_steers: AcceptedSteerDispatchBatch | None = None
    cold_semantic: PreparedColdEpochSemanticAssembly | None = dataclass_field(
        default=None, repr=False
    )
    retained_skill_selection: FrozenRetainedSkillContextSelection | None = (
        dataclass_field(default=None, repr=False)
    )
    installed_provider_open: InstalledProviderOpen | None = dataclass_field(
        default=None, repr=False
    )

    def close_surface_borrow(self) -> None:
        if self.surface_borrow is not None:
            self.surface_borrow.close()


@dataclass(frozen=True, slots=True)
class InstalledProviderOpen:
    request: KernelModelExecutionRequest = dataclass_field(repr=False)
    execution: PreparedKernelModelExecution = dataclass_field(repr=False)
    permit: ProcessLocalProviderInputInstallPermit = dataclass_field(repr=False)
    append_candidate: PreparedProviderInputAppendCandidate = dataclass_field(repr=False)
    subagent_parent_context_subject: FrozenSubagentParentContextCallSubject | None = (
        dataclass_field(default=None, repr=False)
    )


class ProviderDispatchCoordinator:
    """Own one exact canonical cut through continuity installation."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        model: KernelModelPort,
        tools: ToolSurfacePlanningPort,
        input_reader: CanonicalProviderInputReader,
        blob_store: PostgresCanonicalBlobStore,
        safe_point: ProviderSafePointCoordinator,
        io_owner: KernelSessionIO,
        context_source_collector: ContextSourceCollectorPort,
        compiler: StructuredModelInputCompiler,
        cold_epoch_assembler: KernelColdEpochInputAssembler,
        capability_planner: KernelToolCapabilityPlanner,
        continuity_owner: HostProviderInputContinuityOwner,
        memory_support: MemoryDispatchSupport,
        deadline_factory: KernelExecutionDeadlineFactory,
        maximum_input_tokens_per_call: int | None,
        maximum_output_tokens_per_call: int,
        workspace_resolver: SessionWorkspaceResolver,
        compaction_owner: HostCompactionRuntimeOwner | None,
        subagent_runtime: SubagentRuntimePort | None,
    ) -> None:
        self._repository = repository
        self._writer_lease = writer_lease
        self._model = model
        self._tools = tools
        self._input_reader = input_reader
        self._blob_store = blob_store
        self._safe_point = safe_point
        self._io = io_owner
        self._context_source_collector = context_source_collector
        self._compiler = compiler
        self._cold_epoch_assembler = cold_epoch_assembler
        self._capability_planner = capability_planner
        self._continuity = continuity_owner
        self._deadlines = deadline_factory
        self._maximum_input_tokens_per_call = maximum_input_tokens_per_call
        self._maximum_output_tokens_per_call = maximum_output_tokens_per_call
        self._workspace_resolver = workspace_resolver
        self._memory_support = memory_support
        self._steer_consumption = SteerConsumptionCoordinator(
            repository=repository,
            writer_lease=writer_lease,
            blob_store=blob_store,
            io_owner=io_owner,
            deadline_factory=deadline_factory,
        )
        self._compaction_owner = compaction_owner
        self._subagent_runtime = subagent_runtime

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def prepare(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        deadline: float,
        allow_steers: bool = True,
        allow_terminal_compaction: bool = False,
        canonical_read_override: FrozenCanonicalProviderDispatchRead | None = None,
        expected_source_read: FrozenCanonicalProviderDispatchRead | None = None,
        force_empty_capability_predecessor: bool = False,
        cold_seed_override: FrozenColdConversationSeed | None = None,
        existing_handle: PreparedProviderInputHandle | None = None,
        compaction_source_replacements: tuple[
            ContextSourceCandidate | ContextSourceAbsentFact, ...
        ] = (),
        compaction_retained_skill_read: FrozenCompactionCanonicalRead | None = None,
        semantic_only: bool = False,
    ) -> PreparedProviderDispatch:
        """Freeze, quote and (when present) consume one exact steer suffix."""

        prepare_surface = getattr(self._tools, "prepare_tool_surface_safe_point", None)
        if prepare_surface is not None:
            prepare_surface()
        freeze_operation = (
            self._safe_point.freeze_compaction_input
            if allow_terminal_compaction
            else self._safe_point.freeze_provider_input
        )
        handle = existing_handle
        if handle is None:
            handle = await self._io.run(
                freeze_operation,
                turn_id=turn_id,
                **({"allow_terminal": True} if allow_terminal_compaction else {}),
                deadline_monotonic=deadline,
            )
        borrow: ProcessLocalToolSurfaceBorrow | None = None
        try:
            await self._resolved_workspace_id(deadline=deadline)
            headroom_preflight = None
            if (
                self._compaction_owner is not None
                and self._compaction_owner.policy.automatic_enabled
                and allow_steers
                and canonical_read_override is None
            ):
                headroom_preflight = await self.read_compaction_headroom_preflight(
                    handle.cut, deadline=deadline
                )
            observed_read = await self.read_dispatch_read(handle.cut, deadline=deadline)
            if canonical_read_override is not None:
                if (
                    expected_source_read is None
                    or observed_read != expected_source_read
                ):
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                    )
                base_read = canonical_read_override
            else:
                if expected_source_read is not None:
                    raise ValueError(
                        "expected source read requires a canonical override"
                    )
                base_read = observed_read
            base_facts = base_read.compile_snapshot
            base_input = base_facts.canonical_input
            identity = base_input.identity
            subagent_seed: SubagentInitialSeed | None = None
            subagent_profile_kind: SubagentProfileKind | None = None
            subagent_source_replacements: tuple[
                ContextSourceCandidate | ContextSourceAbsentFact, ...
            ] = ()
            if identity.conversation_scope_kind is ModelInputScopeKind.SUBAGENT_TASK:
                task_id = identity.scope_subagent_task_id
                if self._subagent_runtime is None or task_id is None:
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                    )
                try:
                    subagent_profile_kind = self._subagent_runtime.profile_kind(
                        task_id=task_id
                    )
                    subagent_source_replacements = (
                        self._subagent_runtime.initial_context_sources(task_id=task_id)
                    )
                    if cold_seed_override is None:
                        subagent_seed = self._subagent_runtime.build_initial_seed(
                            task_id=task_id,
                            dispatch_read=base_read,
                        )
                except Exception as exc:
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                    ) from exc
            scope = ProviderInputContinuityScope(
                session_id=identity.session_id,
                scope_kind=identity.conversation_scope_kind,
                scope_subagent_task_id=identity.scope_subagent_task_id,
            )
            base_frontier = canonical_frontier(base_input, base_facts)
            current_epoch = self._continuity.current_view(scope)
            context_base_changed = (
                current_epoch is not None
                and current_epoch.canonical_frontier.context_base_semantic_identity
                != base_frontier.context_base_semantic_identity
            )
            predecessor_count = (
                0
                if current_epoch is None or context_base_changed
                else len(current_epoch.canonical_frontier.ordered_item_fingerprints)
            )
            base_anchor = _dispatch_anchor(
                base_input,
                predecessor_item_count=predecessor_count,
                model_call_index=model_call_index,
            )

            try:
                builtin_owner = self._tools.sealed_builtin_capability_snapshot(
                    conversation_scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                )
                _require_dispatch_planning_deadline(deadline)
                mcp_owner = self._tools.freeze_mcp_capability_source_snapshot_set(
                    conversation_scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                )
                _require_dispatch_planning_deadline(deadline)
                skill_owner = await self._io.run(
                    self._context_source_collector.freeze_skill_capability_source_snapshot,
                    conversation_scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                    deadline_monotonic=deadline,
                )
                registry = freeze_capability_registry_from_owner_snapshots(
                    builtin=builtin_owner,
                    mcp=mcp_owner,
                    skills=skill_owner,
                )
                _require_dispatch_planning_deadline(deadline)
            except StructuredModelInputCompileError:
                raise
            except TimeoutError as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.DEADLINE_EXPIRED
                ) from exc
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                ) from exc
            try:
                prepared_target = self._model.prepare_target(
                    KernelModelTargetPreparationRequest(
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        model_call_index=model_call_index,
                        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                        maximum_input_tokens=self._maximum_input_tokens_per_call,
                        maximum_output_tokens=self._maximum_output_tokens_per_call,
                    )
                )
                _require_dispatch_planning_deadline(deadline)
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.MODEL_TARGET_PREPARATION_FAILED
                ) from exc
            try:
                if (
                    current_epoch is None
                    or context_base_changed
                    or force_empty_capability_predecessor
                ):
                    capability_predecessor = EmptyCapabilityEpochPredecessor(0)
                    retained_direct_inputs = ()
                else:
                    predecessor_surface = FrozenModelToolSurface(
                        conversation_scope_kind=identity.conversation_scope_kind,
                        tool_specs=current_epoch.tools,
                        surface_fingerprint=(
                            current_epoch.compatibility.tool_surface_fingerprint
                        ),
                    )
                    capability_predecessor = InstalledCapabilityEpochPredecessor(
                        expected_continuity_revision=current_epoch.epoch_revision,
                        continuity_epoch_nonce=current_epoch.epoch_nonce,
                        tool_surface=predecessor_surface,
                        direct_projection_set=(
                            current_epoch.direct_native_projection_set
                        ),
                        mcp_route_projection=current_epoch.mcp_route_projection,
                    )
                    retained_direct_inputs = tuple(
                        zip(
                            current_epoch.direct_native_projection_set.tool_versions,
                            current_epoch.tools,
                            strict=True,
                        )
                    )
                native_eligibility = self._model.freeze_native_tool_eligibility(
                    prepared_target=prepared_target,
                    conversation_scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                    tool_facts=registry.tool_facts,
                    retained_direct_inputs=retained_direct_inputs,
                    deadline_monotonic=deadline,
                )
                _require_dispatch_planning_deadline(deadline)
                mcp_projection_input = (
                    self._tools.freeze_mcp_capability_projection_input(mcp_owner)
                )
                skill_projection_input = self._context_source_collector.freeze_skill_capability_projection_input(
                    skill_owner
                )
                tool_planning_input = freeze_tool_planning_input(
                    predecessor=capability_predecessor,
                    native_wire=native_eligibility,
                    mcp=mcp_projection_input,
                )
                capability_dispatch_cut, tool_view, skill_view = (
                    freeze_capability_dispatch_cut_and_views(
                        conversation_scope_kind=identity.conversation_scope_kind,
                        scope_subagent_task_id=identity.scope_subagent_task_id,
                        registry=registry,
                        tools=tool_planning_input,
                        skills=skill_projection_input,
                    )
                )
                tool_selection = self._capability_planner.select(view=tool_view)
                direct_projection_set = tool_selection.reusable_direct_projection_set
                if direct_projection_set is None:
                    direct_projection_set = (
                        self._model.materialize_native_tool_projection_set(
                            prepared_target=prepared_target,
                            conversation_scope_kind=(identity.conversation_scope_kind),
                            scope_subagent_task_id=(identity.scope_subagent_task_id),
                            tool_versions=tool_selection.direct_tool_versions,
                            tool_specs=(tool_selection.direct_tool_surface.tool_specs),
                            eligibility=native_eligibility,
                            deadline_monotonic=deadline,
                        )
                    )
                tool_exposure_plan = self._capability_planner.finalize(
                    selection=tool_selection,
                    direct_projection_set=direct_projection_set,
                )
                _require_dispatch_planning_deadline(deadline)
                model_surface = tool_exposure_plan.direct_tool_surface
                if semantic_only:
                    prepared_call = self._model.bind_semantic_tool_surface(
                        prepared_target=prepared_target,
                        tool_surface=model_surface,
                        native_projection_set=(
                            tool_exposure_plan.direct_projection_set
                        ),
                    )
                else:
                    surface = self._tools.prepare_planned_tool_surface(
                        plan=tool_exposure_plan,
                        builtin=builtin_owner,
                    )
                    model_surface = surface.model_surface
                    prepared_call = self._model.bind_tool_surface(
                        prepared_target=prepared_target,
                        tool_surface=surface,
                        native_projection_set=(
                            tool_exposure_plan.direct_projection_set
                        ),
                    )
                    borrow = self._tools.borrow_tool_surface(surface)
                _require_dispatch_planning_deadline(deadline)
            except StructuredModelInputCompileError:
                raise
            except TimeoutError as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.DEADLINE_EXPIRED
                ) from exc
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                ) from exc
            retained_skill_selection = None
            if compaction_retained_skill_read is not None:
                inherited_active_skill = self._context_source_collector.freeze_compaction_active_skill_source(
                    current_epoch
                )
                retained_skill_selection = freeze_retained_skill_context(
                    canonical_read=compaction_retained_skill_read,
                    predecessor_epoch=current_epoch,
                    discovery=skill_owner.discovery,
                    estimator=prepared_call.compile_binding.estimator,
                )
                retained_source = build_compaction_context_source(
                    kind=ContextSourceKind.RETAINED_SKILL_CONTEXT,
                    texts=(retained_skill_selection.rendered_body,)
                    if retained_skill_selection.ordered_items
                    else None,
                    domain_identity=(retained_skill_selection.selection_fingerprint),
                    absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                )
                compaction_source_replacements = (
                    *compaction_source_replacements,
                    inherited_active_skill,
                    retained_source,
                )
            try:
                frozen_sources = await self._io.run(
                    self._context_source_collector.freeze_non_trigger_sources,
                    tool_surface=model_surface,
                    canonical_facts=base_facts,
                    tool_exposure_plan=tool_exposure_plan,
                    skill_dispatch_view=skill_view,
                    skill_owner_snapshot=skill_owner,
                    mcp_catalog_snapshot=mcp_owner.catalog_snapshot,
                    deadline_monotonic=deadline,
                )
                if compaction_source_replacements:
                    frozen_sources = replace_frozen_compaction_context_sources(
                        frozen_sources, compaction_source_replacements
                    )
                if subagent_source_replacements:
                    if subagent_profile_kind is None:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                        )
                    frozen_sources = replace_frozen_subagent_context_sources(
                        frozen_sources,
                        subagent_source_replacements,
                        profile_kind=subagent_profile_kind.value,
                    )
            except StructuredModelInputCompileError:
                raise
            except TimeoutError as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.DEADLINE_EXPIRED
                ) from exc
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                ) from exc

            pending = (
                ()
                if (
                    allow_terminal_compaction
                    or not allow_steers
                    or identity.conversation_scope_kind is not ModelInputScopeKind.ROOT
                )
                else await self._io.run(
                    self._repository.read_pending_prompt_steer_facts,
                    session_id=identity.session_id,
                    target_turn_id=turn_id,
                    deadline_monotonic=deadline,
                )
            )
            hydrated = await self._steer_consumption.hydrate_pending(
                pending, deadline=deadline
            )
            selected_plan: PreparedSteerSuffixAdmissionPlan | None = None
            selected_facts: FrozenCanonicalCompileSnapshot | None = None
            selected_sources: CollectedContextSources | None = None
            selected_append: FrozenProviderInputAppendCompileResult | None = None
            selected_memory_context: FrozenModelCallMemoryContext | None = None
            selected_activation_text: str | None = None
            prepared_preference: (
                ContextSourceCandidate | ContextSourceAbsentFact | None
            ) = None
            selected_preference: (
                ContextSourceCandidate | ContextSourceAbsentFact | None
            ) = None
            selected_trigger_disposition: str | None = None
            selected_memory_use_policy = inherited_memory_use_policy

            # A steer batch is appended to the already-admitted ROOT prompt.
            # Classify that exact base prompt first so a new HUMAN_MESSAGE
            # resets the policy epoch even when busy-Enter steers arrived
            # before the first provider dispatch.  The ordered steer prefix
            # below may only strengthen this base policy.
            steer_base_memory_use_policy = inherited_memory_use_policy
            if self._memory_support.available:
                base_activation_subject, base_activation_text = (
                    _activation_subject_for_anchor(base_input, base_anchor)
                )
                if (
                    base_activation_subject
                    is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
                ):
                    base_trigger_policy = self._memory_support.classify_trigger(
                        base_activation_text
                    )
                    base_trigger_origin = _input_origin_for_anchor(
                        base_input, base_anchor
                    )
                    steer_base_memory_use_policy = (
                        base_trigger_policy.memory_use
                        if base_trigger_origin is CanonicalInputOriginKind.HUMAN_MESSAGE
                        else strongest_memory_use_policy(
                            inherited_memory_use_policy,
                            base_trigger_policy.memory_use,
                        )
                    )

            if hydrated:
                all_hydrated = hydrated
                occurred_at = datetime.now(timezone.utc)
                canonical_base_fence = build_steer_canonical_base_fence(base_facts)
                # Nested FIFO prefixes share one immutable canonical base and
                # the same hydrated steer bodies.  Quote that unique physical
                # materialization once; charging the full base per trial could
                # exhaust the planning bound before reaching a valid shorter
                # prefix.  Cooperative deadline checks still bound the at-most
                # 128 compile trials.
                maximum_suffix_items = max(
                    0,
                    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS - len(base_input.items),
                )
                maximum_suffix_bytes = max(
                    0,
                    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
                    - base_input.canonical_utf8_bytes,
                )
                eligible_count = 0
                eligible_bytes = 0
                for _fact, body in hydrated[:maximum_suffix_items]:
                    if eligible_bytes + len(body) > maximum_suffix_bytes:
                        break
                    eligible_bytes += len(body)
                    eligible_count += 1
                planning_work_bytes = base_input.canonical_utf8_bytes + eligible_bytes
                if planning_work_bytes > (MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES):
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
                    )
                hydrated = all_hydrated[:eligible_count]
                for count in range(len(hydrated), 0, -1):
                    if monotonic() >= deadline:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.DEADLINE_EXPIRED
                        )
                    prefix = hydrated[:count]
                    prospective = _prospective_steer_compile_snapshot(
                        base_facts,
                        facts=tuple(item[0] for item in prefix),
                        bodies=tuple(item[1] for item in prefix),
                        deadline_monotonic=deadline,
                    )
                    prospective_input = prospective.canonical_input
                    prospective_frontier = canonical_frontier(
                        prospective_input,
                        prospective,
                        deadline_monotonic=deadline,
                    )
                    anchor = _new_trigger_anchor(prospective_input.items[-1])
                    planning = self._continuity.freeze_planning_input(
                        scope=scope,
                        canonical_frontier=prospective_frontier,
                        dispatch_anchor=anchor,
                    )
                    candidates = tuple(
                        build_steer_consumption_candidate(
                            fact=fact,
                            body_utf8=body,
                            expected_entry_sequence=(
                                base_input.identity.provider_input_through_sequence
                                + index
                            ),
                            predecessor=planning,
                            canonical_base_fence=canonical_base_fence,
                            occurred_at=occurred_at,
                            actor_id=self._writer_lease.guard.writer_owner_id,
                        )
                        for index, (fact, body) in enumerate(prefix, start=1)
                    )
                    activation_text = prefix[-1][1].decode("utf-8")
                    memory_use_policy = steer_base_memory_use_policy
                    trigger_disposition = "ELIGIBLE"
                    if self._memory_support.available:
                        trigger_policies = tuple(
                            self._memory_support.classify_trigger(body.decode("utf-8"))
                            for _fact, body in prefix
                        )
                        for trigger_policy in trigger_policies:
                            memory_use_policy = strongest_memory_use_policy(
                                memory_use_policy,
                                trigger_policy.memory_use,
                            )
                        trigger_disposition = str(trigger_policies[-1].automatic_recall)
                        if memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER:
                            trigger_disposition = str(
                                AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE
                            )
                    try:
                        sources = await self._io.run(
                            self._context_source_collector.complete_frozen_sources,
                            frozen_sources,
                            activation_subject=(
                                CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
                            ),
                            activation_text=activation_text,
                            deadline_monotonic=deadline,
                        )
                    except StructuredModelInputCompileError:
                        raise
                    except Exception as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                        ) from exc
                    effective_preference = prepared_preference
                    if (
                        self._memory_support.available
                        and memory_use_policy
                        is not MemoryUsePolicy.ALL_DISABLED_BY_USER
                        and effective_preference is None
                    ):
                        prepared_preference = await self._memory_support.freeze_response_preference_source()
                        effective_preference = prepared_preference
                    if (
                        self._memory_support.available
                        and memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER
                    ):
                        effective_preference = build_memory_context_source(
                            kind=(ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD),
                            texts=None,
                            absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                        )
                    recall_desired = build_memory_context_source(
                        kind=ContextSourceKind.MEMORY_RECALL,
                        texts=("", "", "")
                        if trigger_disposition == "ELIGIBLE"
                        else None,
                        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                        domain_identity={
                            "pending_trigger": anchor.provider_input_item_fingerprint,
                            "disposition": trigger_disposition,
                        },
                    )
                    # Phase A never materializes optional memory.  The exact
                    # current preference carrier and recall trigger are bound
                    # into typed reservations below; only their mandatory
                    # invalidation ceilings can reject a steer.
                    sources = replace_memory_context_sources(
                        sources,
                        (
                            build_memory_context_source(
                                kind=(
                                    ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
                                ),
                                texts=None,
                                absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE,
                            ),
                            build_memory_context_source(
                                kind=ContextSourceKind.MEMORY_RECALL,
                                texts=None,
                                absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE,
                            ),
                        ),
                    )
                    compile_request = StructuredModelInputCompileRequest(
                        context_id=_stable_id(
                            "model-context",
                            identity.session_id,
                            turn_id,
                            str(model_call_index),
                            steer_consumption_candidate_identity_fingerprint(
                                candidates[-1]
                            ),
                        ),
                        model_call_index=model_call_index,
                        canonical_input=prospective_input,
                        canonical_facts=prospective,
                        compile_binding=prepared_call.compile_binding,
                        sources=sources,
                        dispatch_anchor_entry_id=anchor.source_entry_id,
                        memory_citation_handles=(
                            memory_snapshot := self._memory_support.freeze_call_context(
                                scope=scope,
                                planning=planning,
                                canonical_facts=prospective,
                                sources=sources,
                                memory_use_policy=memory_use_policy,
                            )
                        )[1],
                    )
                    compatibility = provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=prospective,
                        sources=sources,
                    )
                    try:
                        append = await self._io.run(
                            compile_structured_append,
                            self._compiler,
                            compile_request,
                            planning=planning,
                            compatibility=compatibility,
                            deadline_monotonic=deadline,
                        )
                    except StructuredModelInputCompileError as exc:
                        if exc.kind not in {
                            ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
                            ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
                            ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
                            ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
                            ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
                            ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
                        }:
                            raise
                        continue
                    recall_reservation = None
                    preference_reservation = None
                    if effective_preference is not None:
                        recall_reservation, preference_reservation = (
                            self._memory_support.planning_reservations(
                                planning=planning,
                                prepared_preference=effective_preference,
                                recall_desired=recall_desired,
                                compiled=append.compiled_input,
                                prepared_call=prepared_call,
                            )
                        )
                    reservations = tuple(
                        item
                        for item in (
                            recall_reservation,
                            preference_reservation,
                        )
                        if item is not None
                    )
                    if (
                        append.compiled_input.final_estimate.total_input_tokens
                        + sum(
                            item.invalidation_input_token_ceiling
                            for item in reservations
                        )
                        > prepared_call.compile_binding.effective_input_budget_tokens
                        or provider_input_logical_utf8_bytes(
                            system_prompt=append.compiled_input.system_prompt,
                            tools=append.compiled_input.tools,
                            messages=append.compiled_input.messages,
                        )
                        + sum(
                            item.invalidation_epoch_bytes_ceiling
                            for item in reservations
                        )
                        > (64 << 20)
                    ):
                        continue
                    quote = build_steer_suffix_quote(
                        candidates=candidates,
                        prospective_snapshot_hydrated_bytes=(
                            prospective_input.canonical_utf8_bytes
                        ),
                        resulting_epoch_logical_bytes=provider_input_logical_utf8_bytes(
                            system_prompt=append.compiled_input.system_prompt,
                            tools=append.compiled_input.tools,
                            messages=append.compiled_input.messages,
                        ),
                        resulting_target_estimate=append.compiled_input.final_estimate,
                        effective_target_budget=(
                            prepared_call.compile_binding.effective_input_budget_tokens
                        ),
                        estimator_fingerprint=(
                            prepared_call.compile_binding.estimator.fact.estimator_fingerprint
                        ),
                        predecessor_prefix_fingerprint=(
                            None
                            if planning.predecessor_view is None
                            else planning.predecessor_view.semantic_prefix_fingerprint
                        ),
                        memory_recall_reservation=recall_reservation,
                        memory_response_preference_reservation=(preference_reservation),
                    )
                    selected_plan = build_prepared_steer_suffix_plan(
                        scope=scope,
                        predecessor=planning,
                        base_cut_fingerprint=_provider_cut_fingerprint(handle.cut),
                        base_canonical_frontier_fingerprint=(
                            _canonical_frontier_fingerprint(base_frontier)
                        ),
                        base_compile_snapshot_fingerprint=(
                            base_facts.canonical_read_cut_fingerprint
                        ),
                        target_binding_fingerprint=(
                            prepared_call.compile_binding.binding_fingerprint
                        ),
                        tool_surface_fingerprint=(model_surface.surface_fingerprint),
                        source_facts_fingerprint=sources.collection_fingerprint,
                        ordered_pending_queue_facts=tuple(
                            item for item, _body in all_hydrated
                        ),
                        selected_consumption_candidates=candidates,
                        quote=quote,
                        prospective_compiled_input=append.compiled_input,
                    )
                    selected_facts = prospective
                    selected_sources = sources
                    selected_append = append
                    selected_memory_context = memory_snapshot[0]
                    selected_activation_text = activation_text
                    selected_preference = effective_preference
                    selected_trigger_disposition = trigger_disposition
                    selected_memory_use_policy = memory_use_policy
                    break

                if selected_plan is None:
                    rejection = build_steer_resource_rejection(
                        fact=all_hydrated[0][0],
                        occurred_at=occurred_at,
                        actor_id=self._writer_lease.guard.writer_owner_id,
                    )
                    await self._steer_consumption.settle_resource_rejection(
                        rejection,
                    )
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED
                    )
            if selected_plan is not None:
                assert selected_facts is not None
                assert selected_sources is not None
                assert selected_append is not None
                assert selected_memory_context is not None
                assert selected_activation_text is not None
                assert selected_trigger_disposition is not None
                try:
                    if borrow is None:
                        raise RuntimeError("steer dispatch lacks a physical borrow")
                    self._tools.validate_tool_surface_borrow(borrow, surface)
                    if (
                        frozen_sources.registry_fingerprint
                        != self._context_source_collector.registry_fingerprint
                    ):
                        raise RuntimeError("context source registry drifted")
                except Exception as exc:
                    raise PreparedSteerPlanStale(
                        "prepared steer process-local facts changed before consumption"
                    ) from exc
                accepted_entries = await self._steer_consumption.consume_plan(
                    selected_plan
                )
                try:
                    handle = await self._io.run(
                        self._safe_point.rotate_provider_input,
                        handle,
                        turn_id=turn_id,
                        deadline_monotonic=deadline,
                    )
                    actual_read = await self.read_dispatch_read(
                        handle.cut, deadline=deadline
                    )
                    actual = actual_read.compile_snapshot
                    if (
                        actual.canonical_read_cut_fingerprint
                        != selected_facts.canonical_read_cut_fingerprint
                        or actual.canonical_input.snapshot_fingerprint
                        != selected_facts.canonical_input.snapshot_fingerprint
                        or _provider_cut_fingerprint(handle.cut)
                        != _provider_cut_fingerprint(
                            PreparedProviderInputCut(
                                session_id=(
                                    selected_facts.canonical_input.identity.session_id
                                ),
                                turn_id=selected_facts.canonical_input.identity.turn_id,
                                context_binding_revision_id=(
                                    selected_facts.canonical_input.identity.context_binding_revision_id
                                ),
                                provider_input_through_sequence=(
                                    selected_facts.canonical_input.identity.provider_input_through_sequence
                                ),
                            )
                        )
                    ):
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                        )
                except BaseException:
                    await self._steer_consumption.settle_plan_conflict(selected_plan)
                    raise
                batch = build_accepted_steer_dispatch_batch(
                    session_id=identity.session_id,
                    target_turn_id=turn_id,
                    entries=accepted_entries,
                    canonical_utf8_bytes=sum(
                        item.content.size
                        for item in selected_plan.selected_consumption_candidates
                    ),
                    resulting_epoch_logical_bytes=(
                        selected_plan.quote.resulting_epoch_logical_bytes
                    ),
                )
                final_sources = await self._memory_support.apply_sources(
                    selected_sources,
                    activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
                    activation_text=selected_activation_text,
                    include_recall=True,
                    frozen_preference=selected_preference,
                    trigger_disposition=selected_trigger_disposition,
                )
                final_memory = self._memory_support.freeze_call_context(
                    scope=scope,
                    planning=selected_plan.predecessor,
                    canonical_facts=actual,
                    sources=final_sources,
                    memory_use_policy=selected_memory_use_policy,
                )
                final_request = StructuredModelInputCompileRequest(
                    context_id=_stable_id(
                        "model-context-final-steer",
                        identity.session_id,
                        turn_id,
                        str(model_call_index),
                        prepared_steer_suffix_plan_identity_fingerprint(selected_plan),
                    ),
                    model_call_index=model_call_index,
                    canonical_input=actual.canonical_input,
                    canonical_facts=actual,
                    compile_binding=prepared_call.compile_binding,
                    sources=final_sources,
                    dispatch_anchor_entry_id=(
                        actual.canonical_input.items[-1].source_entry_id
                    ),
                    memory_citation_handles=final_memory[1],
                )
                (
                    final_append,
                    final_sources,
                ) = await self._memory_support.compile_with_fallback(
                    request=final_request,
                    planning=selected_plan.predecessor,
                    compatibility=provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=actual,
                        sources=final_sources,
                    ),
                    canonical_facts=actual,
                    sources=final_sources,
                    preference_source=selected_preference,
                    recall_reservation=(selected_plan.quote.memory_recall_reservation),
                    preference_reservation=(
                        selected_plan.quote.memory_response_preference_reservation
                    ),
                    scope=scope,
                    memory_use_policy=selected_memory_use_policy,
                    deadline=deadline,
                )
                final_memory = self._memory_support.freeze_call_context(
                    scope=scope,
                    planning=selected_plan.predecessor,
                    canonical_facts=actual,
                    sources=final_sources,
                    memory_use_policy=selected_memory_use_policy,
                )
                return PreparedProviderDispatch(
                    handle=handle,
                    canonical_read=actual_read,
                    canonical_facts=actual,
                    planning=selected_plan.predecessor,
                    prepared_call=prepared_call,
                    capability_dispatch_cut=capability_dispatch_cut,
                    tool_exposure_plan=tool_exposure_plan,
                    surface_borrow=borrow,
                    sources=final_sources,
                    append_result=final_append,
                    memory_context=final_memory[0],
                    accepted_steers=batch,
                    retained_skill_selection=retained_skill_selection,
                )

            planning = self._continuity.freeze_planning_input(
                scope=scope,
                canonical_frontier=base_frontier,
                dispatch_anchor=base_anchor,
            )
            activation_subject, activation_text = _activation_subject_for_anchor(
                base_input, base_anchor
            )
            if isinstance(cold_seed_override, CompactionContinuationSeed):
                # A compaction successor is the same activation crossing an
                # explicit cold epoch boundary.  Its synthetic base can expose
                # a historical human anchor after context-base replacement,
                # but that anchor is not a new Skill or memory activation.  The
                # successor instead carries the exact prebound ACTIVE_SKILL
                # state recovered from the predecessor epoch.
                activation_subject = None
                activation_text = ""
            try:
                sources = await self._io.run(
                    self._context_source_collector.complete_frozen_sources,
                    frozen_sources,
                    activation_subject=activation_subject,
                    activation_text=activation_text,
                    deadline_monotonic=deadline,
                )
            except StructuredModelInputCompileError:
                raise
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                ) from exc
            base_sources = sources
            preference_source = None
            recall_reservation = None
            preference_reservation = None
            trigger_disposition = None
            memory_use_policy = inherited_memory_use_policy
            if (
                self._memory_support.available
                and activation_subject
                is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
            ):
                trigger_policy = self._memory_support.classify_trigger(activation_text)
                trigger_origin = _input_origin_for_anchor(base_input, base_anchor)
                memory_use_policy = (
                    trigger_policy.memory_use
                    if trigger_origin is CanonicalInputOriginKind.HUMAN_MESSAGE
                    else strongest_memory_use_policy(
                        inherited_memory_use_policy,
                        trigger_policy.memory_use,
                    )
                )
                trigger_disposition = str(trigger_policy.automatic_recall)
                if memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER:
                    trigger_disposition = str(
                        AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE
                    )
                if memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER:
                    preference_source = build_memory_context_source(
                        kind=(ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD),
                        texts=None,
                        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    )
                else:
                    preference_source = (
                        await self._memory_support.freeze_response_preference_source()
                    )
                base_sources = replace_memory_context_sources(
                    sources,
                    (
                        build_memory_context_source(
                            kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                            texts=None,
                            absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE,
                        ),
                        build_memory_context_source(
                            kind=ContextSourceKind.MEMORY_RECALL,
                            texts=None,
                            absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE,
                        ),
                    ),
                )
            compile_request = StructuredModelInputCompileRequest(
                context_id=f"model-context:{uuid4().hex}",
                model_call_index=model_call_index,
                canonical_input=base_input,
                canonical_facts=base_facts,
                compile_binding=prepared_call.compile_binding,
                sources=base_sources,
                dispatch_anchor_entry_id=(
                    base_anchor.source_entry_id
                    if isinstance(base_anchor, NewTriggerAnchor)
                    else None
                ),
                memory_citation_handles=(
                    memory_snapshot := self._memory_support.freeze_call_context(
                        scope=scope,
                        planning=planning,
                        canonical_facts=base_facts,
                        sources=base_sources,
                        memory_use_policy=memory_use_policy,
                    )
                )[1],
            )
            compatibility = provider_input_compatibility(
                prepared_call=prepared_call,
                canonical_facts=base_facts,
                sources=base_sources,
            )
            cold_seed = cold_seed_override
            if cold_seed is not None and cold_seed.dispatch_read != base_read:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            if cold_seed is None and (
                planning.predecessor_view is None or context_base_changed
            ):
                cold_seed = (
                    subagent_seed
                    if subagent_seed is not None
                    else CanonicalColdContinuationSeed(base_read)
                )
            cold_semantic: PreparedColdEpochSemanticAssembly | None = None
            replay_target = provider_replay_target(prepared_call)
            if cold_seed is not None and preference_source is None:
                cold_semantic = await self._io.run(
                    self._cold_epoch_assembler.prepare_semantic,
                    seed=cold_seed,
                    compile_request=compile_request,
                    planning=planning,
                    compatibility=compatibility,
                    prepared_call=prepared_call,
                    capability_dispatch_cut=capability_dispatch_cut,
                    tool_view=tool_view,
                    skill_view=skill_view,
                    tool_exposure_plan=tool_exposure_plan,
                    non_trigger_sources=frozen_sources,
                    replay_target=replay_target,
                    deadline_monotonic=deadline,
                )
                base_append = cold_semantic.compiled_result
            else:
                base_append = await self._io.run(
                    compile_structured_append,
                    self._compiler,
                    compile_request,
                    planning=planning,
                    compatibility=compatibility,
                    deadline_monotonic=deadline,
                )
            final_sources = base_sources
            append = base_append
            if preference_source is not None and trigger_disposition is not None:
                recall_desired = build_memory_context_source(
                    kind=ContextSourceKind.MEMORY_RECALL,
                    texts=("", "", "") if trigger_disposition == "ELIGIBLE" else None,
                    absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    domain_identity={
                        "dispatch_anchor": (
                            None
                            if not isinstance(base_anchor, NewTriggerAnchor)
                            else base_anchor.provider_input_item_fingerprint
                        ),
                        "disposition": trigger_disposition,
                    },
                )
                recall_reservation, preference_reservation = (
                    self._memory_support.planning_reservations(
                        planning=planning,
                        prepared_preference=preference_source,
                        recall_desired=recall_desired,
                        compiled=base_append.compiled_input,
                        prepared_call=prepared_call,
                    )
                )
                final_sources = await self._memory_support.apply_sources(
                    sources,
                    activation_subject=activation_subject,
                    activation_text=activation_text,
                    include_recall=True,
                    frozen_preference=preference_source,
                    trigger_disposition=trigger_disposition,
                )
                final_memory = self._memory_support.freeze_call_context(
                    scope=scope,
                    planning=planning,
                    canonical_facts=base_facts,
                    sources=final_sources,
                    memory_use_policy=memory_use_policy,
                )
                final_request = replace(
                    compile_request,
                    sources=final_sources,
                    memory_citation_handles=final_memory[1],
                )
                (
                    append,
                    final_sources,
                ) = await self._memory_support.compile_with_fallback(
                    request=final_request,
                    planning=planning,
                    compatibility=provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=base_facts,
                        sources=final_sources,
                    ),
                    canonical_facts=base_facts,
                    sources=final_sources,
                    preference_source=preference_source,
                    recall_reservation=recall_reservation,
                    preference_reservation=preference_reservation,
                    scope=scope,
                    memory_use_policy=memory_use_policy,
                    deadline=deadline,
                )
                if cold_seed is not None:
                    final_request = replace(
                        final_request,
                        sources=final_sources,
                        memory_citation_handles=(
                            self._memory_support.freeze_call_context(
                                scope=scope,
                                planning=planning,
                                canonical_facts=base_facts,
                                sources=final_sources,
                                memory_use_policy=memory_use_policy,
                            )[1]
                        ),
                    )
                    final_compatibility = provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=base_facts,
                        sources=final_sources,
                    )
                    cold_semantic = await self._io.run(
                        self._cold_epoch_assembler.prepare_semantic,
                        seed=cold_seed,
                        compile_request=final_request,
                        planning=planning,
                        compatibility=final_compatibility,
                        prepared_call=prepared_call,
                        capability_dispatch_cut=capability_dispatch_cut,
                        tool_view=tool_view,
                        skill_view=skill_view,
                        tool_exposure_plan=tool_exposure_plan,
                        non_trigger_sources=frozen_sources,
                        replay_target=replay_target,
                        deadline_monotonic=deadline,
                    )
                    append = cold_semantic.compiled_result
            if retained_skill_selection is not None and cold_seed is not None:
                for iteration in range(9):
                    full_results = frozenset(
                        item.source_entry_fingerprint
                        for item in append.compiled_input.tool_result_decisions
                        if item.selected_mode is ToolResultProviderRenderMode.FULL
                    )
                    reduced = remove_full_tail_duplicates(
                        retained_skill_selection,
                        full_source_entry_fingerprints=full_results,
                        estimator=prepared_call.compile_binding.estimator,
                    )
                    if reduced == retained_skill_selection:
                        break
                    if iteration >= 8:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                        )
                    retained_skill_selection = reduced
                    retained_source = build_compaction_context_source(
                        kind=ContextSourceKind.RETAINED_SKILL_CONTEXT,
                        texts=(reduced.rendered_body,)
                        if reduced.ordered_items
                        else None,
                        domain_identity=reduced.selection_fingerprint,
                        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    )
                    final_sources = replace_compaction_context_sources(
                        final_sources, (retained_source,)
                    )
                    frozen_sources = replace_frozen_compaction_context_sources(
                        frozen_sources, (retained_source,)
                    )
                    final_memory = self._memory_support.freeze_call_context(
                        scope=scope,
                        planning=planning,
                        canonical_facts=base_facts,
                        sources=final_sources,
                        memory_use_policy=memory_use_policy,
                    )
                    final_request = replace(
                        compile_request,
                        sources=final_sources,
                        memory_citation_handles=final_memory[1],
                    )
                    final_compatibility = provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=base_facts,
                        sources=final_sources,
                    )
                    cold_semantic = await self._io.run(
                        self._cold_epoch_assembler.prepare_semantic,
                        seed=cold_seed,
                        compile_request=final_request,
                        planning=planning,
                        compatibility=final_compatibility,
                        prepared_call=prepared_call,
                        capability_dispatch_cut=capability_dispatch_cut,
                        tool_view=tool_view,
                        skill_view=skill_view,
                        tool_exposure_plan=tool_exposure_plan,
                        non_trigger_sources=frozen_sources,
                        replay_target=replay_target,
                        deadline_monotonic=deadline,
                    )
                    append = cold_semantic.compiled_result
                    recompiled_full = {
                        item.source_entry_fingerprint
                        for item in append.compiled_input.tool_result_decisions
                        if item.selected_mode is ToolResultProviderRenderMode.FULL
                    }
                    if not full_results.issubset(recompiled_full):
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                        )
            memory_snapshot = self._memory_support.freeze_call_context(
                scope=scope,
                planning=planning,
                canonical_facts=base_facts,
                sources=final_sources,
                memory_use_policy=memory_use_policy,
            )
            return PreparedProviderDispatch(
                handle=handle,
                canonical_read=base_read,
                canonical_facts=base_facts,
                planning=planning,
                prepared_call=prepared_call,
                capability_dispatch_cut=capability_dispatch_cut,
                tool_exposure_plan=tool_exposure_plan,
                surface_borrow=borrow,
                sources=final_sources,
                append_result=append,
                memory_context=memory_snapshot[0],
                compaction_headroom_preflight=headroom_preflight,
                cold_semantic=cold_semantic,
                retained_skill_selection=retained_skill_selection,
            )
        except BaseException:
            handle.close()
            if borrow is not None:
                borrow.close()
            raise

    async def read_compile_snapshot(
        self, cut: PreparedProviderInputCut, *, deadline: float
    ) -> FrozenCanonicalCompileSnapshot:
        try:
            return await self._io.run(
                self._input_reader.read_frozen_compile_snapshot,
                cut,
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            ) from exc

    async def read_dispatch_read(
        self, cut: PreparedProviderInputCut, *, deadline: float
    ) -> FrozenCanonicalProviderDispatchRead:
        try:
            return await self._io.run(
                self._input_reader.read_frozen_dispatch,
                cut,
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            ) from exc

    async def read_compaction_headroom_preflight(
        self, cut: PreparedProviderInputCut, *, deadline: float
    ) -> FrozenCompactionHeadroomPreflight:
        try:
            return await self._io.run(
                self._input_reader.read_compaction_headroom_preflight,
                cut,
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            ) from exc

    async def install_provider_open(
        self,
        *,
        dispatch: PreparedProviderDispatch,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> InstalledProviderOpen:
        """Preflight and CAS one exact dispatch without opening transport."""

        prepared_call = dispatch.prepared_call
        borrow = dispatch.surface_borrow
        if borrow is None or not isinstance(prepared_call, PreparedKernelModelCall):
            raise RuntimeError("provider install lacks an execution-backed surface")
        canonical_facts = dispatch.canonical_facts
        compiled_input = dispatch.append_result.compiled_input
        replay_target = provider_replay_target(prepared_call)
        try:
            replay_hydration = await self._io.run(
                self._input_reader.hydrate_selected_provider_replays,
                dispatch_read=dispatch.canonical_read,
                compiled_input=compiled_input,
                replay_target=replay_target,
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            ) from exc
        except ProviderReplayHydrationError as exc:
            kind = (
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
                if exc.kind is ProviderReplayHydrationFailureKind.RESOURCE_BOUNDARY
                else ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
            raise StructuredModelInputCompileError(kind) from exc
        if dispatch.cold_semantic is not None:
            assembly = self._cold_epoch_assembler.finalize_wire(
                dispatch.cold_semantic,
                replay_hydration=replay_hydration,
                wire_planner=lambda **values: self._model.plan_wire_input(
                    prepared_call=prepared_call,
                    **values,
                ),
                deadline_monotonic=deadline,
            )
            if assembly.compiled_input != compiled_input:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            wire_input_plan = assembly.wire_input_plan
            candidate_inputs = assembly.continuity_candidate_inputs
            append_candidate = prepared_append_candidate(
                planning=candidate_inputs.planning,
                compatibility=candidate_inputs.compatibility,
                compiled_result=candidate_inputs.compiled_result,
                wire_input_plan=candidate_inputs.wire_input_plan,
                tool_exposure_plan=candidate_inputs.tool_exposure_plan,
            )
        else:
            compatibility = provider_input_compatibility(
                prepared_call=prepared_call,
                canonical_facts=canonical_facts,
                sources=dispatch.sources,
            )
            wire_input_plan = self._model.plan_wire_input(
                prepared_call=prepared_call,
                compiled_input=compiled_input,
                predecessor_view=(
                    None
                    if dispatch.append_result.reset_reason is not None
                    else dispatch.planning.predecessor_view
                ),
                replay_hydration=replay_hydration,
            )
            append_candidate = prepared_append_candidate(
                planning=dispatch.planning,
                compatibility=compatibility,
                compiled_result=dispatch.append_result,
                wire_input_plan=wire_input_plan,
                tool_exposure_plan=dispatch.tool_exposure_plan,
            )
        self._continuity.register(append_candidate)
        request = KernelModelExecutionRequest(
            session_id=self._writer_lease.guard.session_id,
            turn_id=turn_id,
            model_call_index=model_call_index,
            prepared_call=prepared_call,
            compiled_input=compiled_input,
            wire_input_plan=wire_input_plan,
            cut=dispatch.handle.cut,
            surface_borrow=borrow,
            memory_context=dispatch.memory_context,
        )
        execution: PreparedKernelModelExecution | None = None
        installed = False
        try:
            try:
                for tool in compiled_input.tools:
                    binding = borrow.execution_binding(tool.name)
                    if binding.descriptor_fingerprint != tool.descriptor_fingerprint:
                        raise RuntimeError("tool binding changed")
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                ) from exc
            try:
                execution = self._model.preflight_execution(
                    request,
                    append_candidate=append_candidate,
                    install_authority=self._continuity.install_authority,
                )
            except StructuredModelInputCompileError:
                raise
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
                ) from exc
            dispatch.handle.begin_model_operation()
            permit = self._continuity.install(
                candidate=append_candidate,
                execution=execution,
            )
            installed = True
            self._tools.install_provider_input_tool_result_deliveries(
                permit=permit,
                canonical_facts=canonical_facts,
                compiled_input=compiled_input,
                surface_borrow=borrow,
            )
            return InstalledProviderOpen(
                request=request,
                execution=execution,
                permit=permit,
                append_candidate=append_candidate,
                subagent_parent_context_subject=(
                    _freeze_subagent_parent_context_call_subject(
                        dispatch=dispatch,
                        permit=permit,
                    )
                    if canonical_facts.canonical_input.identity.conversation_scope_kind
                    is ModelInputScopeKind.ROOT
                    else None
                ),
            )
        except BaseException:
            if not installed:
                if execution is not None:
                    try:
                        execution.discard()
                    except RuntimeError:
                        pass
                self._continuity.discard(append_candidate)
            raise

    async def _resolved_workspace_id(self, *, deadline: float | None = None) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline() if deadline is None else deadline
        )


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _activation_subject_for_anchor(
    canonical_input: CanonicalModelInputSnapshot,
    anchor: NewTriggerAnchor | NoNewTriggerAnchor,
) -> tuple[CapabilityActivationSubjectKind | None, str]:
    if (
        canonical_input.identity.conversation_scope_kind
        is ModelInputScopeKind.SUBAGENT_TASK
    ):
        return CapabilityActivationSubjectKind.SUBAGENT_OBJECTIVE, ""
    if isinstance(anchor, NewTriggerAnchor):
        matches = tuple(
            item
            for item in canonical_input.items
            if item.source_entry_id == anchor.source_entry_id
            and provider_input_item_fingerprint(item)
            == anchor.provider_input_item_fingerprint
        )
        if len(matches) != 1:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        item = matches[0]
        if item.input_origin in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
        }:
            return CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT, item.text
        return CapabilityActivationSubjectKind.ROOT_NON_HUMAN_TRIGGER, ""
    # NoNewTriggerAnchor represents a same-turn tool/result continuation.  It
    # must preserve the last activation snapshot rather than re-evaluating the
    # turn as a fresh non-human trigger.
    return None, ""


def _input_origin_for_anchor(
    canonical_input: CanonicalModelInputSnapshot,
    anchor: NewTriggerAnchor | NoNewTriggerAnchor,
) -> CanonicalInputOriginKind | None:
    if not isinstance(anchor, NewTriggerAnchor):
        return None
    matches = tuple(
        item
        for item in canonical_input.items
        if item.source_entry_id == anchor.source_entry_id
        and provider_input_item_fingerprint(item)
        == anchor.provider_input_item_fingerprint
    )
    if len(matches) != 1:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    return matches[0].input_origin


def _compile_structured_input(
    compiler: StructuredModelInputCompiler,
    request: StructuredModelInputCompileRequest,
    *,
    deadline_monotonic: float,
) -> FrozenCompiledModelInput:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("structured model input deadline expired")
    return compiler.compile(request)


def _require_dispatch_planning_deadline(deadline_monotonic: float) -> None:
    """Fail before another synchronous planning stage starts after expiry."""

    if monotonic() >= deadline_monotonic:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.DEADLINE_EXPIRED
        )


def compile_structured_append(
    compiler: StructuredModelInputCompiler,
    request: StructuredModelInputCompileRequest,
    *,
    planning: FrozenProviderInputAppendPlanningInput,
    compatibility: ProviderInputEpochCompatibility,
    deadline_monotonic: float,
) -> FrozenProviderInputAppendCompileResult:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("structured model input deadline expired")
    return compiler.compile_append(
        request,
        planning=planning,
        compatibility=compatibility,
        deadline_monotonic=deadline_monotonic,
    )


def canonical_frontier(
    snapshot: CanonicalModelInputSnapshot,
    facts: FrozenCanonicalCompileSnapshot,
    *,
    deadline_monotonic: float | None = None,
) -> ProcessLocalCanonicalFrontier:
    binding = facts.context_binding_fact
    fingerprints: list[str] = []
    for item in snapshot.items:
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            )
        fingerprints.append(provider_input_item_fingerprint(item))
    return ProcessLocalCanonicalFrontier(
        latest_context_binding_revision_id=binding.binding_revision_id,
        context_base_semantic_identity=binding.context_base_semantic_identity,
        through_sequence=snapshot.identity.provider_input_through_sequence,
        ordered_item_fingerprints=tuple(fingerprints),
    )


def _provider_cut_fingerprint(cut: PreparedProviderInputCut) -> str:
    return context_fingerprint(
        "pulsara:prepared-provider-input-cut:v1",
        {
            "session_id": cut.session_id,
            "turn_id": cut.turn_id,
            "binding_revision": cut.context_binding_revision_id,
            "through_sequence": cut.provider_input_through_sequence,
        },
    )


def _canonical_frontier_fingerprint(frontier: ProcessLocalCanonicalFrontier) -> str:
    return context_fingerprint(
        "pulsara:provider-input-frontier:v1",
        {
            "binding_revision": frontier.latest_context_binding_revision_id,
            "context_base": frontier.context_base_semantic_identity,
            "through": frontier.through_sequence,
            "items": frontier.ordered_item_fingerprints,
        },
    )


def _new_trigger_anchor(item: FrozenProviderInputItem) -> NewTriggerAnchor:
    if item.source_entry_id is None:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    fingerprint = provider_input_item_fingerprint(item)
    return NewTriggerAnchor(
        source_entry_id=item.source_entry_id,
        provider_input_item_fingerprint=fingerprint,
        provider_group_boundary_fingerprint=context_fingerprint(
            "pulsara:provider-group-boundary:v1",
            {
                "entry_id": item.source_entry_id,
                "entry_sequence": item.source_entry_sequence,
                "item": fingerprint,
                "position": "BEFORE_ITEM",
            },
        ),
    )


def _prospective_steer_compile_snapshot(
    base: FrozenCanonicalCompileSnapshot,
    *,
    facts: tuple[PendingPromptSteerFact, ...],
    bodies: tuple[bytes, ...],
    deadline_monotonic: float | None = None,
) -> FrozenCanonicalCompileSnapshot:
    if not facts or len(facts) != len(bodies):
        raise ValueError("prospective steer suffix cardinality is invalid")
    canonical = base.canonical_input
    identity = canonical.identity
    if identity.conversation_scope_kind is not ModelInputScopeKind.ROOT:
        raise ValueError("prospective steer suffix requires ROOT scope")
    start_sequence = identity.provider_input_through_sequence
    appended: list[FrozenProviderInputItem] = []
    for index, (fact, body) in enumerate(zip(facts, bodies, strict=True), start=1):
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            )
        if fact.session_id != identity.session_id or (
            fact.exact_target_turn_id != identity.turn_id
        ):
            raise ValueError("prospective steer fact target differs from input cut")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("prospective steer text is not UTF-8") from exc
        appended.append(
            FrozenProviderInputItem(
                item_kind=FrozenProviderInputItemKind.USER,
                source_entry_id=_stable_id(
                    "steer-entry", fact.session_id, fact.queue_item_id
                ),
                source_entry_sequence=start_sequence + index,
                source_turn_id=identity.turn_id,
                text=text,
                input_origin=CanonicalInputOriginKind.HUMAN_STEER,
            )
        )
    through = start_sequence + len(appended)
    identity_values = {
        "session_id": identity.session_id,
        "turn_id": identity.turn_id,
        "initial_entry_id": identity.initial_entry_id,
        "context_binding_revision_id": identity.context_binding_revision_id,
        "provider_input_through_sequence": through,
        "conversation_scope_kind": identity.conversation_scope_kind,
        "scope_subagent_task_id": identity.scope_subagent_task_id,
    }
    successor_identity = CanonicalModelInputIdentity(
        **identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            **identity_values
        ),
    )
    items = (*canonical.items, *appended)
    canonical_bytes = canonical.canonical_utf8_bytes + sum(len(item) for item in bodies)
    successor_input = CanonicalModelInputSnapshot(
        identity=successor_identity,
        items=items,
        canonical_utf8_bytes=canonical_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=successor_identity,
            items=items,
            canonical_utf8_bytes=canonical_bytes,
            closures=canonical.closures,
            late_outcomes=canonical.late_outcomes,
        ),
        closures=canonical.closures,
        late_outcomes=canonical.late_outcomes,
    )
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.DEADLINE_EXPIRED
        )
    values = {
        "canonical_input": successor_input,
        "context_binding_fact": base.context_binding_fact,
        "run_permission_snapshot": base.run_permission_snapshot,
        "plan_workflow_fact": base.plan_workflow_fact,
        "plan_handoff_fact": base.plan_handoff_fact,
        "approved_plan_materialization_fact": (base.approved_plan_materialization_fact),
        "previous_turn_outcome_fact": base.previous_turn_outcome_fact,
        "tool_observation_freshness_fact": (base.tool_observation_freshness_fact),
    }
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    result = FrozenCanonicalCompileSnapshot(
        **values,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.DEADLINE_EXPIRED
        )
    return result


def _dispatch_anchor(
    snapshot: CanonicalModelInputSnapshot,
    *,
    predecessor_item_count: int,
    model_call_index: int,
) -> NewTriggerAnchor | NoNewTriggerAnchor:
    delta = snapshot.items[predecessor_item_count:]
    if model_call_index == 1:
        candidates = tuple(
            item
            for item in delta
            if item.source_entry_id == snapshot.identity.initial_entry_id
        )
    else:
        candidates = ()
    if len(candidates) > 1:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    if not candidates:
        return NoNewTriggerAnchor(
            predecessor_frontier_fingerprint=(
                None
                if predecessor_item_count == 0
                else context_fingerprint(
                    "pulsara:canonical-frontier-prefix:v1",
                    tuple(
                        provider_input_item_fingerprint(item)
                        for item in snapshot.items[:predecessor_item_count]
                    ),
                )
            )
        )
    item = candidates[0]
    item_fingerprint = provider_input_item_fingerprint(item)
    return NewTriggerAnchor(
        source_entry_id=item.source_entry_id or "",
        provider_input_item_fingerprint=item_fingerprint,
        provider_group_boundary_fingerprint=context_fingerprint(
            "pulsara:provider-group-boundary:v1",
            {
                "entry_id": item.source_entry_id,
                "item": item_fingerprint,
                "sequence": item.source_entry_sequence,
            },
        ),
    )


def provider_input_compatibility(
    *,
    prepared_call: PreparedKernelModelCall | PreparedKernelSemanticModelCall,
    canonical_facts: FrozenCanonicalCompileSnapshot,
    sources: CollectedContextSources,
) -> ProviderInputEpochCompatibility:
    base = next(
        item for item in sources.candidates if item.source_kind.value == "BASE_SYSTEM"
    )
    binding = prepared_call.compile_binding
    return ProviderInputEpochCompatibility(
        compiler_contract_version=COMPILER_CONTRACT_VERSION,
        base_system_semantic_fingerprint=base.source_semantic_fingerprint,
        tool_surface_fingerprint=binding.tool_surface.surface_fingerprint,
        model_target_fingerprint=binding.target_fact.target_fingerprint,
        estimator_fingerprint=binding.estimator_fingerprint,
        provider_message_lowering_contract=context_fingerprint(
            "provider-message-and-native-tool-lowering-contract:v1",
            {
                "messages": PROVIDER_MESSAGE_LOWERING_CONTRACT,
                "native_tools": (
                    prepared_call.native_projection_set.native_function_tool_wire_contract_fingerprint
                ),
            },
        ),
        context_base_semantic_identity=(
            canonical_facts.context_binding_fact.context_base_semantic_identity
        ),
        provider_assistant_replay_contract_fingerprint=(
            prepared_call.call.target.model_profile.provider_profile.assistant_replay_contract_fingerprint
        ),
    )


def provider_replay_target(
    prepared_call: PreparedKernelModelCall,
):
    profile = prepared_call.call.target.model_profile.provider_profile
    return build_provider_replay_target_compatibility(
        wire_api=profile.wire_api,
        endpoint_identity_fingerprint=(
            prepared_call.call.target.fact.endpoint_fingerprint
        ),
        normalized_model_identifier=prepared_call.call.target.fact.model_id,
        transport_binding_id=(prepared_call.call.target.fact.transport_binding_id),
    )


def prepared_append_candidate(
    *,
    planning: FrozenProviderInputAppendPlanningInput,
    compatibility: ProviderInputEpochCompatibility,
    compiled_result: FrozenProviderInputAppendCompileResult,
    wire_input_plan: FrozenProviderWireInputPlan,
    tool_exposure_plan: FrozenToolCapabilityExposurePlan,
) -> PreparedProviderInputAppendCandidate:
    predecessor = planning.predecessor_view
    epoch_nonce = (
        f"provider-input-epoch:{uuid4().hex}"
        if predecessor is None or compiled_result.reset_reason is not None
        else predecessor.epoch_nonce
    )
    expected_revision = 0 if predecessor is None else predecessor.epoch_revision
    return PreparedProviderInputAppendCandidate(
        planning=planning,
        epoch_nonce=epoch_nonce,
        expected_epoch_revision=expected_revision,
        resulting_compiled_input=compiled_result.compiled_input,
        wire_input_plan=wire_input_plan,
        resulting_canonical_frontier=compiled_result.canonical_frontier,
        resulting_source_heads=compiled_result.source_heads,
        appended_message_count=compiled_result.appended_message_count,
        reset_reason=compiled_result.reset_reason,
        compatibility=compatibility,
        tool_exposure_plan=tool_exposure_plan,
    )


def _freeze_subagent_parent_context_call_subject(
    *,
    dispatch: PreparedProviderDispatch,
    permit: ProcessLocalProviderInputInstallPermit,
) -> FrozenSubagentParentContextCallSubject:
    """Derive the bounded public ROOT tail from the exact installed call.

    Placements are the join between compiled messages and canonical entry
    identity.  Tool roles/groups and all placement-less runtime sources are
    excluded; assistant messages contribute public text only.
    """

    compiled = dispatch.append_result.compiled_input
    canonical = dispatch.canonical_facts.canonical_input
    by_entry = {
        item.source_entry_id: item
        for item in canonical.items
        if item.source_entry_id is not None
    }
    units_buffer: list[tuple[list[str], list[str]]] = []
    current_entries: list[str] = []
    current_items: list[str] = []
    current_has_assistant = False

    def finish_current_unit() -> None:
        nonlocal current_entries, current_items, current_has_assistant
        if current_items:
            units_buffer.append((current_entries, current_items))
        current_entries = []
        current_items = []
        current_has_assistant = False

    for message, placement in zip(
        compiled.messages, compiled.message_placements, strict=True
    ):
        entry_id = placement.origin_entry_id
        item = None if entry_id is None else by_entry.get(entry_id)
        if item is None or item.source_turn_id is None:
            continue
        rendered: str | None = None
        is_assistant = False
        if (
            message.role is MessageRole.USER
            and item.item_kind is FrozenProviderInputItemKind.USER
            and item.input_origin
            in {
                CanonicalInputOriginKind.HUMAN_MESSAGE,
                CanonicalInputOriginKind.HUMAN_STEER,
            }
        ):
            # Several ordinary USER_MESSAGE values can enter one installed
            # provider call before any assistant response (queue admission),
            # while USER_STEER extends the current unit.  A later human message
            # starts a new unit only after public assistant output closed the
            # preceding one.  Canonical turn ids therefore cannot be used as
            # the grouping key.
            if (
                item.input_origin is CanonicalInputOriginKind.HUMAN_MESSAGE
                and current_has_assistant
            ):
                finish_current_unit()
            rendered = "USER: " + "".join(message.content)
        elif (
            message.role is MessageRole.ASSISTANT
            and item.item_kind
            in {
                FrozenProviderInputItemKind.ASSISTANT,
                FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
            }
            and any(message.content)
        ):
            rendered = "ASSISTANT: " + "".join(message.content)
            is_assistant = True
        if rendered is None:
            continue
        current_items.append(rendered)
        if entry_id not in current_entries:
            current_entries.append(entry_id)
        current_has_assistant = current_has_assistant or is_assistant
    finish_current_unit()
    units = tuple(
        build_root_context_unit(
            ordered_entry_ids=entry_ids,
            ordered_public_items=public_items,
        )
        for entry_ids, public_items in units_buffer[-3:]
    )
    cut = dispatch.handle.cut
    return build_parent_context_call_subject(
        session_id=cut.session_id,
        caller_turn_id=cut.turn_id,
        provider_input_cut_fingerprint=context_fingerprint(
            "pulsara:round10:provider-input-cut:v1",
            {
                "session": cut.session_id,
                "turn": cut.turn_id,
                "binding": cut.context_binding_revision_id,
                "through": cut.provider_input_through_sequence,
            },
        ),
        continuity_epoch_nonce=permit.epoch_nonce,
        continuity_epoch_revision=permit.epoch_revision,
        compiled_semantic_input_fingerprint=compiled.compiled_semantic_fingerprint,
        compiled_message_placements_fingerprint=(
            compiled_message_placements_fingerprint(compiled.message_placements)
        ),
        ordered_eligible_units=units,
    )


__all__ = [
    "InstalledProviderOpen",
    "KernelModelPort",
    "PreparedProviderDispatch",
    "ProviderDispatchCoordinator",
    "canonical_frontier",
    "compile_structured_append",
    "prepared_append_candidate",
    "provider_input_compatibility",
    "provider_replay_target",
]
