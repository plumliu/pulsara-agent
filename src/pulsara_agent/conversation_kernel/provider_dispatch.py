"""Exact canonical-cut to provider-open planning coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timezone
from hashlib import sha256
from threading import Lock
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
    replace_frozen_hook_context_source,
    replace_frozen_subagent_context_sources,
    replace_hook_context_source,
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
    ProviderWireMeasurement,
    provider_wire_profile_fingerprint,
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
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputQuote,
    FrozenProviderWireInputPlan,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
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
    SubagentCompletionDisposition,
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
from pulsara_agent.hooks.context import HookContextReservation
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
    ProviderWireSemanticInput,
    ModelInputCompileFailureKind,
    ModelInputCompileBinding,
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
    FrozenProviderInputAppendSemanticProjection,
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
from pulsara_agent.primitives.permission import PermissionMode
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


# One ordinary pending-input transaction stays below the compaction contract's
# reserved 296 items / 4 MiB next-admission headroom even when every completion
# occupies the maximum 64 KiB canonical content carrier.  This is a per-plan
# physical bound, never a session or task-history limit; the answer fence keeps
# the exact ROOT turn open while later FIFO batches remain queued.
_ROOT_COMPLETION_SUFFIX_BATCH_ITEMS = 16


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

    def freeze_wire_measurement(
        self,
        *,
        call,
        compile_binding: ModelInputCompileBinding,
        native_projection_set: FrozenNativeToolProjectionSet,
        semantic_input: ProviderWireSemanticInput,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
        tool_choice: str | None = None,
    ) -> ProviderWireMeasurement: ...

    def resolve_compaction_summary_call(
        self,
        *,
        active_prepared_call: (
            PreparedKernelModelCall | PreparedKernelSemanticModelCall | None
        ) = None,
    ): ...

    def replay_target_for_resolved_call(self, call): ...


@dataclass(frozen=True, slots=True)
class PreparedProviderWireCandidate:
    """Authority-free structural input for one exact wire measurement."""

    canonical_read: FrozenCanonicalProviderDispatchRead = dataclass_field(repr=False)
    semantic_input: ProviderWireSemanticInput = dataclass_field(repr=False)
    prepared_call: PreparedKernelModelCall | PreparedKernelSemanticModelCall = (
        dataclass_field(repr=False)
    )
    native_projection_set: FrozenNativeToolProjectionSet = dataclass_field(repr=False)
    tool_choice: str | None = None
    planning: FrozenProviderInputAppendPlanningInput | None = dataclass_field(
        default=None, repr=False
    )
    append_result: FrozenProviderInputAppendCompileResult | None = dataclass_field(
        default=None, repr=False
    )
    cold_semantic: PreparedColdEpochSemanticAssembly | None = dataclass_field(
        default=None, repr=False
    )
    sources: CollectedContextSources | None = dataclass_field(default=None, repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan | None = dataclass_field(
        default=None, repr=False
    )
    memory_context: FrozenModelCallMemoryContext | None = dataclass_field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if (
            self.semantic_input.canonical_input_identity
            != self.canonical_read.compile_snapshot.canonical_input.identity
            or self.semantic_input.compile_binding_fingerprint
            != self.prepared_call.compile_binding.binding_fingerprint
            or self.native_projection_set != self.prepared_call.native_projection_set
        ):
            raise ValueError("provider wire candidate does not exact-join")
        dispatch_values = (
            self.planning,
            self.append_result,
            self.sources,
            self.tool_exposure_plan,
            self.memory_context,
        )
        if any(item is not None for item in dispatch_values) and any(
            item is None for item in dispatch_values
        ):
            raise ValueError("provider wire dispatch candidate is incomplete")
        if (
            self.append_result is not None
            and self.append_result.compiled_input != self.semantic_input
        ):
            raise ValueError("provider wire candidate changed its compiled input")

    @property
    def call(self) -> ResolvedModelCall:
        return self.prepared_call.call

    @property
    def compile_binding(self) -> ModelInputCompileBinding:
        return self.prepared_call.compile_binding


class ProviderWireMeasurementCandidate(Protocol):
    """Authority-free structural seam shared by dispatch and summary input."""

    @property
    def canonical_read(self) -> FrozenCanonicalProviderDispatchRead: ...

    @property
    def semantic_input(self) -> ProviderWireSemanticInput: ...

    @property
    def call(self) -> ResolvedModelCall: ...

    @property
    def compile_binding(self) -> ModelInputCompileBinding: ...

    @property
    def native_projection_set(self) -> FrozenNativeToolProjectionSet: ...

    @property
    def tool_choice(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PreparedWireMeasurementDecision:
    """Closed quote and, only after hard admission, its same-pass plan."""

    candidate: ProviderWireMeasurementCandidate = dataclass_field(repr=False)
    quote: FrozenProviderWireInputQuote
    wire_input_plan: FrozenProviderWireInputPlan | None = dataclass_field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        candidate = self.candidate
        admitted = (
            self.quote.final_wire_estimated_input_tokens
            <= self.quote.effective_input_budget_tokens
            and self.quote.final_wire_utf8_bytes <= MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
        )
        if admitted != (self.wire_input_plan is not None):
            raise ValueError("wire measurement decision has invalid admission union")
        if (
            self.quote.wire_api
            != candidate.call.target.model_profile.provider_profile.wire_api
            or self.quote.estimator_fingerprint
            != candidate.compile_binding.estimator_fingerprint
            or self.quote.effective_input_budget_tokens
            != candidate.compile_binding.effective_input_budget_tokens
            or self.quote.semantic_estimated_input_tokens
            != candidate.semantic_input.final_estimate.total_input_tokens
        ):
            raise ValueError("wire measurement decision does not join its candidate")
        plan = self.wire_input_plan
        if plan is not None and (
            plan.quote != self.quote
            or plan.compiled_semantic_fingerprint
            != candidate.semantic_input.compiled_semantic_fingerprint
            or plan.message_placements_fingerprint
            != compiled_message_placements_fingerprint(
                candidate.semantic_input.message_placements
            )
            or plan.resolved_target_semantic_fingerprint
            != candidate.call.target.fact.target_fingerprint
            or plan.provider_profile_fingerprint
            != provider_wire_profile_fingerprint(candidate.call)
            or plan.materialization.tool_items
            != tuple(
                item.wire_tool for item in candidate.native_projection_set.projections
            )
        ):
            raise ValueError("wire measurement decision changed its candidate")


class HandleFreeProviderWireObservation:
    """One-shot reusable measurement with no physical/install authority."""

    def __init__(
        self,
        candidate: ProviderWireMeasurementCandidate,
        measurement: ProviderWireMeasurement,
    ) -> None:
        self._candidate = candidate
        self._measurement: ProviderWireMeasurement | None = measurement
        self._lock = Lock()

    def take_for(
        self, candidate: ProviderWireMeasurementCandidate
    ) -> ProviderWireMeasurement | None:
        with self._lock:
            measurement = self._measurement
            self._measurement = None
        if measurement is None:
            raise RuntimeError("provider wire observation is already consumed")
        if _same_provider_wire_measurement_candidate(candidate, self._candidate):
            return measurement
        measurement.discard_materialization_to_quote()
        return None

    def discard(self) -> FrozenProviderWireInputQuote:
        with self._lock:
            measurement = self._measurement
            self._measurement = None
        if measurement is None:
            raise RuntimeError("provider wire observation is already consumed")
        return measurement.discard_materialization_to_quote()


@dataclass(frozen=True, slots=True)
class PreparedExecutableProviderWireInput:
    """Non-owning exact dispatch binding for one already-prepared wire plan."""

    owner_dispatch: "PreparedProviderDispatch" = dataclass_field(repr=False)
    quote: FrozenProviderWireInputQuote
    wire_input_plan: FrozenProviderWireInputPlan = dataclass_field(repr=False)
    selected_candidate: PreparedProviderWireCandidate = dataclass_field(repr=False)
    append_candidate: PreparedProviderInputAppendCandidate = dataclass_field(repr=False)


class ProviderDispatchExecutionAuthority:
    """One-shot owner of the physical resources for one provider dispatch."""

    __slots__ = ("_handle", "_surface_borrow", "_lock")

    def __init__(
        self,
        handle: PreparedProviderInputHandle,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> None:
        if handle is None or surface_borrow is None:
            raise ValueError("provider execution authority is incomplete")
        self._handle: PreparedProviderInputHandle | None = handle
        self._surface_borrow: ProcessLocalToolSurfaceBorrow | None = surface_borrow
        self._lock = Lock()

    @property
    def cut(self) -> PreparedProviderInputCut:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError("provider execution authority is already consumed")
        return handle.cut

    def require_for_install(
        self,
    ) -> tuple[PreparedProviderInputHandle, ProcessLocalToolSurfaceBorrow]:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        return handle, borrow

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        try:
            handle.close()
        finally:
            borrow.close()

    def take_handle_for_rotation(self) -> PreparedProviderInputHandle:
        """Consume the authority while retiring the obsolete Tool borrow."""

        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        borrow.close()
        return handle

    def finish_model_operation(self) -> ProcessLocalToolSurfaceBorrow:
        """Close the safe-point handle and transfer the Tool borrow to execution."""

        with self._lock:
            handle = self._handle
            borrow = self._surface_borrow
            self._handle = None
            self._surface_borrow = None
        if handle is None or borrow is None:
            raise RuntimeError("provider execution authority is already consumed")
        try:
            handle.close()
        except BaseException:
            borrow.close()
            raise
        return borrow


@dataclass(slots=True)
class PreparedProviderDispatch:
    _execution_authority: ProviderDispatchExecutionAuthority | None = dataclass_field(
        repr=False, compare=False
    )
    canonical_read: FrozenCanonicalProviderDispatchRead
    canonical_facts: FrozenCanonicalCompileSnapshot
    planning: FrozenProviderInputAppendPlanningInput
    prepared_call: PreparedKernelModelCall | PreparedKernelSemanticModelCall
    capability_dispatch_cut: FrozenCapabilityDispatchCut
    tool_exposure_plan: FrozenToolCapabilityExposurePlan
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
    _hook_context_reservation: HookContextReservation | None = dataclass_field(
        default=None, repr=False, compare=False
    )
    _authority_lock: Lock = dataclass_field(
        default_factory=Lock, init=False, repr=False, compare=False
    )
    _install_started: bool = dataclass_field(
        default=False, init=False, repr=False, compare=False
    )
    _installed_sealed: bool = dataclass_field(
        default=False, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            self._execution_authority is None
            or self.canonical_facts != self.canonical_read.compile_snapshot
            or not isinstance(self.prepared_call, PreparedKernelModelCall)
            or self.append_result.compiled_input.canonical_input_identity
            != self.canonical_facts.canonical_input.identity
            or self.append_result.compiled_input.compile_binding_fingerprint
            != self.prepared_call.compile_binding.binding_fingerprint
        ):
            raise ValueError("prepared provider dispatch does not exact-join")

    @property
    def cut(self) -> PreparedProviderInputCut:
        with self._authority_lock:
            authority = self._execution_authority
        if authority is None:
            raise RuntimeError("provider dispatch execution authority is consumed")
        return authority.cut

    @property
    def owns_execution_authority(self) -> bool:
        with self._authority_lock:
            return self._execution_authority is not None

    @property
    def has_hook_context_reservation(self) -> bool:
        with self._authority_lock:
            return self._hook_context_reservation is not None

    def take_execution_authority(self) -> ProviderDispatchExecutionAuthority:
        with self._authority_lock:
            authority = self._execution_authority
            self._execution_authority = None
        if authority is None:
            raise RuntimeError("provider dispatch execution authority is consumed")
        return authority

    def take_handle_for_rotation(self) -> PreparedProviderInputHandle:
        return self.take_execution_authority().take_handle_for_rotation()

    def claim_install_authority(
        self,
    ) -> tuple[PreparedProviderInputHandle, ProcessLocalToolSurfaceBorrow]:
        with self._authority_lock:
            authority = self._execution_authority
            if authority is None:
                raise RuntimeError("provider dispatch execution authority is consumed")
            if self._install_started:
                raise RuntimeError("provider dispatch install is already consumed")
            self._install_started = True
        return authority.require_for_install()

    def finish_model_operation(self) -> ProcessLocalToolSurfaceBorrow:
        authority = self.take_execution_authority()
        try:
            return authority.finish_model_operation()
        finally:
            self.retire_hook_context_reservation()

    def retire_hook_context_reservation(self) -> None:
        with self._authority_lock:
            reservation = self._hook_context_reservation
            self._hook_context_reservation = None
        if reservation is not None:
            reservation.retire()

    def close(self) -> None:
        authority = self.take_execution_authority()
        try:
            authority.close()
        finally:
            self.retire_hook_context_reservation()

    def close_for_canonical_replan(self) -> None:
        # HOOK_CONTEXT is one-shot advisory input.  Once a planning cut
        # freezes it, every abandon/conflict/replan retires that exact
        # reservation; it is never put back or replayed into another cut.
        self.close()

    def seal_installed_open(
        self,
        *,
        prepared_wire: PreparedExecutableProviderWireInput,
        installed_open: "InstalledProviderOpen",
    ) -> None:
        selected = prepared_wire.selected_candidate
        if (
            prepared_wire.owner_dispatch is not self
            or selected.append_result is None
            or selected.sources is None
            or selected.memory_context is None
            or not _provider_wire_candidate_matches_dispatch(selected, self)
        ):
            raise ValueError("installed selection belongs to another dispatch")
        with self._authority_lock:
            if self._installed_sealed:
                raise RuntimeError("provider dispatch selection is already sealed")
            if not self._install_started or self._execution_authority is None:
                raise RuntimeError("provider dispatch lacks installed authority")
            self.installed_provider_open = installed_open
            self._installed_sealed = True


@dataclass(slots=True)
class PreparedProviderHeadroomAdmission:
    """One safe-point handle and its metadata-only exact-cut quote."""

    _handle: PreparedProviderInputHandle | None = dataclass_field(repr=False)
    preflight: FrozenCompactionHeadroomPreflight
    prepared_target: PreparedKernelModelTarget = dataclass_field(repr=False)
    _lock: Lock = dataclass_field(default_factory=Lock, init=False, repr=False)

    @property
    def cut(self) -> PreparedProviderInputCut:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError("headroom admission no longer owns its handle")
        return handle.cut

    def take_handle(self) -> PreparedProviderInputHandle:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle is None:
            raise RuntimeError("headroom admission handle is already consumed")
        return handle

    def close(self) -> None:
        self.take_handle().close()


@dataclass(slots=True)
class PreparedCompactionSourceDispatch:
    """Non-executable normal semantic projection for compaction planning."""

    _handle: PreparedProviderInputHandle | None = dataclass_field(repr=False)
    canonical_read: FrozenCanonicalProviderDispatchRead
    canonical_facts: FrozenCanonicalCompileSnapshot
    planning: FrozenProviderInputAppendPlanningInput
    prepared_call: PreparedKernelModelCall | PreparedKernelSemanticModelCall
    capability_dispatch_cut: FrozenCapabilityDispatchCut
    tool_exposure_plan: FrozenToolCapabilityExposurePlan
    sources: CollectedContextSources
    projection: FrozenProviderInputAppendSemanticProjection = dataclass_field(
        repr=False
    )
    memory_context: FrozenModelCallMemoryContext
    wire_candidate: PreparedProviderWireCandidate = dataclass_field(repr=False)
    _wire_measurement: ProviderWireMeasurement | None = dataclass_field(
        repr=False, compare=False
    )
    _lock: Lock = dataclass_field(default_factory=Lock, init=False, repr=False)

    @property
    def cut(self) -> PreparedProviderInputCut:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError("compaction source no longer owns its handle")
        return handle.cut

    @property
    def wire_quote(self) -> FrozenProviderWireInputQuote:
        with self._lock:
            measurement = self._wire_measurement
        if measurement is None:
            raise RuntimeError("compaction source measurement is already consumed")
        return measurement.quote

    def take_below_trigger_ownership(
        self,
    ) -> tuple[PreparedProviderInputHandle, ProviderWireMeasurement]:
        with self._lock:
            handle = self._handle
            measurement = self._wire_measurement
            self._handle = None
            self._wire_measurement = None
        if handle is None or measurement is None:
            raise RuntimeError("compaction source ownership is already consumed")
        return handle, measurement

    def discard_wire_materialization_to_quote(
        self,
    ) -> FrozenProviderWireInputQuote:
        with self._lock:
            measurement = self._wire_measurement
            self._wire_measurement = None
        if measurement is None:
            raise RuntimeError("compaction source measurement is already consumed")
        return measurement.discard_materialization_to_quote()

    def take_handle(self) -> PreparedProviderInputHandle:
        with self._lock:
            handle = self._handle
            measurement = self._wire_measurement
            if measurement is not None:
                raise RuntimeError(
                    "compaction source measurement must settle before handle transfer"
                )
            self._handle = None
        if handle is None:
            raise RuntimeError("compaction source handle is already consumed")
        return handle

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            measurement = self._wire_measurement
            self._handle = None
            self._wire_measurement = None
        if handle is None:
            raise RuntimeError("compaction source handle is already consumed")
        try:
            if measurement is not None:
                measurement.discard_materialization_to_quote()
        finally:
            handle.close()


class PreparedHookContextSibling:
    """Authority-free optional semantic sibling and its one-shot reservation."""

    __slots__ = ("candidate", "_reservation", "_lock")

    def __init__(
        self,
        candidate: PreparedProviderWireCandidate,
        reservation: HookContextReservation,
    ) -> None:
        self.candidate = candidate
        self._reservation: HookContextReservation | None = reservation
        self._lock = Lock()

    def take_reservation(self) -> HookContextReservation:
        with self._lock:
            reservation = self._reservation
            self._reservation = None
        if reservation is None:
            raise RuntimeError("Hook sibling reservation is already consumed")
        return reservation

    @property
    def owns_reservation(self) -> bool:
        with self._lock:
            return self._reservation is not None

    def retire(self) -> None:
        self.take_reservation().retire()


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

    async def _drain_root_completion_suffix(
        self,
        handle: PreparedProviderInputHandle,
        canonical_read: FrozenCanonicalProviderDispatchRead,
        *,
        deadline: float,
    ) -> tuple[
        PreparedProviderInputHandle,
        FrozenCanonicalProviderDispatchRead,
        bool,
    ]:
        """Append the current FIFO completion cut at one ordinary ROOT safe point."""

        runtime = self._subagent_runtime
        identity = canonical_read.compile_snapshot.canonical_input.identity
        if (
            runtime is None
            or identity.conversation_scope_kind is not ModelInputScopeKind.ROOT
        ):
            return handle, canonical_read, False
        # Human steer is the first lane of the single ROOT pending-input
        # coordinator.  The read is only an optimization: the canonical writer
        # repeats this check in its exact-cut transaction, so a concurrent steer
        # still wins and makes the completion plan stale.
        pending_steer = await self._io.run(
            self._repository.read_pending_prompt_steer_facts,
            session_id=identity.session_id,
            target_turn_id=identity.turn_id,
            maximum_items=1,
            deadline_monotonic=deadline,
        )
        if pending_steer:
            return handle, canonical_read, False
        changed = False
        pending_cut = await runtime.snapshot_pending_root_completions(identity.turn_id)
        for task_id in pending_cut[:_ROOT_COMPLETION_SUFFIX_BATCH_ITEMS]:
            outcome = await self._io.run(
                self._safe_point.accept_queued_subagent_completion,
                handle,
                task_id=task_id,
                actor_id=self._writer_lease.guard.writer_owner_id,
                deadline_monotonic=deadline,
            )
            if outcome.disposition is SubagentCompletionDisposition.TARGET_STALE:
                # A pending steer or changed exact cut keeps the FIFO hint for
                # the next rebuilt plan; never leapfrog it with later items.
                break
            await runtime.retire_root_completion(task_id)
            if outcome.disposition is SubagentCompletionDisposition.CREATED:
                changed = True
                handle = await self._io.run(
                    self._safe_point.rotate_provider_input,
                    handle,
                    turn_id=identity.turn_id,
                    deadline_monotonic=deadline,
                )
                canonical_read = await self.read_dispatch_read(
                    handle.cut, deadline=deadline
                )
        return handle, canonical_read, changed

    async def prepare_headroom_admission(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> PreparedProviderHeadroomAdmission:
        """Freeze one cut and quote compaction headroom before body hydration."""

        prepare_surface = getattr(self._tools, "prepare_tool_surface_safe_point", None)
        if prepare_surface is not None:
            prepare_surface()
        handle = await self._io.run(
            self._safe_point.freeze_provider_input,
            turn_id=turn_id,
            deadline_monotonic=deadline,
        )
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
            preflight = await self.read_compaction_headroom_preflight(
                handle.cut, deadline=deadline
            )
            return PreparedProviderHeadroomAdmission(handle, preflight, prepared_target)
        except BaseException:
            handle.close()
            raise

    async def prepare_compaction_source(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        deadline: float,
        allow_terminal_compaction: bool = False,
        existing_handle: PreparedProviderInputHandle | None = None,
        headroom_preflight_override: FrozenCompactionHeadroomPreflight | None = None,
        prepared_target_override: PreparedKernelModelTarget | None = None,
    ) -> PreparedCompactionSourceDispatch:
        result = await self.prepare(
            turn_id=turn_id,
            model_call_index=model_call_index,
            inherited_memory_use_policy=inherited_memory_use_policy,
            deadline=deadline,
            allow_steers=False,
            allow_terminal_compaction=allow_terminal_compaction,
            existing_handle=existing_handle,
            headroom_preflight_override=headroom_preflight_override,
            prepared_target_override=prepared_target_override,
            semantic_only=True,
            _compaction_source_projection=True,
        )
        assert isinstance(result, PreparedCompactionSourceDispatch)
        return result

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
        headroom_preflight_override: FrozenCompactionHeadroomPreflight | None = None,
        prepared_target_override: PreparedKernelModelTarget | None = None,
        compaction_source_replacements: tuple[
            ContextSourceCandidate | ContextSourceAbsentFact, ...
        ] = (),
        compaction_retained_skill_read: FrozenCompactionCanonicalRead | None = None,
        include_hook_context: bool = True,
        semantic_only: bool = False,
        _compaction_source_projection: bool = False,
    ) -> PreparedProviderDispatch | PreparedCompactionSourceDispatch:
        """Freeze, quote and (when present) consume one exact steer suffix."""

        if _compaction_source_projection and (allow_steers or not semantic_only):
            raise ValueError(
                "compaction source projection must be semantic and steer-free"
            )
        prepare_surface = getattr(self._tools, "prepare_tool_surface_safe_point", None)
        if prepare_surface is not None:
            prepare_surface()
        freeze_operation = (
            self._safe_point.freeze_compaction_input
            if allow_terminal_compaction
            else self._safe_point.freeze_provider_input
        )
        handle = existing_handle
        if headroom_preflight_override is not None and handle is None:
            raise ValueError("headroom override requires its prepared handle")
        if handle is None:
            handle = await self._io.run(
                freeze_operation,
                turn_id=turn_id,
                **({"allow_terminal": True} if allow_terminal_compaction else {}),
                deadline_monotonic=deadline,
            )
        borrow: ProcessLocalToolSurfaceBorrow | None = None
        hook_context_reservation: HookContextReservation | None = None
        try:
            await self._resolved_workspace_id(deadline=deadline)
            headroom_preflight = headroom_preflight_override
            if headroom_preflight is not None:
                cut = handle.cut
                if (
                    headroom_preflight.session_id != cut.session_id
                    or headroom_preflight.turn_id != cut.turn_id
                    or headroom_preflight.context_binding_revision_id
                    != cut.context_binding_revision_id
                    or headroom_preflight.provider_input_through_sequence
                    != cut.provider_input_through_sequence
                ):
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                    )
            elif (
                self._compaction_owner is not None
                and self._compaction_owner.policy.automatic_enabled
                and allow_steers
                and canonical_read_override is None
            ):
                headroom_preflight = await self.read_compaction_headroom_preflight(
                    handle.cut, deadline=deadline
                )
            observed_read = await self.read_dispatch_read(handle.cut, deadline=deadline)
            if (
                allow_steers
                and not allow_terminal_compaction
                and canonical_read_override is None
                and not _compaction_source_projection
            ):
                (
                    handle,
                    observed_read,
                    completion_changed,
                ) = await self._drain_root_completion_suffix(
                    handle, observed_read, deadline=deadline
                )
                if completion_changed:
                    # A preflight frozen for the former cut is no longer a
                    # legal quote. Recompute it from the rotated canonical cut.
                    headroom_preflight = None
                    if (
                        self._compaction_owner is not None
                        and self._compaction_owner.policy.automatic_enabled
                    ):
                        headroom_preflight = (
                            await self.read_compaction_headroom_preflight(
                                handle.cut, deadline=deadline
                            )
                        )
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
                prepared_target = prepared_target_override
                if prepared_target is None:
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
                elif (
                    prepared_target.session_id != self._writer_lease.guard.session_id
                    or prepared_target.turn_id != turn_id
                    or prepared_target.model_call_index != model_call_index
                    or prepared_target.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
                ):
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.MODEL_TARGET_PREPARATION_FAILED
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
                    inspection=skill_owner.inspection,
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
                    hook_context_estimator=(
                        None
                        if semantic_only or not include_hook_context
                        else prepared_call.compile_binding.estimator
                    ),
                    deadline_monotonic=deadline,
                )
                hook_context_reservation = frozen_sources.hook_context_reservation
                if hook_context_reservation is not None:
                    # The reservation is physical one-shot authority owned by
                    # the dispatch, never part of frozen semantic source state.
                    frozen_sources = replace(
                        frozen_sources,
                        hook_context_reservation=None,
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
            selected_write_hint = False
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
                    write_hint = False
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
                        write_hint = (
                            trigger_policies[-1].write_hint
                            and memory_use_policy.allows_writes
                            and prospective.run_permission_snapshot.effective_mode
                            is not PermissionMode.READ_ONLY
                            and any(
                                item.name == "remember"
                                for item in model_surface.tool_specs
                            )
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
                    selected_write_hint = write_hint
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
                (
                    handle,
                    actual_read,
                    _completion_changed,
                ) = await self._drain_root_completion_suffix(
                    handle, actual_read, deadline=deadline
                )
                actual = actual_read.compile_snapshot
                final_sources = await self._memory_support.apply_sources(
                    selected_sources,
                    activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
                    activation_text=selected_activation_text,
                    include_recall=True,
                    frozen_preference=selected_preference,
                    trigger_disposition=selected_trigger_disposition,
                    write_hint=selected_write_hint,
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
                    _execution_authority=ProviderDispatchExecutionAuthority(
                        handle, borrow
                    ),
                    canonical_read=actual_read,
                    canonical_facts=actual,
                    planning=selected_plan.predecessor,
                    prepared_call=prepared_call,
                    capability_dispatch_cut=capability_dispatch_cut,
                    tool_exposure_plan=tool_exposure_plan,
                    sources=final_sources,
                    append_result=final_append,
                    memory_context=final_memory[0],
                    accepted_steers=batch,
                    retained_skill_selection=retained_skill_selection,
                    _hook_context_reservation=hook_context_reservation,
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
                # Every compaction candidate crosses the same synthetic cold
                # base assembly path.  Its historical human anchor is not a new
                # Skill or memory activation.  An active candidate may later be
                # installed as a successor; an idle candidate is closed after
                # the same proof without opening a provider call.
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
            write_hint = False
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
                write_hint = (
                    trigger_policy.write_hint
                    and memory_use_policy.allows_writes
                    and base_facts.run_permission_snapshot.effective_mode
                    is not PermissionMode.READ_ONLY
                    and any(item.name == "remember" for item in model_surface.tool_specs)
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
            if _compaction_source_projection:
                projection_sources = base_sources
                if preference_source is not None and trigger_disposition is not None:
                    projection_sources = await self._memory_support.apply_sources(
                        sources,
                        activation_subject=activation_subject,
                        activation_text=activation_text,
                        include_recall=True,
                        frozen_preference=preference_source,
                        trigger_disposition=trigger_disposition,
                        write_hint=write_hint,
                    )
                projection_memory = self._memory_support.freeze_call_context(
                    scope=scope,
                    planning=planning,
                    canonical_facts=base_facts,
                    sources=projection_sources,
                    memory_use_policy=memory_use_policy,
                )
                projection_request = replace(
                    compile_request,
                    sources=projection_sources,
                    memory_citation_handles=projection_memory[1],
                )
                projection = await self._io.run(
                    project_structured_append,
                    self._compiler,
                    projection_request,
                    planning=planning,
                    compatibility=provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=base_facts,
                        sources=projection_sources,
                    ),
                    deadline_monotonic=deadline,
                )
                if borrow is not None:
                    raise RuntimeError(
                        "semantic compaction projection acquired a physical borrow"
                    )
                wire_candidate = PreparedProviderWireCandidate(
                    canonical_read=base_read,
                    semantic_input=projection.projected_input,
                    prepared_call=prepared_call,
                    native_projection_set=prepared_call.native_projection_set,
                )
                wire_measurement = await self._freeze_candidate_wire_measurement(
                    wire_candidate,
                    deadline=deadline,
                )
                try:
                    return PreparedCompactionSourceDispatch(
                        _handle=handle,
                        canonical_read=base_read,
                        canonical_facts=base_facts,
                        planning=planning,
                        prepared_call=prepared_call,
                        capability_dispatch_cut=capability_dispatch_cut,
                        tool_exposure_plan=tool_exposure_plan,
                        sources=projection_sources,
                        projection=projection,
                        memory_context=projection_memory[0],
                        wire_candidate=wire_candidate,
                        _wire_measurement=wire_measurement,
                    )
                except BaseException:
                    wire_measurement.discard_materialization_to_quote()
                    raise
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
                    write_hint=write_hint,
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
                _execution_authority=ProviderDispatchExecutionAuthority(handle, borrow),
                canonical_read=base_read,
                canonical_facts=base_facts,
                planning=planning,
                prepared_call=prepared_call,
                capability_dispatch_cut=capability_dispatch_cut,
                tool_exposure_plan=tool_exposure_plan,
                sources=final_sources,
                append_result=append,
                memory_context=memory_snapshot[0],
                compaction_headroom_preflight=headroom_preflight,
                cold_semantic=cold_semantic,
                retained_skill_selection=retained_skill_selection,
                _hook_context_reservation=hook_context_reservation,
            )
        except BaseException:
            handle.close()
            if borrow is not None:
                borrow.close()
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            raise

    async def prepare_hook_context_sibling(
        self,
        base: PreparedProviderDispatch,
        *,
        model_call_index: int,
        deadline: float,
    ) -> PreparedHookContextSibling | None:
        """Compile the optional Hook cold sibling from the base's exact facts.

        The base remains the final fallback and owns the sole physical borrow.
        This method performs no continuity registration, install, ToolResult
        delivery, provider preflight, or transport open.
        """

        semantic = base.cold_semantic
        if semantic is None or base.has_hook_context_reservation:
            raise ValueError("Hook sibling requires one no-Hook cold base")
        identity = base.canonical_facts.canonical_input.identity
        replacement, reservation = (
            self._context_source_collector.freeze_hook_context_source(
                scope_kind=identity.conversation_scope_kind.value,
                child_task_id=identity.scope_subagent_task_id,
                estimator=base.prepared_call.compile_binding.estimator,
            )
        )
        if reservation is None:
            return None
        try:
            hook_sources = replace_hook_context_source(base.sources, replacement)
            hook_non_trigger = replace_frozen_hook_context_source(
                semantic.non_trigger_sources,
                replacement,
                reservation=None,
            )
            anchor = semantic.planning.dispatch_anchor
            compile_request = StructuredModelInputCompileRequest(
                context_id=f"model-context-hook-sibling:{uuid4().hex}",
                model_call_index=model_call_index,
                canonical_input=base.canonical_facts.canonical_input,
                canonical_facts=base.canonical_facts,
                compile_binding=base.prepared_call.compile_binding,
                sources=hook_sources,
                dispatch_anchor_entry_id=(
                    anchor.source_entry_id
                    if isinstance(anchor, NewTriggerAnchor)
                    else None
                ),
                memory_citation_handles=tuple(
                    (item.reference.tool_result_id, item.handle)
                    for item in base.memory_context.citation_handles
                ),
            )
            compatibility = provider_input_compatibility(
                prepared_call=base.prepared_call,
                canonical_facts=base.canonical_facts,
                sources=hook_sources,
            )
            if compatibility != semantic.compatibility:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            hook_semantic = await self._io.run(
                self._cold_epoch_assembler.prepare_semantic,
                seed=semantic.seed,
                compile_request=compile_request,
                planning=semantic.planning,
                compatibility=compatibility,
                prepared_call=semantic.prepared_call,
                capability_dispatch_cut=semantic.capability_dispatch_cut,
                tool_view=semantic.tool_view,
                skill_view=semantic.skill_view,
                tool_exposure_plan=semantic.tool_exposure_plan,
                non_trigger_sources=hook_non_trigger,
                replay_target=semantic.replay_target,
                deadline_monotonic=deadline,
            )
            compiled = hook_semantic.compiled_result.compiled_input
            base_compiled = base.append_result.compiled_input
            if compiled.system_prompt != base_compiled.system_prompt or (
                compiled.tools != base_compiled.tools
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            hook_decision = next(
                (
                    item
                    for item in compiled.source_decisions
                    if item.source_kind is ContextSourceKind.HOOK_CONTEXT
                ),
                None,
            )
            if hook_decision is None or not hook_decision.included:
                reservation.retire()
                return None
            candidate = PreparedProviderWireCandidate(
                canonical_read=base.canonical_read,
                semantic_input=hook_semantic.compiled_result.compiled_input,
                prepared_call=base.prepared_call,
                native_projection_set=base.prepared_call.native_projection_set,
                planning=base.planning,
                append_result=hook_semantic.compiled_result,
                cold_semantic=hook_semantic,
                sources=hook_sources,
                tool_exposure_plan=base.tool_exposure_plan,
                memory_context=base.memory_context,
            )
            return PreparedHookContextSibling(candidate, reservation)
        except BaseException:
            reservation.retire()
            raise

    @staticmethod
    def wire_candidate_for_dispatch(
        dispatch: PreparedProviderDispatch,
    ) -> PreparedProviderWireCandidate:
        return PreparedProviderWireCandidate(
            canonical_read=dispatch.canonical_read,
            semantic_input=dispatch.append_result.compiled_input,
            prepared_call=dispatch.prepared_call,
            native_projection_set=dispatch.prepared_call.native_projection_set,
            planning=dispatch.planning,
            append_result=dispatch.append_result,
            cold_semantic=dispatch.cold_semantic,
            sources=dispatch.sources,
            tool_exposure_plan=dispatch.tool_exposure_plan,
            memory_context=dispatch.memory_context,
        )

    def bind_selected_provider_dispatch(
        self,
        *,
        base: PreparedProviderDispatch,
        sibling: PreparedHookContextSibling,
    ) -> PreparedProviderDispatch:
        """Atomically move the base authority into one selected Hook sibling."""

        selected = sibling.candidate
        if (
            selected.planning is None
            or selected.append_result is None
            or selected.sources is None
            or selected.tool_exposure_plan is None
            or selected.memory_context is None
            or selected.cold_semantic is None
            or selected.canonical_read != base.canonical_read
            or selected.prepared_call != base.prepared_call
            or selected.native_projection_set
            != base.prepared_call.native_projection_set
            or selected.planning != base.planning
            or selected.tool_exposure_plan != base.tool_exposure_plan
            or base.has_hook_context_reservation
        ):
            sibling.retire()
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        authority = base.take_execution_authority()
        reservation: HookContextReservation | None = None
        final: PreparedProviderDispatch | None = None
        try:
            reservation = sibling.take_reservation()
            final = PreparedProviderDispatch(
                _execution_authority=authority,
                canonical_read=selected.canonical_read,
                canonical_facts=selected.canonical_read.compile_snapshot,
                planning=selected.planning,
                prepared_call=selected.prepared_call,
                capability_dispatch_cut=base.capability_dispatch_cut,
                tool_exposure_plan=selected.tool_exposure_plan,
                sources=selected.sources,
                append_result=selected.append_result,
                memory_context=selected.memory_context,
                compaction_headroom_preflight=base.compaction_headroom_preflight,
                accepted_steers=base.accepted_steers,
                cold_semantic=selected.cold_semantic,
                retained_skill_selection=base.retained_skill_selection,
                _hook_context_reservation=reservation,
            )
            return final
        except BaseException:
            if final is not None:
                final.close()
            else:
                try:
                    authority.close()
                finally:
                    if reservation is not None:
                        reservation.retire()
            raise

    async def _freeze_candidate_wire_measurement(
        self,
        candidate: ProviderWireMeasurementCandidate,
        *,
        deadline: float,
    ) -> ProviderWireMeasurement:
        replay_target = self._model.replay_target_for_resolved_call(candidate.call)
        try:
            replay_hydration = await self._io.run(
                self._input_reader.hydrate_selected_provider_replays,
                dispatch_read=candidate.canonical_read,
                compiled_input=candidate.semantic_input,
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
        _require_dispatch_planning_deadline(deadline)
        return self._model.freeze_wire_measurement(
            call=candidate.call,
            compile_binding=candidate.compile_binding,
            native_projection_set=candidate.native_projection_set,
            semantic_input=candidate.semantic_input,
            replay_hydration=replay_hydration,
            tool_choice=candidate.tool_choice,
        )

    async def measure_prepared_wire_candidate(
        self,
        candidate: ProviderWireMeasurementCandidate,
        *,
        deadline: float,
        reusable_observation: HandleFreeProviderWireObservation | None = None,
    ) -> PreparedWireMeasurementDecision:
        measurement = None
        if reusable_observation is not None:
            measurement = reusable_observation.take_for(candidate)
        if measurement is None:
            measurement = await self._freeze_candidate_wire_measurement(
                candidate,
                deadline=deadline,
            )
        try:
            _require_dispatch_planning_deadline(deadline)
        except BaseException:
            measurement.discard_materialization_to_quote()
            raise
        quote = measurement.quote
        if (
            quote.final_wire_estimated_input_tokens
            <= quote.effective_input_budget_tokens
            and quote.final_wire_utf8_bytes <= MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
        ):
            plan = measurement.prepare_executable_plan(
                semantic_input=candidate.semantic_input
            )
        else:
            quote = measurement.discard_materialization_to_quote()
            plan = None
        decision = PreparedWireMeasurementDecision(candidate, quote, plan)
        _require_dispatch_planning_deadline(deadline)
        return decision

    def bind_prepared_executable_wire_input(
        self,
        *,
        owner_dispatch: PreparedProviderDispatch,
        decision: PreparedWireMeasurementDecision,
        deadline: float,
    ) -> PreparedExecutableProviderWireInput:
        _require_dispatch_planning_deadline(deadline)
        if decision.wire_input_plan is None:
            kind = (
                ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
                if decision.quote.final_wire_estimated_input_tokens
                > decision.quote.effective_input_budget_tokens
                else ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
            )
            raise StructuredModelInputCompileError(kind)
        selected = decision.candidate
        if (
            not isinstance(selected, PreparedProviderWireCandidate)
            or selected.planning is None
            or selected.append_result is None
            or selected.sources is None
            or selected.tool_exposure_plan is None
            or selected.memory_context is None
            or not _provider_wire_candidate_matches_dispatch(selected, owner_dispatch)
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        wire_input_plan = decision.wire_input_plan
        if selected.cold_semantic is not None:
            assembly = self._cold_epoch_assembler.bind_prepared_wire(
                selected.cold_semantic,
                wire_input_plan=wire_input_plan,
                deadline_monotonic=deadline,
            )
            if assembly.compiled_input != selected.append_result.compiled_input:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            inputs = assembly.continuity_candidate_inputs
            append_candidate = prepared_append_candidate(
                planning=inputs.planning,
                compatibility=inputs.compatibility,
                compiled_result=inputs.compiled_result,
                wire_input_plan=inputs.wire_input_plan,
                tool_exposure_plan=inputs.tool_exposure_plan,
            )
        else:
            compatibility = provider_input_compatibility(
                prepared_call=selected.prepared_call,
                canonical_facts=owner_dispatch.canonical_facts,
                sources=selected.sources,
            )
            append_candidate = prepared_append_candidate(
                planning=selected.planning,
                compatibility=compatibility,
                compiled_result=selected.append_result,
                wire_input_plan=wire_input_plan,
                tool_exposure_plan=selected.tool_exposure_plan,
            )
        prepared = PreparedExecutableProviderWireInput(
            owner_dispatch=owner_dispatch,
            quote=decision.quote,
            wire_input_plan=wire_input_plan,
            selected_candidate=selected,
            append_candidate=append_candidate,
        )
        _require_dispatch_planning_deadline(deadline)
        return prepared

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
        prepared_wire: PreparedExecutableProviderWireInput,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> InstalledProviderOpen:
        """Preflight and CAS one exact dispatch without opening transport."""

        prepared_call = dispatch.prepared_call
        if not isinstance(prepared_call, PreparedKernelModelCall):
            raise RuntimeError("provider install lacks an execution-backed surface")
        if prepared_wire.owner_dispatch is not dispatch:
            raise ValueError("prepared wire input belongs to another dispatch owner")
        selected = prepared_wire.selected_candidate
        canonical_facts = dispatch.canonical_facts
        if (
            selected.append_result is None
            or selected.memory_context is None
            or not _provider_wire_candidate_matches_dispatch(selected, dispatch)
        ):
            raise ValueError("prepared wire selection lost its dispatch join")
        _require_dispatch_planning_deadline(deadline)
        handle, borrow = dispatch.claim_install_authority()
        compiled_input = selected.append_result.compiled_input
        wire_input_plan = prepared_wire.wire_input_plan
        append_candidate = prepared_wire.append_candidate
        request: KernelModelExecutionRequest | None = None
        execution: PreparedKernelModelExecution | None = None
        registered = False
        installed = False
        try:
            self._continuity.register(append_candidate)
            registered = True
            request = KernelModelExecutionRequest(
                session_id=self._writer_lease.guard.session_id,
                turn_id=turn_id,
                model_call_index=model_call_index,
                prepared_call=prepared_call,
                compiled_input=compiled_input,
                wire_input_plan=wire_input_plan,
                cut=handle.cut,
                surface_borrow=borrow,
                memory_context=selected.memory_context,
            )
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
            _require_dispatch_planning_deadline(deadline)
            handle.begin_model_operation()
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
            installed_open = InstalledProviderOpen(
                request=request,
                execution=execution,
                permit=permit,
                append_candidate=append_candidate,
                subagent_parent_context_subject=(
                    _freeze_subagent_parent_context_call_subject(
                        dispatch=dispatch,
                        compiled_input=compiled_input,
                        permit=permit,
                    )
                    if canonical_facts.canonical_input.identity.conversation_scope_kind
                    is ModelInputScopeKind.ROOT
                    else None
                ),
            )
            dispatch.seal_installed_open(
                prepared_wire=prepared_wire,
                installed_open=installed_open,
            )
            return installed_open
        except BaseException:
            if not installed and registered:
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


def project_structured_append(
    compiler: StructuredModelInputCompiler,
    request: StructuredModelInputCompileRequest,
    *,
    planning: FrozenProviderInputAppendPlanningInput,
    compatibility: ProviderInputEpochCompatibility,
    deadline_monotonic: float,
) -> FrozenProviderInputAppendSemanticProjection:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("structured model input deadline expired")
    return compiler.project_append(
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


def _same_provider_wire_measurement_candidate(
    left: ProviderWireMeasurementCandidate,
    right: ProviderWireMeasurementCandidate,
) -> bool:
    return (
        left.canonical_read == right.canonical_read
        and left.semantic_input.canonical_input_identity
        == right.semantic_input.canonical_input_identity
        and left.semantic_input.system_prompt == right.semantic_input.system_prompt
        and left.semantic_input.messages == right.semantic_input.messages
        and left.semantic_input.message_placements
        == right.semantic_input.message_placements
        and left.semantic_input.tools == right.semantic_input.tools
        and left.semantic_input.final_estimate == right.semantic_input.final_estimate
        and left.semantic_input.compile_binding_fingerprint
        == right.semantic_input.compile_binding_fingerprint
        and left.call == right.call
        and left.compile_binding == right.compile_binding
        and left.compile_binding.estimator is right.compile_binding.estimator
        and left.native_projection_set == right.native_projection_set
        and left.tool_choice == right.tool_choice
    )


def _provider_wire_candidate_matches_dispatch(
    candidate: PreparedProviderWireCandidate,
    dispatch: PreparedProviderDispatch,
) -> bool:
    return (
        candidate.canonical_read == dispatch.canonical_read
        and candidate.semantic_input == dispatch.append_result.compiled_input
        and candidate.prepared_call == dispatch.prepared_call
        and candidate.native_projection_set
        == dispatch.prepared_call.native_projection_set
        and candidate.planning == dispatch.planning
        and candidate.append_result == dispatch.append_result
        and candidate.cold_semantic == dispatch.cold_semantic
        and candidate.sources == dispatch.sources
        and candidate.tool_exposure_plan == dispatch.tool_exposure_plan
        and candidate.memory_context == dispatch.memory_context
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
    compiled_input: FrozenCompiledModelInput,
    permit: ProcessLocalProviderInputInstallPermit,
) -> FrozenSubagentParentContextCallSubject:
    """Derive the bounded public ROOT tail from the exact installed call.

    Placements are the join between compiled messages and canonical entry
    identity.  Tool roles/groups and all placement-less runtime sources are
    excluded; assistant messages contribute public text only.
    """

    compiled = compiled_input
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
    cut = dispatch.cut
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
    "HandleFreeProviderWireObservation",
    "InstalledProviderOpen",
    "KernelModelPort",
    "PreparedCompactionSourceDispatch",
    "PreparedExecutableProviderWireInput",
    "PreparedProviderDispatch",
    "PreparedProviderHeadroomAdmission",
    "PreparedProviderWireCandidate",
    "PreparedWireMeasurementDecision",
    "ProviderWireMeasurementCandidate",
    "ProviderDispatchExecutionAuthority",
    "ProviderDispatchCoordinator",
    "canonical_frontier",
    "compile_structured_append",
    "prepared_append_candidate",
    "provider_input_compatibility",
    "provider_replay_target",
]
