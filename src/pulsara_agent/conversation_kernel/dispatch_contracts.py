"""Prepared provider dispatch values and their one-shot ownership contracts."""

from __future__ import annotations

from pulsara_agent.conversation_kernel.agent_context import (
    FrozenAgentInvocationSnapshot,
)
from dataclasses import dataclass, field as dataclass_field, replace
from threading import Lock
from typing import Callable, Protocol
from pulsara_agent.conversation_kernel.context_sources import (
    FrozenNonTriggerContextSources,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionHeadroomPreflight,
)
from pulsara_agent.conversation_kernel.compaction.retained_skill import (
    FrozenRetainedSkillContextSelection,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    AdoptedCompactionContinuationSeed,
    CanonicalColdContinuationSeed,
    CompactionDryProjectionSeed,
    PreparedColdEpochSemanticAssembly,
)
from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
    KernelModelTargetPreparationRequest,
    PreparedKernelModelCall,
    PreparedKernelModelExecution,
    PreparedKernelSemanticModelCall,
    PreparedKernelModelTarget,
    ProviderWireMeasurement,
)
from pulsara_agent.capability.contracts import (
    FrozenCapabilityDispatchCut,
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilitySet,
    FrozenSkillCapabilityDispatchView,
    FrozenToolCapabilityDispatchView,
    FrozenToolCapabilityExposurePlan,
)
from pulsara_agent.llm.request import (
    FrozenProviderWireInputQuote,
    FrozenProviderWireInputPlan,
    ProviderInputUsageAnchor,
)
from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelCallTarget,
    FrozenEpochModelTargetBundle,
)
from pulsara_agent.llm.provider_replay import ProviderReplayTargetCompatibilityFact
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    SealedProviderInputPreparationBasis,
)
from pulsara_agent.conversation_kernel.steer import AcceptedSteerDispatchBatch
from pulsara_agent.hooks.context import HookContextReservation
from pulsara_agent.model_input.contracts import (
    CollectedContextSources,
    FrozenCanonicalCompileSnapshot,
    FrozenModelToolSurface,
    PreparedProviderInputCut,
    FrozenCompiledModelInput,
    FrozenModelInputSemanticProjection,
    ProviderWireSemanticInput,
    ModelInputCompileFailureKind,
    ModelInputCompileBinding,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
)
from pulsara_agent.model_input.continuity import (
    FrozenDirectSwitchAdmission,
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputAppendSemanticProjection,
    FrozenProviderInputEpochView,
    PreparedProviderInputAppendCandidate,
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    FrozenSelectedDurableProviderReplayHydration,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)

from pulsara_agent.conversation_kernel.dispatch_resources import (
    CompactionDryResourceAuthority,
    ProviderDispatchExecutionAuthority,
)


class KernelModelPort(Protocol):
    @property
    def model_runtime(self) -> ModelRuntime: ...

    @property
    def transport_timeout_policy(self) -> OpenAITransportTimeoutPolicy: ...

    def prepare_target(
        self, request: KernelModelTargetPreparationRequest
    ) -> PreparedKernelModelTarget: ...

    def prepare_target_from_snapshot(
        self,
        request: KernelModelTargetPreparationRequest,
        *,
        resolution_snapshot: FrozenModelResolutionSnapshot,
    ) -> PreparedKernelModelTarget: ...

    def prepare_resolved_target(
        self,
        request: KernelModelTargetPreparationRequest,
        *,
        target: ResolvedModelTarget,
        binding: ModelCallBinding,
    ) -> PreparedKernelModelTarget: ...

    def prepare_frozen_epoch_target(
        self,
        request: KernelModelTargetPreparationRequest,
        *,
        bundle: FrozenEpochModelTargetBundle,
    ) -> PreparedKernelModelTarget: ...

    def freeze_native_tool_eligibility(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
        tool_facts: tuple,
        retained_direct_inputs: tuple = (),
        deadline_monotonic: float | None = None,
    ) -> FrozenNativeToolWireEligibilitySet: ...

    def materialize_native_tool_projection_set(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
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
        usage_anchor: ProviderInputUsageAnchor | None = None,
        validate_content_capability: bool = True,
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
    compile_request: StructuredModelInputCompileRequest | None = dataclass_field(
        default=None, repr=False
    )
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
        if self.compile_request is not None and (
            self.compile_request.canonical_facts != self.canonical_read.compile_snapshot
            or self.compile_request.canonical_input
            != self.canonical_read.compile_snapshot.canonical_input
            or self.compile_request.compile_binding
            is not self.prepared_call.compile_binding
            or self.compile_request.sources != self.sources
            or self.compile_request.context_id != self.semantic_input.context_id
            or self.compile_request.sources.collection_fingerprint
            != self.semantic_input.source_collection_fingerprint
        ):
            raise ValueError("wire candidate changed its frozen compilation basis")
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
        cold = self.cold_semantic
        if cold is not None and (
            cold.compile_request != self.compile_request
            or cold.planning != self.planning
            or cold.compiled_result != self.append_result
            or cold.prepared_call is not self.prepared_call
            or cold.seed.dispatch_read != self.canonical_read
        ):
            raise ValueError("wire candidate changed its frozen cold assembly")

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


@dataclass(slots=True)
class PreparedProviderDispatch:
    _execution_authority: ProviderDispatchExecutionAuthority | None = dataclass_field(
        repr=False, compare=False
    )
    _continuity_owner: HostProviderInputContinuityOwner = dataclass_field(
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
    compile_request: StructuredModelInputCompileRequest | None = dataclass_field(
        default=None, repr=False
    )
    direct_switch_admission: FrozenDirectSwitchAdmission | None = dataclass_field(
        default=None, repr=False
    )
    compaction_headroom_preflight: FrozenCompactionHeadroomPreflight | None = (
        dataclass_field(default=None, repr=False)
    )
    accepted_steers: AcceptedSteerDispatchBatch | None = None
    non_trigger_sources: FrozenNonTriggerContextSources | None = dataclass_field(
        default=None, repr=False
    )
    base_trigger_pending: bool = False
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
    _wire_bound: bool = dataclass_field(
        default=False, init=False, repr=False, compare=False
    )
    _continuation_abandonment: Callable[[BaseException], None] | None = dataclass_field(
        default=None, init=False, repr=False, compare=False
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
            or (
                self.direct_switch_admission is not None
                and (
                    self.direct_switch_admission.destination
                    != self.prepared_call.epoch_call_target
                    or self.direct_switch_admission.semantic_projection
                    != _semantic_projection_from_compiled(
                        self.append_result.compiled_input
                    )
                )
            )
        ):
            raise ValueError("prepared provider dispatch does not exact-join")

    def select_wire_candidate(self, candidate: PreparedProviderWireCandidate) -> None:
        """Adopt a measured render winner while retaining the same cut authority."""
        if _provider_wire_candidate_matches_dispatch(candidate, self):
            return
        if (
            candidate.canonical_read != self.canonical_read
            or candidate.prepared_call is not self.prepared_call
            or candidate.native_projection_set
            != self.prepared_call.native_projection_set
            or candidate.planning != self.planning
            or candidate.sources != self.sources
            or candidate.compile_request != self.compile_request
            or candidate.tool_exposure_plan != self.tool_exposure_plan
            or candidate.memory_context != self.memory_context
            or candidate.append_result is None
            or self.direct_switch_admission is not None
            or (candidate.cold_semantic is None) != (self.cold_semantic is None)
            or (
                self.cold_semantic is not None
                and replace(self.cold_semantic, compiled_result=candidate.append_result)
                != candidate.cold_semantic
            )
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        with self._authority_lock:
            if (
                self._wire_bound
                or self._install_started
                or self._execution_authority is None
            ):
                raise ConversationKernelConflict(
                    "wire selection followed dispatch installation"
                )
            self.append_result = candidate.append_result
            self.cold_semantic = candidate.cold_semantic

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
    ) -> tuple[
        SealedProviderInputPreparationBasis,
        PreparedProviderInputHandle,
        ProcessLocalToolSurfaceBorrow,
    ]:
        with self._authority_lock:
            authority = self._execution_authority
            if authority is None:
                raise RuntimeError("provider dispatch execution authority is consumed")
            if self._install_started:
                raise RuntimeError("provider dispatch install is already consumed")
            self._install_started = True
        return authority.require_for_install()

    @property
    def preparation_basis(self) -> SealedProviderInputPreparationBasis:
        with self._authority_lock:
            authority = self._execution_authority
        if authority is None:
            raise RuntimeError("provider dispatch execution authority is consumed")
        return authority.basis

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

    def take_hook_context_reservation(self) -> HookContextReservation | None:
        with self._authority_lock:
            reservation = self._hook_context_reservation
            self._hook_context_reservation = None
        return reservation

    def require_unbound(self) -> None:
        with self._authority_lock:
            if (
                self._wire_bound
                or self._install_started
                or self._execution_authority is None
            ):
                raise ConversationKernelConflict(
                    "provider input is already bound or consumed"
                )

    def claim_wire_binding(self) -> None:
        with self._authority_lock:
            if (
                self._wire_bound
                or self._install_started
                or self._execution_authority is None
            ):
                raise ConversationKernelConflict(
                    "provider input is already bound or consumed"
                )
            self._wire_bound = True

    def install_continuation_abandonment(
        self, callback: Callable[[BaseException], None]
    ) -> None:
        with self._authority_lock:
            if self._continuation_abandonment is not None or self._installed_sealed:
                raise RuntimeError("continuation abandonment owner is already bound")
            self._continuation_abandonment = callback

    def take_continuation_abandonment(self) -> Callable[[BaseException], None] | None:
        with self._authority_lock:
            callback = self._continuation_abandonment
            self._continuation_abandonment = None
        return callback

    def close(self, *, error: BaseException | None = None) -> None:
        authority = self.take_execution_authority()
        abandon = self.take_continuation_abandonment()
        try:
            authority.close()
        finally:
            try:
                if not self._installed_sealed:
                    self._continuity_owner.abort_planning(self.planning)
            finally:
                try:
                    self.retire_hook_context_reservation()
                finally:
                    if abandon is not None:
                        abandon(error or RuntimeError("adopted continuation abandoned"))

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
            self._continuation_abandonment = None


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
class PreparedCompactionCandidateFamily:
    """One frozen physical/source basis for ordered recent-window variants."""

    _resource_authority: (
        ProviderDispatchExecutionAuthority | CompactionDryResourceAuthority | None
    ) = dataclass_field(repr=False, compare=False)
    first_candidate_read: FrozenCanonicalProviderDispatchRead = dataclass_field(
        repr=False
    )
    seed: (
        AdoptedCompactionContinuationSeed
        | CanonicalColdContinuationSeed
        | CompactionDryProjectionSeed
    ) = dataclass_field(repr=False)
    model_call_index: int
    planning_basis: FrozenProviderInputAppendPlanningInput = dataclass_field(repr=False)
    prepared_call: PreparedKernelModelCall = dataclass_field(repr=False)
    capability_dispatch_cut: FrozenCapabilityDispatchCut = dataclass_field(repr=False)
    tool_view: FrozenToolCapabilityDispatchView = dataclass_field(repr=False)
    skill_view: FrozenSkillCapabilityDispatchView = dataclass_field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = dataclass_field(repr=False)
    sources: CollectedContextSources = dataclass_field(repr=False)
    non_trigger_sources: FrozenNonTriggerContextSources = dataclass_field(repr=False)
    memory_use_policy: MemoryUsePolicy
    replay_target: ProviderReplayTargetCompatibilityFact = dataclass_field(repr=False)
    retained_skill_selection: FrozenRetainedSkillContextSelection | None = (
        dataclass_field(default=None, repr=False)
    )
    _lock: Lock = dataclass_field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        identity = self.first_candidate_read.compile_snapshot.canonical_input.identity
        if (
            self._resource_authority is None
            or self.model_call_index < 1
            or self.seed.dispatch_read != self.first_candidate_read
            or (self.prepared_call.session_id != identity.session_id)
            or (self.prepared_call.turn_id != identity.turn_id)
            or (self.planning_basis.scope.session_id != identity.session_id)
            or (
                self.planning_basis.scope.scope_kind
                is not identity.conversation_scope_kind
            )
            or (self.tool_view.parent_dispatch_cut is not self.capability_dispatch_cut)
            or (self.skill_view.parent_dispatch_cut is not self.capability_dispatch_cut)
            or (self.tool_exposure_plan.dispatch_view is not self.tool_view)
            or (self.non_trigger_sources.tool_exposure_plan != self.tool_exposure_plan)
            or (self.non_trigger_sources.skill_dispatch_view != self.skill_view)
        ):
            raise ValueError("compaction candidate family does not exact-join")

    @property
    def owns_execution_authority(self) -> bool:
        with self._lock:
            return isinstance(
                self._resource_authority, ProviderDispatchExecutionAuthority
            )

    @property
    def owns_resource_authority(self) -> bool:
        with self._lock:
            return self._resource_authority is not None

    def take_execution_authority(self) -> ProviderDispatchExecutionAuthority:
        with self._lock:
            authority = self._resource_authority
            if isinstance(authority, ProviderDispatchExecutionAuthority):
                self._resource_authority = None
        if not isinstance(authority, ProviderDispatchExecutionAuthority):
            raise RuntimeError("compaction candidate family authority is consumed")
        return authority

    def take_dry_resource_authority(self) -> CompactionDryResourceAuthority:
        with self._lock:
            authority = self._resource_authority
            if isinstance(authority, CompactionDryResourceAuthority):
                self._resource_authority = None
        if not isinstance(authority, CompactionDryResourceAuthority):
            raise RuntimeError("compaction dry resource authority is consumed")
        return authority

    def close(self) -> None:
        with self._lock:
            authority = self._resource_authority
            self._resource_authority = None
        if authority is None:
            raise RuntimeError("compaction candidate family authority is consumed")
        authority.close()


@dataclass(frozen=True, slots=True)
class PreparedCompactionCandidate:
    """One authority-free semantic candidate from a frozen compaction family."""

    wire_candidate: PreparedProviderWireCandidate = dataclass_field(repr=False)
    retained_skill_selection: FrozenRetainedSkillContextSelection | None = (
        dataclass_field(default=None, repr=False)
    )

    def __post_init__(self) -> None:
        if self.wire_candidate.cold_semantic is None:
            raise ValueError("compaction candidate lacks cold semantic assembly")


@dataclass(frozen=True, slots=True)
class InstalledCompactionDrySource:
    predecessor: object = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class PendingEmptyCompactionDrySource:
    reservation: object = dataclass_field(repr=False)


CompactionDryProjectionSource = (
    InstalledCompactionDrySource | PendingEmptyCompactionDrySource
)


@dataclass(frozen=True, slots=True)
class CompactionDryProjectionBasis:
    """Pure destination basis; it carries no install/open capability."""

    source: CompactionDryProjectionSource = dataclass_field(repr=False)
    destination: FrozenEpochModelCallTarget
    canonical_read: FrozenCanonicalProviderDispatchRead = dataclass_field(repr=False)
    capability_dispatch_cut: FrozenCapabilityDispatchCut = dataclass_field(repr=False)

    def __post_init__(self) -> None:
        identity = self.canonical_read.compile_snapshot.canonical_input.identity
        if (
            self.destination.session_id != identity.session_id
            or self.destination.turn_id != identity.turn_id
            or self.capability_dispatch_cut.conversation_scope_kind
            is not identity.conversation_scope_kind
        ):
            raise ValueError("compaction dry basis does not exact-join")


@dataclass(frozen=True, slots=True)
class CompactionDryProjectionResult:
    """Authority-free dry admission; never accepted by register/install."""

    basis: CompactionDryProjectionBasis = dataclass_field(repr=False)
    semantic_projection: FrozenModelInputSemanticProjection = dataclass_field(
        repr=False
    )
    wire_materialization: object = dataclass_field(repr=False)
    quote: FrozenProviderWireInputQuote
    admitted: bool

    def __post_init__(self) -> None:
        if (
            not self.admitted
            or self.semantic_projection.canonical_input_identity
            != self.basis.canonical_read.compile_snapshot.canonical_input.identity
            or self.quote.wire_api
            != self.basis.destination.target_bundle.target_fact.wire_api
        ):
            raise ValueError("compaction dry projection is not admitted")


@dataclass(slots=True)
class PreparedCompactionDryProjection:
    """Resource owner around a non-installable dry result."""

    result: CompactionDryProjectionResult
    _resource_authority: CompactionDryResourceAuthority | None = dataclass_field(
        repr=False, compare=False
    )
    _lock: Lock = dataclass_field(default_factory=Lock, init=False, repr=False)
    _empty_adoption_started: bool = dataclass_field(
        default=False, init=False, repr=False
    )

    def _require_for_empty_adoption(
        self,
    ) -> tuple[SealedProviderInputPreparationBasis, PreparedProviderInputHandle]:
        with self._lock:
            authority = self._resource_authority
            start = not self._empty_adoption_started
            self._empty_adoption_started = True
        if authority is None:
            raise RuntimeError("compaction dry projection authority is consumed")
        if start:
            authority.begin_empty_adoption()
        return authority.require_for_empty_adoption()

    def take_handle_for_rotation(self) -> PreparedProviderInputHandle:
        with self._lock:
            authority = self._resource_authority
            self._resource_authority = None
        if authority is None:
            raise RuntimeError("compaction dry projection authority is consumed")
        return authority.take_handle_for_rotation()

    def close_after_empty_adoption_none(self) -> None:
        with self._lock:
            authority = self._resource_authority
        if authority is None:
            raise RuntimeError("compaction dry projection authority is consumed")
        authority.restore_empty_adoption_abort()
        self.close()

    def close(self) -> None:
        with self._lock:
            authority = self._resource_authority
            self._resource_authority = None
        if authority is None:
            raise RuntimeError("compaction dry projection authority is consumed")
        authority.close()


@dataclass(slots=True)
class PreparedCompactionSourceDispatch:
    """Non-executable normal semantic projection for compaction planning."""

    _handle: PreparedProviderInputHandle | None = dataclass_field(repr=False)
    _continuity_owner: HostProviderInputContinuityOwner = dataclass_field(
        repr=False, compare=False
    )
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
    _planning_owned: bool = dataclass_field(
        default=True, init=False, repr=False, compare=False
    )

    def _settle_planning(self, *, transfer: bool) -> None:
        with self._lock:
            owned = self._planning_owned
            self._planning_owned = False
        if owned and not transfer:
            self._continuity_owner.abort_planning(self.planning)

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
        self._settle_planning(transfer=False)
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
        self._settle_planning(transfer=True)
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
            try:
                handle.close()
            finally:
                self._settle_planning(transfer=False)


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
    agent_invocation_snapshot: FrozenAgentInvocationSnapshot | None = dataclass_field(
        default=None, repr=False
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
        and candidate.compile_request == dispatch.compile_request
        and candidate.tool_exposure_plan == dispatch.tool_exposure_plan
        and candidate.memory_context == dispatch.memory_context
    )


def _semantic_projection_from_compiled(
    compiled: FrozenCompiledModelInput,
) -> FrozenModelInputSemanticProjection:
    return FrozenModelInputSemanticProjection(
        canonical_input_identity=compiled.canonical_input_identity,
        system_prompt=compiled.system_prompt,
        messages=compiled.messages,
        message_placements=compiled.message_placements,
        tools=compiled.tools,
        source_decisions=compiled.source_decisions,
        tool_result_decisions=compiled.tool_result_decisions,
        diagnostic_codes=compiled.diagnostic_codes,
        source_collection_fingerprint=compiled.source_collection_fingerprint,
        compile_binding_fingerprint=compiled.compile_binding_fingerprint,
    )
