"""Fresh Stage 2 foreground conversation runner.

The runner owns only one live Host activation.  It never resumes a provider,
coroutine, interaction, terminal process, or subagent execution after a crash.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from time import monotonic
from threading import Lock
from typing import Mapping, Protocol, TypeVar
from uuid import uuid4

from jsonschema import ValidationError, validators

from pulsara_agent.conversation_kernel.assembler import (
    CompletedAssistantMessage,
    CompletedDataBlock,
    CompletedTextBlock,
    CompletedToolCallBlock,
    ProviderStreamAssembler,
)
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceCollectorPort,
    build_memory_context_source,
    replace_memory_context_sources,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
    stable_subagent_turn_id,
)
from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
    PreparedKernelModelCall,
    PreparedKernelModelExecution,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    InlineContent,
    TurnStatus,
    WriterLease,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
    LiveSettlementKind,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.ports.live_agent_event import (
    ToolResultDeltaPayload,
    ToolResultEndPayload,
    ToolResultStartPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.extensions import (
    KernelExtensionHost,
    OperationalHookOffer,
    OperationalHookType,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    ModelVisibleMemoryProvenanceDisposition,
    MemoryCitationEvidenceKind,
    MemoryCitationVisibility,
    MemoryUsePolicy,
    PreparedMemoryCandidateAcceptance,
    strongest_memory_use_policy,
)
from pulsara_agent.conversation_kernel.memory.citations import (
    ProcessLocalMemoryCallContextOwner,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AcceptedPlanToolBatch,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    ConversationKernelConflict,
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PreparedRootTurnAdmission,
    PreparedSubagentTurnAdmission,
    PreparedToolRemoteIdentityPublication,
    PreparedToolResultAcceptance,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    TurnAdmissionConfirmationKind,
    build_prepared_root_turn_admission,
    build_prepared_subagent_turn_admission,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.plan_runtime import (
    KernelPlanInteractionCoordinator,
    PlanQuestionWaiter,
)
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.primitives.plan_workflow import (
    PlanInteractionBinding,
    PlanInteractionKind,
    extract_plan_draft,
    extract_plan_entry_reason,
    extract_plan_question,
)
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    TrustedToolObservationSupplement,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    ProviderSafePointCoordinator,
)
from pulsara_agent.conversation_kernel.steer import (
    MAXIMUM_STEER_CANDIDATE_UTF8_BYTES,
    MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES,
    AcceptedSteerDispatchBatch,
    AcceptedSteerDispatchEntry,
    PendingPromptSteerFact,
    PreparedSteerResourceRejection,
    PreparedSteerPlanConflictInterruption,
    PreparedSteerSuffixAdmissionPlan,
    MemorySourceInvalidationReservation,
    SteerConsumptionConfirmationKind,
    SteerPlanConflictConfirmationKind,
    SteerResourceRejectionConfirmationKind,
    build_accepted_steer_dispatch_batch,
    build_prepared_steer_suffix_plan,
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
    build_steer_plan_conflict_interruption,
    build_steer_resource_rejection,
    build_steer_suffix_quote,
    build_memory_source_invalidation_reservation,
)
from pulsara_agent.ports.terminal_observation import PreparedInstallationTarget
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    thaw_tool_json_object,
)
from pulsara_agent.model_input.compiler import (
    COMPILER_CONTRACT_VERSION,
    StructuredModelInputCompiler,
)
from pulsara_agent.model_input.diagnostics import (
    project_model_input_compile_observation,
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
    ContextSourceLifecycle,
    FrozenCanonicalCompileSnapshot,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    PreparedProviderInputCut,
    FrozenCompiledModelInput,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    canonical_compile_snapshot_fingerprint,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    NewTriggerAnchor,
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    NoNewTriggerAnchor,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
    PreparedProviderInputAppendCandidate,
    ProcessLocalCanonicalFrontier,
    ProcessLocalProviderInputInstallPermit,
    ProcessLocalSourceHead,
    ProviderInputContinuityScope,
    ProviderInputEpochCompatibility,
    provider_input_logical_utf8_bytes,
    encode_runtime_observation,
    SourceObservationLifecycle,
    SourceObservationPresence,
    prepared_provider_input_append_candidate_fingerprint,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE, PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
    tool_observation_origin_for_binding,
)


_T = TypeVar("_T")


class _PreparedSteerPlanStale(ConversationKernelConflict):
    """The frozen pre-consumption plan lost before its first mutation."""


class KernelModelPort(Protocol):
    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall: ...

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        expected_append_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> PreparedKernelModelExecution: ...


class MemoryContextProjectionPort(Protocol):
    async def freeze_response_preference_source(
        self,
    ) -> ContextSourceCandidate | ContextSourceAbsentFact: ...

    async def freeze_automatic_recall_source(
        self, query: str
    ) -> ContextSourceCandidate | ContextSourceAbsentFact: ...

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition: ...

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy: ...

    def offer_candidate_wake(self, candidate_id: str) -> None: ...

    def prepare_and_adopt_reflection(
        self,
        *,
        canonical: CanonicalModelInputSnapshot,
        permission: FrozenRunPermissionSnapshot,
        remember_requested: bool,
    ) -> str | None: ...


class AutomaticPlanContinuationPort(Protocol):
    async def __call__(
        self,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch: ...


@dataclass(frozen=True, slots=True)
class KernelToolResult:
    state: str
    content: bytes
    memory_candidate: PreparedMemoryCandidateAcceptance | None = None
    remote_identity: str | None = None
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    artifact_source_read: bool = False
    process_local_settlement: "ProcessLocalEffectSettlementToken | None" = None
    physical_timing: str = "ON_TIME"
    caller_cancelled_while_running: bool = False
    effect_class: str | None = None
    physical_observation: PhysicalToolObservationSupplement | None = None
    trusted_observation: TrustedToolObservationSupplement | None = None
    model_visible_memory_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The model-facing tool result and artifact candidate are both strict
        # UTF-8 product contracts.  No errors="replace" lowering is allowed.
        self.content.decode("utf-8")
        if self.artifact_source_read and self.output_artifact_candidate is not None:
            raise ValueError("artifact_read cannot recursively own an artifact")


@dataclass(frozen=True, slots=True)
class _KnownToolResultSettlementOutcome:
    accepted: AcceptedEntry
    process_local_effect_committed: bool


@dataclass(slots=True)
class _TurnAdmissionSettlementAttempt:
    candidate: PreparedRootTurnAdmission | PreparedSubagentTurnAdmission
    root: bool
    reissue_allowed: bool
    cancellation_requested: bool = False
    cancellation_intent: ActiveTurnCancellationIntent | None = None


class KernelToolPhysicalInvocationError(RuntimeError):
    """Process-local exact exception classified by the frozen tool contract."""

    def __init__(
        self,
        *,
        effect_class: str,
        error: BaseException,
        timing: str,
        caller_cancelled: bool,
        physical_observation: PhysicalToolObservationSupplement | None = None,
    ) -> None:
        self.effect_class = effect_class
        self.physical_error = error
        self.timing = timing
        self.caller_cancelled = caller_cancelled
        self.physical_observation = physical_observation
        super().__init__(f"tool physical invocation raised: {type(error).__name__}")


@dataclass(frozen=True, slots=True)
class KernelToolInvocationContext:
    session_id: str
    workspace_id: str
    turn_id: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str
    result_entry_id: str
    conversation_scope_kind: str
    scope_subagent_task_id: str | None
    host_owner_epoch: int
    authorization_reference: str
    permission_snapshot_fingerprint: str
    attempt_permission_snapshot_fingerprint: str
    tool_surface_fingerprint: str
    executor_binding_fingerprint: str
    surface_borrow: ProcessLocalToolSurfaceBorrow = dataclass_field(
        repr=False, compare=False
    )
    memory_context: FrozenModelCallMemoryContext = dataclass_field(
        default_factory=lambda: FrozenModelCallMemoryContext(
            FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.COMPLETE,
                (),
            )
        ),
        repr=False,
    )

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.turn_id,
                self.assistant_entry_id,
                self.tool_call_id,
                self.attempt_id,
                self.result_entry_id,
                self.authorization_reference,
                self.permission_snapshot_fingerprint,
                self.attempt_permission_snapshot_fingerprint,
                self.tool_surface_fingerprint,
                self.executor_binding_fingerprint,
            )
        ):
            raise ValueError("kernel tool invocation context is incomplete")
        if self.conversation_scope_kind not in {"ROOT", "SUBAGENT_TASK"}:
            raise ValueError("kernel tool invocation scope is invalid")
        if (self.conversation_scope_kind == "ROOT") != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("kernel tool invocation scope identity is invalid")
        if (
            self.attempt_permission_snapshot_fingerprint
            != self.permission_snapshot_fingerprint
        ):
            raise ValueError(
                "tool attempt permission snapshot does not exact-join the run"
            )
        access = self.surface_borrow.prepared.access
        if (
            access.conversation_scope_kind.value != self.conversation_scope_kind
            or access.scope_subagent_task_id != self.scope_subagent_task_id
        ):
            raise ValueError("kernel tool invocation scope access does not exact-join")


@dataclass(frozen=True, slots=True)
class ProcessLocalEffectSettlementToken:
    token_id: str
    token_fingerprint: str

    def __post_init__(self) -> None:
        if not self.token_id or not self.token_fingerprint.startswith("sha256:"):
            raise ValueError("process-local settlement token is invalid")


class ProcessLocalEffectSettlementDisposition(StrEnum):
    COMMITTED = "COMMITTED"
    DISCARDED = "DISCARDED"


class KernelToolLiveSink(Protocol):
    def offer_text(self, text: str) -> None: ...


class _ToolResultLiveSink:
    """Thread-safe, bounded, mechanically coalescing live handoff."""

    _MAXIMUM_PENDING_BYTES = 1 * 1024 * 1024

    def __init__(
        self,
        *,
        live_bus: LiveAgentEventBus,
        session_id: str,
        turn_id: str,
        draft_identity: str,
        block_identity: str,
        attribution: Mapping[str, object],
    ) -> None:
        self._live_bus = live_bus
        self._session_id = session_id
        self._turn_id = turn_id
        self._draft_identity = draft_identity
        self._block_identity = block_identity
        self._attribution = dict(attribution)
        self._loop = asyncio.get_running_loop()
        self._lock = Lock()
        self._pending: list[str] = []
        self._pending_bytes = 0
        self._scheduled = False
        self._closed = False
        self._overflowed = False
        self._gap_pending = False
        self._drained = asyncio.Event()
        self._drained.set()
        self.emitted = False

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed

    def offer_text(self, text: str) -> None:
        if not text:
            return
        encoded = text.encode("utf-8")
        with self._lock:
            if self._closed:
                return
            self._drained.clear()
            remaining = self._MAXIMUM_PENDING_BYTES - self._pending_bytes
            if len(encoded) > remaining:
                if not self._overflowed:
                    self._gap_pending = True
                self._overflowed = True
                if remaining <= 0:
                    return
                encoded = encoded[:remaining]
                while encoded:
                    try:
                        text = encoded.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        encoded = encoded[:-1]
                if not encoded:
                    return
            self._pending.append(text)
            self._pending_bytes += len(encoded)
            if self._scheduled:
                return
            self._scheduled = True
        self._loop.call_soon_threadsafe(self._drain_nowait)

    def _drain_nowait(self) -> None:
        with self._lock:
            text = "".join(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            self._scheduled = False
            gap = self._gap_pending
            self._gap_pending = False
        if gap:
            self._live_bus.invalidate_observation_generation_nowait()
        if text:
            self.emitted = True
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TOOL_RESULT_DELTA,
                session_id=self._session_id,
                turn_id=self._turn_id,
                draft_identity=self._draft_identity,
                payload=ToolResultDeltaPayload(self._block_identity, text),
                block_id=self._block_identity,
                block_ordinal=0,
                block_kind=LiveBlockKind.TOOL_RESULT,
                **self._attribution,
            )
        with self._lock:
            if self._pending and not self._scheduled:
                self._scheduled = True
                self._loop.call_soon(self._drain_nowait)
                return
            self._drained.set()

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._pending and not self._scheduled:
                self._scheduled = True
                self._loop.call_soon(self._drain_nowait)
        await asyncio.wait_for(self._drained.wait(), timeout=1.0)


class KernelToolAuthorizationKind(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"


@dataclass(frozen=True, slots=True)
class KernelToolAuthorization:
    kind: KernelToolAuthorizationKind
    reference: str
    public_message: str = ""
    accepted_attempt_id: str | None = None
    accepted_result_entry_id: str | None = None
    accepted_permission_snapshot_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            self.accepted_attempt_id is not None
            and self.kind is not KernelToolAuthorizationKind.ALLOW
        ):
            raise ValueError("only an allowed authorization may own an attempt")
        if (
            self.accepted_result_entry_id is not None
            and self.kind is not KernelToolAuthorizationKind.PERMISSION_DENIED
        ):
            raise ValueError("only a denied authorization may own a result")
        if (
            self.accepted_attempt_id is not None
            and self.accepted_result_entry_id is not None
        ):
            raise ValueError("authorization effect union is invalid")
        if (self.accepted_attempt_id is not None) != (
            self.accepted_permission_snapshot_fingerprint is not None
        ):
            raise ValueError("accepted attempt permission attribution is incomplete")


class KernelToolPort(Protocol):
    def snapshot_tool_surface(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedKernelToolSurface: ...

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow: ...

    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None: ...

    async def authorize(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        memory_context: FrozenModelCallMemoryContext,
    ) -> KernelToolAuthorization: ...

    async def request_confirmation(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        attempt_id: str,
        turn_id: str,
        assistant_entry_id: str,
        invocation_context: KernelToolInvocationContext,
        live_sink: KernelToolLiveSink | None = None,
    ) -> KernelToolResult: ...

    async def settle_process_local_effect(
        self,
        token: ProcessLocalEffectSettlementToken,
        disposition: ProcessLocalEffectSettlementDisposition,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class KernelRunResult:
    turn_id: str
    final_entry_id: str
    final_text: str
    model_call_count: int
    tool_call_count: int
    continuation_turn_id: str | None = None
    continuation_entry_id: str | None = None
    pending_plan_interaction_id: str | None = None
    memory_reflection_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PreparedProviderDispatch:
    handle: PreparedProviderInputHandle
    canonical_facts: FrozenCanonicalCompileSnapshot
    planning: FrozenProviderInputAppendPlanningInput
    prepared_call: PreparedKernelModelCall
    surface_borrow: ProcessLocalToolSurfaceBorrow
    sources: CollectedContextSources
    append_result: FrozenProviderInputAppendCompileResult
    memory_context: FrozenModelCallMemoryContext
    accepted_steers: AcceptedSteerDispatchBatch | None = None


class ConversationKernelRunner:
    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        model: KernelModelPort,
        tools: KernelToolPort,
        live_bus: LiveAgentEventBus,
        input_reader: CanonicalProviderInputReader | None = None,
        safe_point: ProviderSafePointCoordinator | None = None,
        content_publisher: CanonicalContentPublisher | None = None,
        io_owner: KernelSessionIO | None = None,
        context_source_collector: ContextSourceCollectorPort,
        compiler: StructuredModelInputCompiler | None = None,
        continuity_owner: HostProviderInputContinuityOwner | None = None,
        extensions: KernelExtensionHost | None = None,
        workspace_id: str | None = None,
        tool_output_processor: ToolOutputArtifactProcessor | None = None,
        plan_interactions: KernelPlanInteractionCoordinator | None = None,
        automatic_plan_continuation: AutomaticPlanContinuationPort | None = None,
        launch_permission_mode: PermissionMode = DEFAULT_PERMISSION_MODE,
        maximum_input_tokens_per_call: int = STAGE2_LIMITS.provider_input_tokens_per_call_hard,
        maximum_output_tokens_per_call: int = STAGE2_LIMITS.provider_output_tokens_per_call_hard,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        memory_projection: MemoryContextProjectionPort | None = None,
    ) -> None:
        if (
            min(
                maximum_input_tokens_per_call,
                maximum_output_tokens_per_call,
            )
            < 1
        ):
            raise ValueError("runner limits must be finite and positive")
        self._repository = repository
        self._plan_interactions = plan_interactions
        self._automatic_plan_continuation = automatic_plan_continuation
        self._writer_lease = writer_lease
        self._model = model
        self._tools = tools
        self._live_bus = live_bus
        self._input_reader = input_reader or CanonicalProviderInputReader(
            repository.connection_provider,
            blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
        )
        self._blob_store = PostgresCanonicalBlobStore(repository.connection_provider)
        self._safe_point = safe_point or ProviderSafePointCoordinator(
            repository=repository,
            guard=writer_lease.guard,
        )
        self._content_publisher = content_publisher or CanonicalContentPublisher(
            repository.connection_provider
        )
        self._tool_output_processor = (
            tool_output_processor
            or ToolOutputArtifactProcessor(repository.connection_provider)
        )
        self._launch_permission_mode = launch_permission_mode
        self._workspace_id = workspace_id
        self._io = io_owner or KernelSessionIO()
        self._context_source_collector = context_source_collector
        self._compiler = compiler or StructuredModelInputCompiler()
        self._continuity = continuity_owner or HostProviderInputContinuityOwner(
            session_id=writer_lease.guard.session_id
        )
        self._memory_contexts = ProcessLocalMemoryCallContextOwner(
            session_id=writer_lease.guard.session_id
        )
        self._extensions = extensions
        self._maximum_input_tokens_per_call = maximum_input_tokens_per_call
        self._maximum_output_tokens_per_call = maximum_output_tokens_per_call
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._memory_projection = memory_projection
        self._root_memory_use_policy = MemoryUsePolicy.ENABLED

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _planning_deadline(self) -> float:
        return self._deadlines.deadline(
            KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING
        )

    async def run_turn(
        self,
        text: str,
        *,
        command_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> KernelRunResult:
        return await self._run_turn(
            text,
            command_id=command_id,
            subagent_task_id=None,
            requested_permission_mode=(
                requested_permission_mode or self._launch_permission_mode
            ),
            cancellation_intent=cancellation_intent,
        )

    async def run_subagent_turn(
        self,
        *,
        task_id: str,
        objective: str,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> KernelRunResult:
        if not task_id:
            raise ValueError("subagent task identity is required")
        try:
            return await self._run_turn(
                objective,
                command_id=None,
                subagent_task_id=task_id,
                requested_permission_mode=None,
                cancellation_intent=cancellation_intent,
            )
        finally:
            scope = ProviderInputContinuityScope(
                session_id=self._writer_lease.guard.session_id,
                scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id=task_id,
            )
            self._continuity.discard_scope(scope)
            self._memory_contexts.discard_scope(scope)

    async def _run_turn(
        self,
        text: str,
        *,
        command_id: str | None,
        subagent_task_id: str | None,
        requested_permission_mode: PermissionMode | None,
        cancellation_intent: ActiveTurnCancellationIntent | None,
    ) -> KernelRunResult:
        if not text:
            raise ValueError("user message must be non-empty")
        if subagent_task_id is None:
            stable_command_id = command_id or _id("command")
            turn_id = _stable_id(
                "turn", self._writer_lease.guard.session_id, stable_command_id
            )
            content = await self._content(
                text.encode("utf-8"), deadline=self._canonical_deadline()
            )
            occurred_at = datetime.now(timezone.utc)
            candidate = build_prepared_root_turn_admission(
                session_id=self._writer_lease.guard.session_id,
                command_id=stable_command_id,
                turn_id=turn_id,
                entry_id=_stable_id("entry", turn_id, "user"),
                context_binding_revision_id=_stable_id(
                    "context-revision", turn_id, "0"
                ),
                permission_snapshot_id=_stable_id("permission-snapshot", turn_id),
                requested_permission_mode=(
                    requested_permission_mode or self._launch_permission_mode
                ),
                content=content,
                occurred_at=occurred_at,
            )
            intent = cancellation_intent or ActiveTurnCancellationIntent(
                turn_id, ModelInputScopeKind.ROOT, None
            )
            intent.require_exact(
                turn_id=turn_id,
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            await self._accept_root_turn_exact(candidate, cancellation_intent=intent)
        else:
            turn_id = stable_subagent_turn_id(
                session_id=self._writer_lease.guard.session_id,
                task_id=subagent_task_id,
            )
            content = await self._content(
                text.encode("utf-8"), deadline=self._canonical_deadline()
            )
            occurred_at = datetime.now(timezone.utc)
            candidate = build_prepared_subagent_turn_admission(
                session_id=self._writer_lease.guard.session_id,
                task_id=subagent_task_id,
                turn_id=turn_id,
                entry_id=_stable_id("entry", turn_id, "objective"),
                context_binding_revision_id=_stable_id(
                    "context-revision", turn_id, "0"
                ),
                permission_snapshot_id=_stable_id("permission-snapshot", turn_id),
                content=content,
                occurred_at=occurred_at,
                actor_id="subagent-manager",
            )
            intent = cancellation_intent or ActiveTurnCancellationIntent(
                turn_id, ModelInputScopeKind.SUBAGENT_TASK, subagent_task_id
            )
            intent.require_exact(
                turn_id=turn_id,
                scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id=subagent_task_id,
            )
            await self._accept_subagent_turn_exact(
                candidate, cancellation_intent=intent
            )
        return await self.run_accepted_turn(turn_id, cancellation_intent=intent)

    async def _accept_root_turn_exact(
        self,
        candidate: PreparedRootTurnAdmission,
        *,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry:
        return await self._accept_turn_exact(
            candidate=candidate,
            root=True,
            cancellation_intent=cancellation_intent,
        )

    async def _accept_subagent_turn_exact(
        self,
        candidate: PreparedSubagentTurnAdmission,
        *,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry:
        return await self._accept_turn_exact(
            candidate=candidate,
            root=False,
            cancellation_intent=cancellation_intent,
        )

    async def _accept_turn_exact(
        self,
        *,
        candidate: PreparedRootTurnAdmission | PreparedSubagentTurnAdmission,
        root: bool,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry:
        accept_operation = (
            self._repository.accept_root_turn
            if root
            else self._repository.accept_subagent_turn
        )
        try:
            return await self._io.run(
                accept_operation,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
        except asyncio.CancelledError as cancellation:
            attempt = _TurnAdmissionSettlementAttempt(
                candidate=candidate,
                root=root,
                reissue_allowed=False,
                cancellation_requested=True,
                cancellation_intent=cancellation_intent,
            )
            settlement = asyncio.create_task(
                self._settle_turn_admission(attempt),
                name=f"kernel-cancelled-turn-admission:{candidate.turn_id}",
            )
            await _await_turn_admission_settlement(settlement, attempt)
            raise cancellation
        except BaseException:
            attempt = _TurnAdmissionSettlementAttempt(
                candidate=candidate,
                root=root,
                reissue_allowed=True,
            )
            settlement = asyncio.create_task(
                self._settle_turn_admission(attempt),
                name=f"kernel-turn-admission-settlement:{candidate.turn_id}",
            )
            accepted, cancellation = await _await_turn_admission_settlement(
                settlement, attempt
            )
            if cancellation is not None:
                raise cancellation
            if accepted is None:
                raise ConversationKernelConflict("turn admission did not settle")
            return accepted

    async def _settle_turn_admission(
        self,
        attempt: _TurnAdmissionSettlementAttempt,
    ) -> AcceptedEntry | None:
        """Own one ACK-unknown admission until its exact state is proved.

        The shielded process-local task remains attached to the Host runner
        through transient confirmation failures.  Cancellation only disables
        future writes; a write already in flight is confirmed and any FULL
        winner is interrupted before the caller observes cancellation.
        """

        confirmation_operation = (
            self._repository.confirm_root_turn_admission
            if attempt.root
            else self._repository.confirm_subagent_turn_admission
        )
        accept_operation = (
            self._repository.accept_root_turn
            if attempt.root
            else self._repository.accept_subagent_turn
        )
        while True:
            try:
                confirmation = await self._io.run(
                    confirmation_operation,
                    candidate=attempt.candidate,
                    guard=self._writer_lease.guard,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                assert confirmation.accepted is not None
                if attempt.cancellation_requested:
                    if attempt.root:
                        await self._settle_failed_turn_worker(
                            attempt.candidate.turn_id,
                            _root_cancellation_terminal_reason(
                                attempt.cancellation_intent
                            ),
                        )
                    return None
                return confirmation.accepted
            if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                kind = "ROOT" if attempt.root else "subagent"
                raise ConversationKernelConflict(
                    f"{kind} turn admission has a conflicting winner"
                )
            if attempt.cancellation_requested or not attempt.reissue_allowed:
                return None
            try:
                accepted = await self._io.run(
                    accept_operation,
                    self._writer_lease.guard,
                    candidate=attempt.candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                continue
            if attempt.cancellation_requested:
                if attempt.root:
                    await self._settle_failed_turn_worker(
                        attempt.candidate.turn_id,
                        _root_cancellation_terminal_reason(
                            attempt.cancellation_intent
                        ),
                    )
                return None
            return accepted

    async def _prepare_provider_dispatch(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        deadline: float,
    ) -> _PreparedProviderDispatch:
        """Freeze, quote and (when present) consume one exact steer suffix."""

        prepare_surface = getattr(self._tools, "prepare_tool_surface_safe_point", None)
        if prepare_surface is not None:
            prepare_surface()
        handle = await self._io.run(
            self._safe_point.freeze_provider_input,
            turn_id=turn_id,
            deadline_monotonic=deadline,
        )
        borrow: ProcessLocalToolSurfaceBorrow | None = None
        try:
            base_facts = await self._read_compile_snapshot(
                handle.cut, deadline=deadline
            )
            base_input = base_facts.canonical_input
            identity = base_input.identity
            scope = ProviderInputContinuityScope(
                session_id=identity.session_id,
                scope_kind=identity.conversation_scope_kind,
                scope_subagent_task_id=identity.scope_subagent_task_id,
            )
            base_frontier = _canonical_frontier(base_input, base_facts)
            current_epoch = self._continuity.current_view(scope)
            predecessor_count = (
                0
                if current_epoch is None
                or current_epoch.canonical_frontier.context_base_semantic_identity
                != base_frontier.context_base_semantic_identity
                else len(current_epoch.canonical_frontier.ordered_item_fingerprints)
            )
            base_anchor = _dispatch_anchor(
                base_input,
                predecessor_item_count=predecessor_count,
                model_call_index=model_call_index,
            )

            try:
                surface = self._tools.snapshot_tool_surface(
                    conversation_scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                )
                borrow = self._tools.borrow_tool_surface(surface)
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                ) from exc
            try:
                prepared_call = self._model.prepare_call(
                    KernelModelPreparationRequest(
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        model_call_index=model_call_index,
                        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                        maximum_input_tokens=self._maximum_input_tokens_per_call,
                        maximum_output_tokens=self._maximum_output_tokens_per_call,
                        tool_surface=surface,
                    )
                )
            except Exception as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.MODEL_TARGET_PREPARATION_FAILED
                ) from exc
            try:
                frozen_sources = await self._io.run(
                    self._context_source_collector.freeze_non_trigger_sources,
                    tool_surface=surface.model_surface,
                    canonical_facts=base_facts,
                    deadline_monotonic=deadline,
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
                if identity.conversation_scope_kind is not ModelInputScopeKind.ROOT
                else await self._io.run(
                    self._repository.read_pending_prompt_steer_facts,
                    session_id=identity.session_id,
                    target_turn_id=turn_id,
                    deadline_monotonic=deadline,
                )
            )
            hydrated = await self._hydrate_pending_steers(pending, deadline=deadline)
            selected_plan: PreparedSteerSuffixAdmissionPlan | None = None
            selected_facts: FrozenCanonicalCompileSnapshot | None = None
            selected_sources: CollectedContextSources | None = None
            selected_append: FrozenProviderInputAppendCompileResult | None = None
            selected_memory_context: FrozenModelCallMemoryContext | None = None
            selected_activation_text: str | None = None
            prepared_preference: ContextSourceCandidate | ContextSourceAbsentFact | None = None
            selected_preference: ContextSourceCandidate | ContextSourceAbsentFact | None = None
            selected_trigger_disposition: str | None = None
            selected_memory_use_policy = inherited_memory_use_policy

            # A steer batch is appended to the already-admitted ROOT prompt.
            # Classify that exact base prompt first so a new HUMAN_MESSAGE
            # resets the policy epoch even when busy-Enter steers arrived
            # before the first provider dispatch.  The ordered steer prefix
            # below may only strengthen this base policy.
            steer_base_memory_use_policy = inherited_memory_use_policy
            if self._memory_projection is not None:
                base_activation_subject, base_activation_text = (
                    _activation_subject_for_anchor(base_input, base_anchor)
                )
                if (
                    base_activation_subject
                    is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
                ):
                    base_trigger_policy = (
                        self._memory_projection.classify_memory_trigger(
                            base_activation_text
                        )
                    )
                    base_trigger_origin = _input_origin_for_anchor(
                        base_input, base_anchor
                    )
                    steer_base_memory_use_policy = (
                        base_trigger_policy.memory_use
                        if base_trigger_origin
                        is CanonicalInputOriginKind.HUMAN_MESSAGE
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
                    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS
                    - len(base_input.items),
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
                planning_work_bytes = (
                    base_input.canonical_utf8_bytes + eligible_bytes
                )
                if planning_work_bytes > (
                    MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES
                ):
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
                    prospective_frontier = _canonical_frontier(
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
                    if self._memory_projection is not None:
                        trigger_policies = tuple(
                            self._memory_projection.classify_memory_trigger(
                                body.decode("utf-8")
                            )
                            for _fact, body in prefix
                        )
                        for trigger_policy in trigger_policies:
                            memory_use_policy = strongest_memory_use_policy(
                                memory_use_policy,
                                trigger_policy.memory_use,
                            )
                        trigger_disposition = str(
                            trigger_policies[-1].automatic_recall
                        )
                        if (
                            memory_use_policy
                            is MemoryUsePolicy.ALL_DISABLED_BY_USER
                        ):
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
                        self._memory_projection is not None
                        and memory_use_policy
                        is not MemoryUsePolicy.ALL_DISABLED_BY_USER
                        and effective_preference is None
                    ):
                        prepared_preference = (
                            await self._memory_projection.freeze_response_preference_source()
                        )
                        effective_preference = prepared_preference
                    if (
                        self._memory_projection is not None
                        and memory_use_policy
                        is MemoryUsePolicy.ALL_DISABLED_BY_USER
                    ):
                        effective_preference = build_memory_context_source(
                            kind=(
                                ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
                            ),
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
                            candidates[-1].candidate_fingerprint,
                        ),
                        model_call_index=model_call_index,
                        canonical_input=prospective_input,
                        canonical_facts=prospective,
                        compile_binding=prepared_call.compile_binding,
                        sources=sources,
                        dispatch_anchor_entry_id=anchor.source_entry_id,
                        memory_citation_handles=(
                            memory_snapshot := self._freeze_memory_call_context(
                                scope=scope,
                                planning=planning,
                                canonical_facts=prospective,
                                sources=sources,
                                memory_use_policy=memory_use_policy,
                            )
                        )[1],
                    )
                    compatibility = _provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=prospective,
                        sources=sources,
                    )
                    try:
                        append = await self._io.run(
                            _compile_structured_append,
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
                        }:
                            raise
                        continue
                    recall_reservation = None
                    preference_reservation = None
                    if effective_preference is not None:
                        recall_reservation, preference_reservation = (
                            self._memory_planning_reservations(
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
                        memory_response_preference_reservation=(
                            preference_reservation
                        ),
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
                        tool_surface_fingerprint=(
                            surface.model_surface.surface_fingerprint
                        ),
                        source_facts_fingerprint=sources.collection_fingerprint,
                        ordered_pending_queue_fingerprints=tuple(
                            item.fact_fingerprint for item, _body in all_hydrated
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
                    source_plan_fingerprint = context_fingerprint(
                        "pulsara:unfit-steer-source-plan:v1",
                        {
                            "scope": (
                                scope.session_id,
                                scope.scope_kind.value,
                                scope.scope_subagent_task_id,
                            ),
                            "base_cut": _provider_cut_fingerprint(handle.cut),
                            "base_frontier": _canonical_frontier_fingerprint(
                                base_frontier
                            ),
                            "base_compile": (base_facts.canonical_read_cut_fingerprint),
                            "target": prepared_call.compile_binding.binding_fingerprint,
                            "surface": surface.model_surface.surface_fingerprint,
                            "sources": frozen_sources.freeze_fingerprint,
                            "pending": tuple(
                                item.fact_fingerprint
                                for item, _body in all_hydrated
                            ),
                        },
                    )
                    rejection = build_steer_resource_rejection(
                        source_plan_fingerprint=source_plan_fingerprint,
                        fact=all_hydrated[0][0],
                        occurred_at=occurred_at,
                        actor_id=self._writer_lease.guard.writer_owner_id,
                    )
                    await self._settle_steer_resource_rejection(
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
                    self._tools.validate_tool_surface_borrow(borrow, surface)
                    if (
                        frozen_sources.registry_fingerprint
                        != self._context_source_collector.registry_fingerprint
                    ):
                        raise RuntimeError("context source registry drifted")
                except Exception as exc:
                    raise _PreparedSteerPlanStale(
                        "prepared steer process-local facts changed before consumption"
                    ) from exc
                accepted_entries = await self._consume_prepared_steer_plan(selected_plan)
                try:
                    canonical_deadline = self._canonical_deadline()
                    handle = await self._io.run(
                        self._safe_point.rotate_provider_input,
                        handle,
                        turn_id=turn_id,
                        deadline_monotonic=canonical_deadline,
                    )
                    actual = await self._read_compile_snapshot(
                        handle.cut, deadline=canonical_deadline
                    )
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
                    await self._settle_post_consumption_plan_conflict(selected_plan)
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
                final_sources = await self._apply_memory_sources(
                    selected_sources,
                    activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
                    activation_text=selected_activation_text,
                    include_recall=True,
                    frozen_preference=selected_preference,
                    trigger_disposition=selected_trigger_disposition,
                )
                final_memory = self._freeze_memory_call_context(
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
                        selected_plan.plan_fingerprint,
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
                final_append, final_sources = await self._compile_with_memory_fallback(
                    request=final_request,
                    planning=selected_plan.predecessor,
                    prepared_call=prepared_call,
                    canonical_facts=actual,
                    sources=final_sources,
                    preference_source=selected_preference,
                    recall_reservation=(
                        selected_plan.quote.memory_recall_reservation
                    ),
                    preference_reservation=(
                        selected_plan.quote.memory_response_preference_reservation
                    ),
                    scope=scope,
                    memory_use_policy=selected_memory_use_policy,
                    deadline=deadline,
                )
                final_memory = self._freeze_memory_call_context(
                    scope=scope,
                    planning=selected_plan.predecessor,
                    canonical_facts=actual,
                    sources=final_sources,
                    memory_use_policy=selected_memory_use_policy,
                )
                return _PreparedProviderDispatch(
                    handle=handle,
                    canonical_facts=actual,
                    planning=selected_plan.predecessor,
                    prepared_call=prepared_call,
                    surface_borrow=borrow,
                    sources=final_sources,
                    append_result=final_append,
                    memory_context=final_memory[0],
                    accepted_steers=batch,
                )

            planning = self._continuity.freeze_planning_input(
                scope=scope,
                canonical_frontier=base_frontier,
                dispatch_anchor=base_anchor,
            )
            activation_subject, activation_text = _activation_subject_for_anchor(
                base_input, base_anchor
            )
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
                self._memory_projection is not None
                and activation_subject
                is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
            ):
                trigger_policy = self._memory_projection.classify_memory_trigger(
                    activation_text
                )
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
                        await self._memory_projection.freeze_response_preference_source()
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
                    memory_snapshot := self._freeze_memory_call_context(
                        scope=scope,
                        planning=planning,
                        canonical_facts=base_facts,
                        sources=base_sources,
                        memory_use_policy=memory_use_policy,
                    )
                )[1],
            )
            compatibility = _provider_input_compatibility(
                prepared_call=prepared_call,
                canonical_facts=base_facts,
                sources=base_sources,
            )
            base_append = await self._io.run(
                _compile_structured_append,
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
                    texts=("", "", "")
                    if trigger_disposition == "ELIGIBLE"
                    else None,
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
                    self._memory_planning_reservations(
                        planning=planning,
                        prepared_preference=preference_source,
                        recall_desired=recall_desired,
                        compiled=base_append.compiled_input,
                        prepared_call=prepared_call,
                    )
                )
                final_sources = await self._apply_memory_sources(
                    sources,
                    activation_subject=activation_subject,
                    activation_text=activation_text,
                    include_recall=True,
                    frozen_preference=preference_source,
                    trigger_disposition=trigger_disposition,
                )
                final_memory = self._freeze_memory_call_context(
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
                append, final_sources = await self._compile_with_memory_fallback(
                    request=final_request,
                    planning=planning,
                    prepared_call=prepared_call,
                    canonical_facts=base_facts,
                    sources=final_sources,
                    preference_source=preference_source,
                    recall_reservation=recall_reservation,
                    preference_reservation=preference_reservation,
                    scope=scope,
                    memory_use_policy=memory_use_policy,
                    deadline=deadline,
                )
            memory_snapshot = self._freeze_memory_call_context(
                scope=scope,
                planning=planning,
                canonical_facts=base_facts,
                sources=final_sources,
                memory_use_policy=memory_use_policy,
            )
            return _PreparedProviderDispatch(
                handle=handle,
                canonical_facts=base_facts,
                planning=planning,
                prepared_call=prepared_call,
                surface_borrow=borrow,
                sources=final_sources,
                append_result=append,
                memory_context=memory_snapshot[0],
            )
        except BaseException:
            handle.close()
            if borrow is not None:
                borrow.close()
            raise

    async def _read_compile_snapshot(
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

    def _freeze_memory_call_context(
        self,
        *,
        scope: ProviderInputContinuityScope,
        planning: FrozenProviderInputAppendPlanningInput,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        sources: CollectedContextSources,
        memory_use_policy: MemoryUsePolicy,
    ) -> tuple[FrozenModelCallMemoryContext, tuple[tuple[str, str], ...]]:
        epoch_nonce = (
            planning.predecessor_view.epoch_nonce
            if planning.predecessor_view is not None
            else f"cold:{planning.planning_fingerprint}"
        )
        return self._memory_contexts.freeze_call(
            scope=scope,
            epoch_nonce=epoch_nonce,
            canonical_facts=canonical_facts,
            sources=sources.candidates,
            memory_use_policy=memory_use_policy,
        )

    @staticmethod
    def _memory_source_head(
        planning: FrozenProviderInputAppendPlanningInput,
        kind: ContextSourceKind,
    ) -> ProcessLocalSourceHead | None:
        predecessor = planning.predecessor_view
        if predecessor is None:
            return None
        return next(
            (item for item in predecessor.source_heads if item.source_kind is kind),
            None,
        )

    @staticmethod
    def _memory_source_presence(
        source: ContextSourceCandidate | ContextSourceAbsentFact,
    ) -> SourceObservationPresence:
        if isinstance(source, ContextSourceCandidate):
            return SourceObservationPresence.VALUE
        if source.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE:
            return SourceObservationPresence.UNAVAILABLE
        return SourceObservationPresence.CLEARED

    @staticmethod
    def _memory_source_occurrence_fingerprint(
        source: ContextSourceCandidate | ContextSourceAbsentFact,
    ) -> str:
        # Both Round 8 sources are SNAPSHOT_ON_CHANGE.  Keep the occurrence
        # derivation identical to the pure compiler without importing its
        # private implementation.
        if source.lifecycle is not ContextSourceLifecycle.SNAPSHOT_ON_CHANGE:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        return context_fingerprint(
            "pulsara:context-source-occurrence:v1",
            {
                "domain": source.domain_semantic_fingerprint,
                "lifecycle": source.lifecycle.value,
                "occurrence": None,
            },
        )

    def _memory_invalidation_reservation(
        self,
        *,
        source_kind: ContextSourceKind,
        prior: ProcessLocalSourceHead,
        desired: ContextSourceCandidate | ContextSourceAbsentFact,
        compiled: FrozenCompiledModelInput,
        prepared_call: PreparedKernelModelCall,
    ) -> MemorySourceInvalidationReservation:
        cleared = build_memory_context_source(
            kind=source_kind,
            texts=None,
            absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
        )
        unavailable = build_memory_context_source(
            kind=source_kind,
            texts=None,
            absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
        )
        assert isinstance(cleared, ContextSourceAbsentFact)
        assert isinstance(unavailable, ContextSourceAbsentFact)
        invalidation_messages = tuple(
            encode_runtime_observation(
                source_kind=source_kind,
                trust_class=item.trust_class,
                lifecycle=(
                    SourceObservationLifecycle.CLEARED
                    if item.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                    else SourceObservationLifecycle.UNAVAILABLE
                ),
                presence=(
                    SourceObservationPresence.CLEARED
                    if item.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                    else SourceObservationPresence.UNAVAILABLE
                ),
                contract_version=item.source_contract_version,
                body="",
            )
            for item in (cleared, unavailable)
        )
        estimator = prepared_call.compile_binding.estimator
        base_estimate = compiled.final_estimate
        token_deltas: list[int] = []
        byte_deltas: list[int] = []
        for message in invalidation_messages:
            estimate = estimator.estimate_frozen_input(
                system_prompt=compiled.system_prompt,
                messages=(*compiled.messages, message),
                tools=compiled.tools,
            )
            token_deltas.append(
                max(0, estimate.total_input_tokens - base_estimate.total_input_tokens)
            )
            byte_deltas.append(
                provider_input_logical_utf8_bytes(
                    system_prompt="",
                    tools=(),
                    messages=(message,),
                )
            )
        full_bytes = 0
        full_tokens = 0
        if isinstance(desired, ContextSourceCandidate):
            full_message = encode_runtime_observation(
                source_kind=source_kind,
                trust_class=desired.trust_class,
                lifecycle=SourceObservationLifecycle.SNAPSHOT,
                presence=SourceObservationPresence.VALUE,
                contract_version=desired.source_contract_version,
                body=desired.variants[0].text,
            )
            full_bytes = provider_input_logical_utf8_bytes(
                system_prompt="", tools=(), messages=(full_message,)
            )
            full_estimate = estimator.estimate_frozen_input(
                system_prompt=compiled.system_prompt,
                messages=(*compiled.messages, full_message),
                tools=compiled.tools,
            )
            full_tokens = max(
                0,
                full_estimate.total_input_tokens - base_estimate.total_input_tokens,
            )
        return build_memory_source_invalidation_reservation(
            source_kind=source_kind,
            prior_presence=prior.presence,
            prior_semantic_fingerprint=prior.semantic_fingerprint,
            desired_presence=self._memory_source_presence(desired),
            desired_semantic_fingerprint=(
                self._memory_source_occurrence_fingerprint(desired)
            ),
            source_contract_fingerprint=desired.source_contract_fingerprint,
            invalidation_encoded_utf8_bytes_ceiling=max(byte_deltas),
            invalidation_input_token_ceiling=max(token_deltas),
            invalidation_epoch_bytes_ceiling=max(byte_deltas),
            full_encoded_utf8_bytes=full_bytes,
            full_input_token_cost=full_tokens,
            estimator_fingerprint=(
                prepared_call.compile_binding.estimator.fact.estimator_fingerprint
            ),
        )

    def _memory_planning_reservations(
        self,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        prepared_preference: ContextSourceCandidate | ContextSourceAbsentFact,
        recall_desired: ContextSourceCandidate | ContextSourceAbsentFact,
        compiled: FrozenCompiledModelInput,
        prepared_call: PreparedKernelModelCall,
    ) -> tuple[
        MemorySourceInvalidationReservation | None,
        MemorySourceInvalidationReservation | None,
    ]:
        recall_prior = self._memory_source_head(
            planning, ContextSourceKind.MEMORY_RECALL
        )
        preference_prior = self._memory_source_head(
            planning, ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
        )
        recall = None
        if recall_prior is not None and recall_prior.presence in {
            SourceObservationPresence.VALUE,
            SourceObservationPresence.UNAVAILABLE,
        }:
            recall = self._memory_invalidation_reservation(
                source_kind=ContextSourceKind.MEMORY_RECALL,
                prior=recall_prior,
                desired=recall_desired,
                compiled=compiled,
                prepared_call=prepared_call,
            )
        preference = None
        desired_presence = self._memory_source_presence(prepared_preference)
        desired_semantic = self._memory_source_occurrence_fingerprint(
            prepared_preference
        )
        if (
            preference_prior is not None
            and preference_prior.presence is SourceObservationPresence.VALUE
            and (
                desired_presence is not SourceObservationPresence.VALUE
                or preference_prior.semantic_fingerprint != desired_semantic
            )
        ):
            preference = self._memory_invalidation_reservation(
                source_kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                prior=preference_prior,
                desired=prepared_preference,
                compiled=compiled,
                prepared_call=prepared_call,
            )
        return recall, preference

    async def _apply_memory_sources(
        self,
        sources: CollectedContextSources,
        *,
        activation_subject: CapabilityActivationSubjectKind | None,
        activation_text: str,
        include_recall: bool,
        frozen_preference: ContextSourceCandidate | ContextSourceAbsentFact | None = None,
        trigger_disposition: str | None = None,
    ) -> CollectedContextSources:
        if (
            self._memory_projection is None
            or activation_subject
            is not CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
        ):
            return sources
        disposition = trigger_disposition or str(
            self._memory_projection.classify_automatic_trigger(activation_text)
        )
        preference = frozen_preference or (
            await self._memory_projection.freeze_response_preference_source()
        )
        if disposition == "DISABLED_BY_EXPLICIT_USER_DIRECTIVE":
            preference = build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        replacements: list[ContextSourceCandidate | ContextSourceAbsentFact] = [
            preference
        ]
        if include_recall:
            if disposition in {
                "DISABLED_BY_EXPLICIT_USER_DIRECTIVE",
                "SKIPPED_LOW_INFORMATION",
            }:
                replacements.append(
                    build_memory_context_source(
                        kind=ContextSourceKind.MEMORY_RECALL,
                        texts=None,
                        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    )
                )
            else:
                replacements.append(
                    await self._memory_projection.freeze_automatic_recall_source(
                        activation_text
                    )
                )
        return replace_memory_context_sources(sources, tuple(replacements))

    async def _compile_with_memory_fallback(
        self,
        *,
        request: StructuredModelInputCompileRequest,
        planning: FrozenProviderInputAppendPlanningInput,
        prepared_call: PreparedKernelModelCall,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        sources: CollectedContextSources,
        preference_source: ContextSourceCandidate | ContextSourceAbsentFact | None,
        recall_reservation: MemorySourceInvalidationReservation | None,
        preference_reservation: MemorySourceInvalidationReservation | None,
        scope: ProviderInputContinuityScope,
        memory_use_policy: MemoryUsePolicy,
        deadline: float,
    ) -> tuple[FrozenProviderInputAppendCompileResult, CollectedContextSources]:
        """Materialize optional memory without invalidating an accepted steer.

        Preference FULL gets the first optional allocation.  Recall then uses
        the ordinary FULL/COMPACT/REF_ONLY compiler degradation.  If either
        optional VALUE cannot fit, only the already-quoted stale-state carrier
        may remain.  No queue row is reconsidered and no remote operation is
        retried here.
        """

        budget_failures = {
            ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
            ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
            ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
            ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
            ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
        }

        def request_for(
            selected_sources: CollectedContextSources,
        ) -> StructuredModelInputCompileRequest:
            memory = self._freeze_memory_call_context(
                scope=scope,
                planning=planning,
                canonical_facts=canonical_facts,
                sources=selected_sources,
                memory_use_policy=memory_use_policy,
            )
            return replace(
                request,
                sources=selected_sources,
                memory_citation_handles=memory[1],
            )

        async def compile_one(selected_sources: CollectedContextSources):
            selected_request = request_for(selected_sources)
            return await self._io.run(
                _compile_structured_append,
                self._compiler,
                selected_request,
                planning=planning,
                compatibility=_provider_input_compatibility(
                    prepared_call=prepared_call,
                    canonical_facts=canonical_facts,
                    sources=selected_sources,
                ),
                deadline_monotonic=deadline,
            )

        try:
            return await compile_one(sources), sources
        except StructuredModelInputCompileError as exc:
            if exc.kind not in budget_failures:
                raise

        def fallback_source(
            kind: ContextSourceKind,
            reservation: MemorySourceInvalidationReservation | None,
            desired: ContextSourceCandidate | ContextSourceAbsentFact | None,
        ) -> ContextSourceAbsentFact:
            absence = ContextSourceAbsenceKind.NOT_APPLICABLE
            if reservation is not None:
                if (
                    isinstance(desired, ContextSourceAbsentFact)
                    and desired.absence_kind
                    is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                ):
                    absence = ContextSourceAbsenceKind.EXPLICIT_EMPTY
                else:
                    absence = ContextSourceAbsenceKind.UNAVAILABLE
            value = build_memory_context_source(
                kind=kind,
                texts=None,
                absence_kind=absence,
            )
            assert isinstance(value, ContextSourceAbsentFact)
            return value

        recall_desired = next(
            (
                item
                for item in (*sources.candidates, *sources.absent_facts)
                if item.source_kind is ContextSourceKind.MEMORY_RECALL
            ),
            None,
        )
        without_recall = replace_memory_context_sources(
            sources,
            (
                fallback_source(
                    ContextSourceKind.MEMORY_RECALL,
                    recall_reservation,
                    recall_desired,
                ),
            ),
        )
        try:
            return await compile_one(without_recall), without_recall
        except StructuredModelInputCompileError as exc:
            if exc.kind not in budget_failures:
                raise

        without_optional_values = replace_memory_context_sources(
            without_recall,
            (
                fallback_source(
                    ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                    preference_reservation,
                    preference_source,
                ),
            ),
        )
        return await compile_one(without_optional_values), without_optional_values

    async def _prepare_memory_reflection(
        self,
        *,
        cut: PreparedProviderInputCut,
        through_sequence: int,
        permission: FrozenRunPermissionSnapshot,
        remember_requested: bool,
        memory_use_policy: MemoryUsePolicy,
    ) -> str | None:
        """Install one optional DORMANT handoff before the ROOT slot releases."""

        if (
            self._memory_projection is None
            or memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER
        ):
            return None
        post_turn_cut = PreparedProviderInputCut(
            session_id=cut.session_id,
            turn_id=cut.turn_id,
            context_binding_revision_id=cut.context_binding_revision_id,
            provider_input_through_sequence=through_sequence,
        )
        try:
            frozen = await self._read_compile_snapshot(
                post_turn_cut, deadline=self._canonical_deadline()
            )
            return self._memory_projection.prepare_and_adopt_reflection(
                canonical=frozen.canonical_input,
                permission=permission,
                remember_requested=remember_requested,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reflection is weaker than the already-committed reply.
            return None

    async def _hydrate_pending_steers(
        self,
        facts: tuple[PendingPromptSteerFact, ...],
        *,
        deadline: float,
    ) -> tuple[tuple[PendingPromptSteerFact, bytes], ...]:
        result: list[tuple[PendingPromptSteerFact, bytes]] = []
        used = 0
        for fact in facts:
            if used + fact.content.size > MAXIMUM_STEER_CANDIDATE_UTF8_BYTES:
                break
            if isinstance(fact.content, InlineContent):
                body = fact.content.canonical_bytes
            else:
                body = await self._io.run(
                    self._blob_store.read_exact,
                    blob_id=fact.content.blob_id,
                    expected_digest=fact.content.digest,
                    expected_size=fact.content.size,
                    deadline_monotonic=deadline,
                )
            try:
                body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                ) from exc
            used += len(body)
            result.append((fact, body))
        return tuple(result)

    async def _consume_prepared_steer_plan(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> tuple[AcceptedSteerDispatchEntry, ...]:
        task = asyncio.create_task(
            self._consume_prepared_steer_plan_worker(plan),
            name=(
                "kernel-steer-consumption:"
                f"{plan.selected_consumption_candidates[0].exact_target_turn_id}"
            ),
        )
        return await _await_started_settlement(task)

    async def _consume_prepared_steer_plan_worker(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> tuple[AcceptedSteerDispatchEntry, ...]:
        accepted: list[AcceptedSteerDispatchEntry] = []
        for candidate in plan.selected_consumption_candidates:
            while True:
                try:
                    deadline = self._canonical_deadline()
                    value = await self._io.run(
                        self._repository.consume_prepared_prompt_steer,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=deadline,
                    )
                    accepted.append(value)
                    break
                except ConversationKernelConflict:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if confirmation.kind is SteerConsumptionConfirmationKind.FULL:
                        assert confirmation.accepted is not None
                        accepted.append(confirmation.accepted)
                        break
                    if accepted:
                        await self._settle_post_consumption_plan_conflict(plan)
                        raise ConversationKernelConflict(
                            "prepared steer plan changed after partial consumption"
                        )
                    raise _PreparedSteerPlanStale(
                        "prepared steer plan changed before first consumption"
                    )
                except BaseException:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if confirmation.kind is SteerConsumptionConfirmationKind.FULL:
                        assert confirmation.accepted is not None
                        accepted.append(confirmation.accepted)
                        break
                    if (
                        confirmation.kind is SteerConsumptionConfirmationKind.NONE
                    ):
                        continue
                    if accepted:
                        await self._settle_post_consumption_plan_conflict(plan)
                    elif confirmation.kind is SteerConsumptionConfirmationKind.CONFLICT:
                        raise _PreparedSteerPlanStale(
                            "prepared steer plan changed before first consumption"
                        )
                    raise ConversationKernelConflict(
                        "prepared steer consumption could not be settled"
                    )
        return tuple(accepted)

    async def _settle_post_consumption_plan_conflict(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> None:
        first = plan.selected_consumption_candidates[0]
        candidate = build_steer_plan_conflict_interruption(
            session_id=first.session_id,
            exact_target_turn_id=first.exact_target_turn_id,
            source_plan_fingerprint=plan.plan_fingerprint,
            occurred_at=datetime.now(timezone.utc),
            actor_id=self._writer_lease.guard.writer_owner_id,
        )
        task = asyncio.create_task(
            self._settle_post_consumption_plan_conflict_worker(candidate),
            name=f"kernel-steer-plan-conflict:{candidate.exact_target_turn_id}",
        )
        await _await_started_settlement(task)

    async def _settle_post_consumption_plan_conflict_worker(
        self,
        candidate: PreparedSteerPlanConflictInterruption,
    ) -> None:
        while True:
            deadline = self._canonical_deadline()
            try:
                await self._io.run(
                    self._repository.interrupt_prepared_steer_plan_conflict,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=deadline,
                )
            except StaleHostWriter:
                # The acquiring generation atomically interrupts all prior
                # RUNNING turns before it can become the new writer.
                return
            except BaseException:
                pass
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_prepared_steer_plan_conflict,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind in {
                SteerPlanConflictConfirmationKind.FULL,
                SteerPlanConflictConfirmationKind.HISTORICAL_TERMINAL,
            }:
                return
            if confirmation.kind is SteerPlanConflictConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "steer plan-conflict interruption has a foreign winner"
                )
            await asyncio.sleep(0.05)

    async def _settle_steer_resource_rejection(
        self,
        candidate: PreparedSteerResourceRejection,
    ) -> None:
        task = asyncio.create_task(
            self._settle_steer_resource_rejection_worker(candidate),
            name=(f"kernel-steer-resource-rejection:{candidate.exact_target_turn_id}"),
        )
        await _await_started_settlement(task)

    async def _settle_steer_resource_rejection_worker(
        self,
        candidate: PreparedSteerResourceRejection,
    ) -> None:
        write_deadline = self._canonical_deadline()
        while True:
            try:
                await self._io.run(
                    self._repository.reject_prepared_prompt_steer_resource_exhaustion,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=write_deadline,
                )
                return
            except StaleHostWriter:
                # A replacement Host terminalizes the old RUNNING turn and
                # rejects its exact-target steer lane during writer takeover.
                return
            except BaseException:
                settlement_deadline = self._canonical_deadline()
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer_resource_rejection,
                        session_id=self._writer_lease.guard.session_id,
                        candidate=candidate,
                        deadline_monotonic=settlement_deadline,
                    )
                except BaseException:
                    await asyncio.sleep(0.05)
                    write_deadline = self._canonical_deadline()
                    continue
                if confirmation.kind is SteerResourceRejectionConfirmationKind.FULL:
                    return
                if confirmation.kind is SteerResourceRejectionConfirmationKind.NONE:
                    # The ordinary operation deadline may already be exhausted,
                    # but this admitted atomic rejection must still reach one
                    # exact winner (or writer takeover) before the ROOT slot can
                    # retire.  Use a fresh bounded physical attempt; do not split
                    # queue rejection and turn interruption into two writes.
                    write_deadline = self._canonical_deadline()
                    continue
                raise ConversationKernelConflict(
                    "steer resource rejection could not be settled"
                )

    async def run_accepted_turn(
        self,
        turn_id: str,
        *,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> KernelRunResult:
        """Execute a ROOT/task turn whose user entry is already canonical."""

        intent = cancellation_intent or ActiveTurnCancellationIntent(
            turn_id, ModelInputScopeKind.ROOT, None
        )
        intent.require_exact(
            turn_id=turn_id,
            scope_kind=intent.scope_kind,
            scope_subagent_task_id=intent.scope_subagent_task_id,
        )

        model_call_count = 0
        tool_call_count = 0
        remember_requested = False
        current_memory_use_policy = (
            self._root_memory_use_policy
            if intent.scope_kind is ModelInputScopeKind.ROOT
            else MemoryUsePolicy.ENABLED
        )
        active_surface_borrow: ProcessLocalToolSurfaceBorrow | None = None
        try:
            unsettled_process_local_effect: ProcessLocalEffectSettlementToken | None = (
                None
            )
            while True:
                model_call_count += 1
                planning_deadline = self._planning_deadline()
                steer_plan_retries = 0
                while True:
                    try:
                        dispatch = await self._prepare_provider_dispatch(
                            turn_id=turn_id,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            deadline=planning_deadline,
                        )
                        break
                    except _PreparedSteerPlanStale:
                        steer_plan_retries += 1
                        if (
                            steer_plan_retries >= 3
                            or monotonic() >= planning_deadline
                        ):
                            raise
                        await asyncio.sleep(0)
                prepared = dispatch.handle
                active_surface_borrow = dispatch.surface_borrow
                try:
                    canonical_facts = dispatch.canonical_facts
                    canonical_input = canonical_facts.canonical_input
                    identity = canonical_input.identity
                    planning = dispatch.planning
                    prepared_call = dispatch.prepared_call
                    sources = dispatch.sources
                    memory_context = dispatch.memory_context
                    current_memory_use_policy = memory_context.memory_use_policy
                    if identity.conversation_scope_kind is ModelInputScopeKind.ROOT:
                        self._root_memory_use_policy = current_memory_use_policy
                    if (
                        sources.registry_fingerprint
                        != self._context_source_collector.registry_fingerprint
                    ):
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                        )
                    compatibility = _provider_input_compatibility(
                        prepared_call=prepared_call,
                        canonical_facts=canonical_facts,
                        sources=sources,
                    )
                    append_result = dispatch.append_result
                    compiled_input = append_result.compiled_input
                    self._offer_compile_observation(
                        turn_id=turn_id,
                        model_call_index=model_call_count,
                        compiled=compiled_input,
                    )
                    append_candidate = _prepared_append_candidate(
                        planning=planning,
                        compatibility=compatibility,
                        compiled_result=append_result,
                    )
                    self._continuity.register(append_candidate)
                    request = KernelModelExecutionRequest(
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        model_call_index=model_call_count,
                        prepared_call=prepared_call,
                        compiled_input=compiled_input,
                        cut=prepared.cut,
                        surface_borrow=active_surface_borrow,
                        memory_context=memory_context,
                    )
                    execution: PreparedKernelModelExecution | None = None
                    installed = False
                    try:
                        try:
                            for tool in compiled_input.tools:
                                binding = active_surface_borrow.execution_binding(
                                    tool.name
                                )
                                if (
                                    binding.descriptor_fingerprint
                                    != tool.descriptor_fingerprint
                                ):
                                    raise RuntimeError("tool binding changed")
                        except Exception as exc:
                            raise StructuredModelInputCompileError(
                                ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                            ) from exc
                        try:
                            execution = self._model.preflight_execution(
                                request,
                                expected_append_candidate_fingerprint=(
                                    append_candidate.candidate_fingerprint
                                ),
                                install_authority=(
                                    self._continuity.install_authority
                                ),
                            )
                        except StructuredModelInputCompileError:
                            raise
                        except Exception as exc:
                            raise StructuredModelInputCompileError(
                                ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
                            ) from exc
                        prepared.begin_model_operation()
                        permit = self._continuity.install(
                            candidate_fingerprint=(
                                append_candidate.candidate_fingerprint
                            ),
                            execution_fingerprint=execution.execution_fingerprint,
                        )
                        installed = True
                        entry_id = _id("entry")
                        completed = await self._collect_model(
                            request,
                            execution=execution,
                            permit=permit,
                            proposed_entry_id=entry_id,
                        )
                    except BaseException:
                        if not installed:
                            if execution is not None:
                                try:
                                    execution.discard()
                                except RuntimeError:
                                    pass
                            self._continuity.discard(
                                append_candidate.candidate_fingerprint
                            )
                        raise
                    canonical_blocks = await self._canonical_blocks(completed)
                    calls = tuple(
                        item
                        for item in completed.blocks
                        if isinstance(item, CompletedToolCallBlock)
                    )
                    parent_bytes = json.dumps(
                        {
                            "draft_identity": completed.draft_identity,
                            "blocks": [
                                self._block_manifest(item) for item in canonical_blocks
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    parent_content = await self._content(
                        parent_bytes, deadline=self._canonical_deadline()
                    )
                    occurred_at = datetime.now(timezone.utc)
                    assistant_deadline = self._canonical_deadline()
                    try:
                        accepted = await self._io.run(
                            self._repository.commit_assistant_message,
                            self._writer_lease.guard,
                            cut=request.cut,
                            entry_id=entry_id,
                            parent_content=parent_content,
                            blocks=canonical_blocks,
                            complete_turn=not calls,
                            occurred_at=occurred_at,
                            actor_id="model:foreground",
                            deadline_monotonic=assistant_deadline,
                        )
                    except Exception:
                        winner = await self._io.run(
                            self._repository.confirm_assistant_message_winner,
                            self._writer_lease.guard,
                            cut=request.cut,
                            entry_id=entry_id,
                            parent_content=parent_content,
                            blocks=canonical_blocks,
                            complete_turn=not calls,
                            occurred_at=occurred_at,
                            actor_id="model:foreground",
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        if winner is None:
                            raise
                        accepted = winner
                    self._live_bus.offer_settlement_nowait(
                        kind=LiveSettlementKind.COMMITTED,
                        session_id=request.session_id,
                        turn_id=turn_id,
                        draft_identity=entry_id,
                        committed_entry_id=accepted.entry_id,
                        scope_kind=identity.conversation_scope_kind.value,
                        scope_subagent_task_id=identity.scope_subagent_task_id,
                        channel_kind=LiveChannelKind.MODEL_OUTPUT,
                        generation_id=f"model-output:{entry_id}",
                        proposed_entry_id=entry_id,
                    )
                finally:
                    prepared.close()
                if not calls and accepted.turn_completed:
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    reflection_token = await self._prepare_memory_reflection(
                        cut=request.cut,
                        through_sequence=accepted.entry_sequence,
                        permission=canonical_facts.run_permission_snapshot,
                        remember_requested=remember_requested,
                        memory_use_policy=current_memory_use_policy,
                    )
                    return KernelRunResult(
                        turn_id=turn_id,
                        final_entry_id=accepted.entry_id,
                        final_text=completed.public_text,
                        model_call_count=model_call_count,
                        tool_call_count=tool_call_count,
                        memory_reflection_tokens=(
                            () if reflection_token is None else (reflection_token,)
                        ),
                    )
                if not calls:
                    # A steer arrived after this provider call froze its cut.
                    # The assistant entry is valid and exactly attributed to
                    # the old cut, while the atomic commit kept the turn open.
                    # The next loop iteration consumes the steer before a new
                    # provider dispatch.
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    continue
                plan_call_indexes = tuple(
                    index
                    for index, call in enumerate(calls)
                    if call.tool_name
                    in {"enter_plan", "ask_plan_question", "exit_plan"}
                )
                if plan_call_indexes:
                    if identity.conversation_scope_kind is not ModelInputScopeKind.ROOT:
                        raise RuntimeError("Plan control escaped the ROOT tool surface")
                    if active_surface_borrow is None:
                        raise RuntimeError("Plan batch lost its tool surface borrow")
                    tool_call_count += len(calls)
                    outcome = await self._accept_plan_control_batch(
                        calls=calls,
                        selected_call_index=plan_call_indexes[0],
                        assistant_entry_id=accepted.entry_id,
                        canonical_facts=canonical_facts,
                        surface_borrow=active_surface_borrow,
                        deadline=self._canonical_deadline(),
                    )
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    if outcome.interaction_kind is PlanInteractionKind.QUESTION:
                        # The canonical answer/tool result is installed by the
                        # Host resolution command.  Human think time is not a
                        # provider/tool operation deadline.
                        continue
                    if not outcome.origin_turn_completed:
                        # Idempotent enter_plan against the already-active
                        # workflow settles the batch but keeps this exact run.
                        continue
                    return KernelRunResult(
                        turn_id=turn_id,
                        final_entry_id=(
                            outcome.selected_result_entry_id or accepted.entry_id
                        ),
                        final_text=completed.public_text,
                        model_call_count=model_call_count,
                        tool_call_count=tool_call_count,
                        continuation_turn_id=outcome.continuation_turn_id,
                        continuation_entry_id=outcome.continuation_entry_id,
                        pending_plan_interaction_id=outcome.interaction_id,
                    )
                for call in calls:
                    tool_call_count += 1
                    observation_origin = ToolObservationOrigin.POLICY
                    invocation_arguments = thaw_json(call.arguments)
                    if not isinstance(invocation_arguments, dict):
                        raise RuntimeError(
                            "canonical tool-call arguments did not thaw as an object"
                        )
                    result_entry_id = _id("entry")
                    result_id = _id("tool-result")
                    if active_surface_borrow is None:
                        raise RuntimeError(
                            "model response lost its tool surface borrow"
                        )
                    authorization = await self._tools.authorize(
                        tool_name=call.tool_name,
                        arguments=invocation_arguments,
                        tool_call_id=call.tool_call_id,
                        turn_id=turn_id,
                        assistant_entry_id=accepted.entry_id,
                        permission_snapshot=(canonical_facts.run_permission_snapshot),
                        surface_borrow=active_surface_borrow,
                        memory_context=request.memory_context,
                    )
                    machine_policy_kind = authorization.kind
                    capability_decision_id = _stable_id(
                        "capability-decision", accepted.entry_id, call.tool_call_id
                    )
                    if (
                        authorization.kind
                        is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
                    ):
                        await self._io.run(
                            self._repository.accept_tool_capability_decision,
                            self._writer_lease.guard,
                            decision_id=capability_decision_id,
                            assistant_entry_id=accepted.entry_id,
                            tool_call_id=call.tool_call_id,
                            decision="REQUIRE_CONFIRMATION",
                            authorization_reference=authorization.reference,
                            redacted_subject=f"tool:{call.tool_name}",
                            attempt_id=None,
                            result_id=None,
                            result_entry_id=None,
                            denial_content=None,
                            denial_result_state=None,
                            occurred_at=datetime.now(timezone.utc),
                            actor_id="tool-dispatch-policy",
                            permission_snapshot_fingerprint=(
                                canonical_facts.run_permission_snapshot.snapshot_fingerprint
                            ),
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        authorization = await self._tools.request_confirmation(
                            tool_name=call.tool_name,
                            tool_call_id=call.tool_call_id,
                            turn_id=turn_id,
                            assistant_entry_id=accepted.entry_id,
                            permission_snapshot=(
                                canonical_facts.run_permission_snapshot
                            ),
                        )
                    attempt_id: str | None = None
                    attempt_permission_snapshot_fingerprint: str | None = (
                        authorization.accepted_permission_snapshot_fingerprint
                    )
                    live_sink: _ToolResultLiveSink | None = None
                    live_attribution: dict[str, object] | None = None
                    tool_result_block_id: str | None = None
                    workspace_id: str | None = None
                    binding_fingerprint: str | None = None
                    if authorization.kind is KernelToolAuthorizationKind.ALLOW:
                        try:
                            advertised_binding = (
                                active_surface_borrow.execution_binding(call.tool_name)
                            )
                            binding_fingerprint = (
                                advertised_binding.executor_binding_fingerprint
                            )
                            observation_origin = tool_observation_origin_for_binding(
                                advertised_binding
                            )
                        except RuntimeError:
                            authorization = KernelToolAuthorization(
                                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                                "tool-surface:revoked",
                                f"tool unavailable: {call.tool_name}",
                            )
                    if authorization.kind is not KernelToolAuthorizationKind.ALLOW:
                        if authorization.accepted_result_entry_id is not None:
                            # Human DENY atomically committed the decision and
                            # no-attempt result.  It is already available to the
                            # next provider cut and must not be written twice.
                            continue
                        result = KernelToolResult(
                            state=authorization.kind.value,
                            content=(
                                authorization.public_message
                                or authorization.kind.value.lower().replace("_", " ")
                            ).encode("utf-8"),
                        )
                        if (
                            machine_policy_kind
                            is KernelToolAuthorizationKind.PERMISSION_DENIED
                        ):
                            denial_content = await self._content(
                                result.content, deadline=self._canonical_deadline()
                            )
                            await self._io.run(
                                self._repository.accept_tool_capability_decision,
                                self._writer_lease.guard,
                                decision_id=capability_decision_id,
                                assistant_entry_id=accepted.entry_id,
                                tool_call_id=call.tool_call_id,
                                decision="DENY",
                                authorization_reference=authorization.reference,
                                redacted_subject=f"tool:{call.tool_name}",
                                attempt_id=None,
                                result_id=_id("tool-result"),
                                result_entry_id=result_entry_id,
                                denial_content=denial_content,
                                denial_result_state="PERMISSION_DENIED",
                                occurred_at=datetime.now(timezone.utc),
                                actor_id="tool-dispatch-policy",
                                permission_snapshot_fingerprint=(
                                    canonical_facts.run_permission_snapshot.snapshot_fingerprint
                                ),
                                deadline_monotonic=self._canonical_deadline(),
                            )
                            continue
                    else:
                        assert binding_fingerprint is not None
                        attempt_id = authorization.accepted_attempt_id or _id(
                            "tool-attempt"
                        )
                        # The adapter is not reachable until both the complete
                        # tool-request message and this attempt transaction return.
                        if authorization.accepted_attempt_id is None:
                            if (
                                active_surface_borrow.binding_fingerprint(
                                    call.tool_name
                                )
                                != binding_fingerprint
                            ):
                                raise RuntimeError(
                                    "tool binding drifted before attempt acceptance"
                                )
                            accepted_decision = await self._io.run(
                                self._repository.accept_tool_capability_decision,
                                self._writer_lease.guard,
                                decision_id=capability_decision_id,
                                attempt_id=attempt_id,
                                assistant_entry_id=accepted.entry_id,
                                tool_call_id=call.tool_call_id,
                                authorization_reference=authorization.reference,
                                decision="ALLOW",
                                redacted_subject=f"tool:{call.tool_name}",
                                result_id=None,
                                result_entry_id=None,
                                denial_content=None,
                                denial_result_state=None,
                                occurred_at=datetime.now(timezone.utc),
                                actor_id="tool-dispatch-policy",
                                permission_snapshot_fingerprint=(
                                    canonical_facts.run_permission_snapshot.snapshot_fingerprint
                                ),
                                deadline_monotonic=self._canonical_deadline(),
                            )
                            if accepted_decision.attempt_id != attempt_id:
                                raise RuntimeError(
                                    "accepted tool attempt identity drifted"
                                )
                            attempt_permission_snapshot_fingerprint = (
                                accepted_decision.permission_snapshot_fingerprint
                            )
                        if (
                            attempt_permission_snapshot_fingerprint
                            != canonical_facts.run_permission_snapshot.snapshot_fingerprint
                        ):
                            raise RuntimeError(
                                "accepted tool attempt permission drifted before invoke"
                            )
                        tool_result_generation = f"tool-result:{result_entry_id}"
                        tool_result_block_id = _stable_id(
                            "tool-result-block", result_entry_id, call.tool_call_id
                        )
                        live_attribution = {
                            "scope_kind": identity.conversation_scope_kind.value,
                            "scope_subagent_task_id": identity.scope_subagent_task_id,
                            "channel_kind": LiveChannelKind.TOOL_RESULT,
                            "channel_tool_call_id": call.tool_call_id,
                            "channel_attempt_id": attempt_id,
                            "generation_id": tool_result_generation,
                            "proposed_entry_id": result_entry_id,
                        }
                        self._live_bus.offer_nowait(
                            event_type=LiveEventType.TOOL_RESULT_START,
                            session_id=request.session_id,
                            turn_id=turn_id,
                            draft_identity=result_entry_id,
                            payload=ToolResultStartPayload(
                                tool_result_block_id,
                                call.tool_call_id,
                                attempt_id,
                            ),
                            block_id=tool_result_block_id,
                            block_ordinal=0,
                            block_kind=LiveBlockKind.TOOL_RESULT,
                            **live_attribution,
                        )
                        terminal_streaming = call.tool_name == "terminal" or (
                            call.tool_name == "terminal_process"
                            and invocation_arguments.get("action") == "wait"
                        )
                        live_sink = (
                            _ToolResultLiveSink(
                                live_bus=self._live_bus,
                                session_id=request.session_id,
                                turn_id=turn_id,
                                draft_identity=result_entry_id,
                                block_identity=tool_result_block_id,
                                attribution=live_attribution,
                            )
                            if terminal_streaming
                            else None
                        )
                        workspace_id = await self._resolved_workspace_id()
                        invocation_context = KernelToolInvocationContext(
                            session_id=request.session_id,
                            workspace_id=workspace_id,
                            turn_id=turn_id,
                            assistant_entry_id=accepted.entry_id,
                            tool_call_id=call.tool_call_id,
                            attempt_id=attempt_id,
                            result_entry_id=result_entry_id,
                            conversation_scope_kind=(
                                identity.conversation_scope_kind.value
                            ),
                            scope_subagent_task_id=identity.scope_subagent_task_id,
                            host_owner_epoch=(
                                self._writer_lease.guard.writer_generation
                            ),
                            authorization_reference=authorization.reference,
                            permission_snapshot_fingerprint=(
                                canonical_facts.run_permission_snapshot.snapshot_fingerprint
                            ),
                            attempt_permission_snapshot_fingerprint=(
                                attempt_permission_snapshot_fingerprint
                            ),
                            tool_surface_fingerprint=(
                                active_surface_borrow.prepared.model_surface.surface_fingerprint
                            ),
                            executor_binding_fingerprint=binding_fingerprint,
                            surface_borrow=active_surface_borrow,
                            memory_context=request.memory_context,
                        )
                        try:
                            result = await self._tools.invoke(
                                tool_name=call.tool_name,
                                arguments=invocation_arguments,
                                tool_call_id=call.tool_call_id,
                                attempt_id=attempt_id,
                                turn_id=turn_id,
                                assistant_entry_id=accepted.entry_id,
                                invocation_context=invocation_context,
                                live_sink=live_sink,
                            )
                        except asyncio.CancelledError:
                            if live_sink is not None:
                                await asyncio.shield(live_sink.close())
                            assert live_attribution is not None
                            self._live_bus.offer_settlement_nowait(
                                kind=LiveSettlementKind.ABORTED,
                                session_id=request.session_id,
                                turn_id=turn_id,
                                draft_identity=result_entry_id,
                                reason_code="TOOL_RESULT_CANCELLED",
                                **live_attribution,
                            )
                            raise
                        except KernelToolPhysicalInvocationError as exc:
                            self._offer_operational_best_effort(
                                OperationalHookOffer(
                                    event_type=(
                                        OperationalHookType.TOOL_INVOCATION_OBSERVED
                                    ),
                                    session_id=request.session_id,
                                    turn_id=turn_id,
                                    public_payload={
                                        "tool_name": call.tool_name,
                                        "effect_class": exc.effect_class,
                                        "physical_timing": exc.timing,
                                        "outcome": "RAISED",
                                    },
                                )
                            )
                            if exc.effect_class not in {
                                "read_only",
                                "TERMINAL_OBSERVATION",
                            }:
                                if live_sink is not None:
                                    await asyncio.shield(live_sink.close())
                                assert live_attribution is not None
                                self._live_bus.offer_settlement_nowait(
                                    kind=LiveSettlementKind.ABORTED,
                                    session_id=request.session_id,
                                    turn_id=turn_id,
                                    draft_identity=result_entry_id,
                                    reason_code="TOOL_EFFECT_OUTCOME_UNKNOWN",
                                    **live_attribution,
                                )
                                raise
                            result = KernelToolResult(
                                state="SYSTEM_ERROR",
                                content=(
                                    "tool observation failed: "
                                    f"{type(exc.physical_error).__name__}"
                                ).encode("utf-8"),
                                physical_timing=exc.timing,
                                caller_cancelled_while_running=exc.caller_cancelled,
                                physical_observation=exc.physical_observation,
                            )
                        except Exception as exc:
                            severity = builtin_tool_catalog_entry(
                                call.tool_name
                            ).recovery_contract.severity
                            if severity != "read_only":
                                if live_sink is not None:
                                    await asyncio.shield(live_sink.close())
                                raise
                            result = KernelToolResult(
                                state="SYSTEM_ERROR",
                                content=(
                                    f"tool admission failed: {type(exc).__name__}"
                                ).encode("utf-8"),
                            )

                    # Once a physical call has returned an exact outcome, this
                    # process-local task owns every remaining settlement step.
                    # Cancelling the turn detaches only its waiter; it cannot
                    # erase the result or race a Terminal monitor token discard.
                    unsettled_process_local_effect = result.process_local_settlement
                    if (
                        result.physical_observation is not None
                        and result.physical_observation.observation_origin_kind
                        is not observation_origin
                    ):
                        raise RuntimeError(
                            "physical tool observation origin drifted from binding"
                        )
                    outcome_observed_at = (
                        result.physical_observation.observed_at
                        if result.physical_observation is not None
                        else datetime.now(timezone.utc)
                    )
                    if workspace_id is None:
                        workspace_id = await self._resolved_workspace_id()
                    settlement_task = asyncio.create_task(
                        self._settle_known_tool_result(
                            session_id=request.session_id,
                            turn_id=turn_id,
                            assistant_entry_id=accepted.entry_id,
                            tool_name=call.tool_name,
                            tool_call_id=call.tool_call_id,
                            invocation_arguments=invocation_arguments,
                            result_id=result_id,
                            result_entry_id=result_entry_id,
                            attempt_id=attempt_id,
                            workspace_id=workspace_id,
                            result=result,
                            observed_at=outcome_observed_at,
                            observation_origin=observation_origin,
                            live_sink=live_sink,
                            tool_result_block_id=tool_result_block_id,
                            live_attribution=live_attribution,
                            continuity_scope=planning.scope,
                            memory_citation_visibility=(
                                MemoryCitationVisibility(
                                    binding.memory_citation_visibility
                                )
                            ),
                            memory_citation_evidence_kind=(
                                MemoryCitationEvidenceKind.MEMORY_READ_EXPOSURE
                                if call.tool_name == "artifact_read"
                                and result.model_visible_memory_fact_ids
                                else MemoryCitationEvidenceKind(
                                    binding.memory_citation_evidence_kind
                                )
                            ),
                            execution_binding_fingerprint=(
                                binding.executor_binding_fingerprint
                            ),
                        ),
                        name=f"kernel-tool-result-settlement:{result_entry_id}",
                    )
                    settlement, cancellation = await _await_tool_result_settlement(
                        settlement_task
                    )
                    if settlement.process_local_effect_committed:
                        unsettled_process_local_effect = None
                    if result.memory_candidate is not None:
                        remember_requested = True
                    if cancellation is not None:
                        raise cancellation
                    if result.caller_cancelled_while_running:
                        # Preserve the unique known result first, then honor
                        # the user/Host cancellation by interrupting the turn.
                        raise asyncio.CancelledError
                active_surface_borrow.close()
                active_surface_borrow = None
        except BaseException as error:
            if active_surface_borrow is not None:
                active_surface_borrow.close()
                active_surface_borrow = None
            if unsettled_process_local_effect is not None:
                try:
                    await asyncio.shield(
                        self._tools.settle_process_local_effect(
                            unsettled_process_local_effect,
                            ProcessLocalEffectSettlementDisposition.DISCARDED,
                        )
                    )
                except BaseException:
                    pass
            if self._extensions is not None:
                if isinstance(error, StructuredModelInputCompileError):
                    self._offer_operational_best_effort(
                        OperationalHookOffer(
                            event_type=(
                                OperationalHookType.MODEL_INPUT_COMPILE_OBSERVED
                            ),
                            session_id=self._writer_lease.guard.session_id,
                            turn_id=turn_id,
                            public_payload={
                                "disposition": "FAILED",
                                "model_call_index": model_call_count,
                                "failure_kind": error.kind.value,
                            },
                        )
                    )
                if isinstance(error, CanonicalProviderContinuityError):
                    self._offer_operational_best_effort(
                        OperationalHookOffer(
                            event_type=OperationalHookType.PROVIDER_CONTINUITY_FAILED,
                            session_id=self._writer_lease.guard.session_id,
                            turn_id=turn_id,
                            public_payload={"failure_kind": error.kind.value},
                        )
                    )
                self._offer_operational_best_effort(
                    OperationalHookOffer(
                        event_type=OperationalHookType.FOREGROUND_TURN_FAILED,
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        public_payload={
                            "failure_code": "FOREGROUND_EXECUTION_INTERRUPTED"
                        },
                    )
                )
            cause = intent.cause
            if intent.scope_kind is ModelInputScopeKind.SUBAGENT_TASK and cause is not None:
                # The child manager owns the atomic turn+task settlement.
                raise
            reason = (
                _root_cancellation_terminal_reason(intent)
                if isinstance(error, asyncio.CancelledError) and cause is not None
                else "FOREGROUND_EXECUTION_INTERRUPTED"
            )
            await self._settle_failed_turn(turn_id, reason=reason)
            raise

    async def _settle_failed_turn(self, turn_id: str, *, reason: str) -> None:
        task = asyncio.create_task(
            self._settle_failed_turn_worker(turn_id, reason),
            name=f"kernel-turn-terminalization:{turn_id}",
        )
        await _await_started_settlement(task)

    async def _settle_failed_turn_worker(self, turn_id: str, reason: str) -> None:
        while True:
            deadline = self._canonical_deadline()
            try:
                changed = await self._io.run(
                    self._repository.interrupt_turn,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="foreground-runner",
                    deadline_monotonic=deadline,
                )
                if changed:
                    return
            except StaleHostWriter:
                return
            except BaseException:
                pass
            try:
                outcome = await self._io.run(
                    self._repository.read_turn_terminal_outcome,
                    session_id=self._writer_lease.guard.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if outcome is None:
                return
            status = str(outcome["status"])
            if status == TurnStatus.INTERRUPTED.value:
                # A different exact interruption is the canonical winner; do
                # not overwrite it with a later process-local diagnosis.
                return
            if status == TurnStatus.COMPLETED.value:
                return
            if status != TurnStatus.RUNNING.value:
                raise ConversationKernelConflict(
                    "turn terminal outcome has an invalid status"
                )
            await asyncio.sleep(0.05)

    async def _accept_plan_control_batch(
        self,
        *,
        calls: tuple[CompletedToolCallBlock, ...],
        selected_call_index: int,
        assistant_entry_id: str,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        deadline: float,
    ) -> AcceptedPlanToolBatch:
        selected = calls[selected_call_index]
        kind = {
            "enter_plan": PlanToolControlKind.ENTER,
            "ask_plan_question": PlanToolControlKind.QUESTION,
            "exit_plan": PlanToolControlKind.DRAFT,
        }[selected.tool_name]
        workflow_fact = canonical_facts.plan_workflow_fact
        if kind is PlanToolControlKind.ENTER:
            if workflow_fact is None:
                workflow_id = _stable_id(
                    "plan-workflow",
                    self._writer_lease.guard.session_id,
                    assistant_entry_id,
                    selected.tool_call_id,
                )
                expected_revision = None
            else:
                workflow_id = workflow_fact.workflow_id
                expected_revision = workflow_fact.current_workflow_revision
        else:
            workflow_id = (
                workflow_fact.workflow_id
                if workflow_fact is not None
                else _stable_id(
                    "plan-unavailable-workflow",
                    self._writer_lease.guard.session_id,
                    assistant_entry_id,
                    selected.tool_call_id,
                )
            )
            expected_revision = (
                None
                if workflow_fact is None
                else workflow_fact.current_workflow_revision
            )
        provisional_interaction_id = (
            None
            if kind is PlanToolControlKind.ENTER
            else _stable_id(
                "plan-interaction",
                workflow_id,
                assistant_entry_id,
                selected.tool_call_id,
            )
        )
        catalog_entry = builtin_tool_catalog_entry(selected.tool_name)
        catalog_binding = catalog_entry.binding_contract.base
        request_binding = PlanInteractionBinding(
            catalog_binding.contract_id,
            catalog_binding.contract_version,
            catalog_binding.binding_fingerprint,
        )
        disposition = PlanToolBatchDisposition.APPLY
        # A Plan call owns the complete batch even when its frozen surface was
        # revoked or its arguments are invalid.  Classify those conditions
        # before constructing any workflow/interaction subject so the
        # repository can install one closed no-attempt result for every call.
        try:
            advertised_execution_binding = surface_borrow.execution_binding(
                selected.tool_name
            )
            advertised_spec = next(
                (
                    item
                    for item in surface_borrow.prepared.model_surface.tool_specs
                    if item.name == selected.tool_name
                ),
                None,
            )
            if (
                advertised_spec is None
                or advertised_execution_binding.descriptor_fingerprint
                != advertised_spec.descriptor_fingerprint
                or advertised_spec.descriptor_fingerprint
                != catalog_entry.descriptor.fingerprint()
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        except (KeyError, RuntimeError):
            disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        if disposition is PlanToolBatchDisposition.APPLY:
            schema_source = catalog_entry.descriptor.input_schema
            if schema_source is None:
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            else:
                schema = thaw_tool_json_object(schema_source)
                try:
                    validator = validators.validator_for(schema)
                    validator.check_schema(schema)
                except Exception:
                    disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
                else:
                    try:
                        raw_arguments = thaw_json(selected.arguments)
                        if not isinstance(raw_arguments, dict):
                            raise ValidationError("arguments must be an object")
                        validator(schema).validate(raw_arguments)
                    except ValidationError:
                        disposition = PlanToolBatchDisposition.INVALID_ARGUMENTS
        if disposition is PlanToolBatchDisposition.APPLY:
            try:
                if kind is PlanToolControlKind.ENTER:
                    extract_plan_entry_reason(
                        binding=request_binding,
                        arguments=selected.arguments,
                    )
                elif kind is PlanToolControlKind.QUESTION:
                    assert provisional_interaction_id is not None
                    extract_plan_question(
                        interaction_id=provisional_interaction_id,
                        binding=request_binding,
                        arguments=selected.arguments,
                    )
                else:
                    assert provisional_interaction_id is not None
                    extract_plan_draft(
                        interaction_id=provisional_interaction_id,
                        assistant_entry_id=assistant_entry_id,
                        tool_call_id=selected.tool_call_id,
                        binding=request_binding,
                        request_semantic_digest=_json_digest(selected.arguments),
                        arguments=selected.arguments,
                    )
            except ValueError:
                disposition = PlanToolBatchDisposition.INVALID_ARGUMENTS
        if disposition is PlanToolBatchDisposition.APPLY:
            if kind is not PlanToolControlKind.ENTER and workflow_fact is None:
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            elif (
                kind is PlanToolControlKind.QUESTION and self._plan_interactions is None
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            elif (
                kind is PlanToolControlKind.ENTER
                and workflow_fact is None
                and self._automatic_plan_continuation is None
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        apply_control = disposition is PlanToolBatchDisposition.APPLY
        interaction_id = provisional_interaction_id if apply_control else None
        continuation_turn_id = (
            _stable_id("plan-continuation-turn", workflow_id, selected.tool_call_id)
            if apply_control
            and kind is PlanToolControlKind.ENTER
            and workflow_fact is None
            else None
        )
        continuation_entry_id = (
            _stable_id("plan-continuation-entry", workflow_id, selected.tool_call_id)
            if apply_control
            and kind is PlanToolControlKind.ENTER
            and workflow_fact is None
            else None
        )
        continuation_revision_id = (
            _stable_id("context-revision", continuation_turn_id or "", "0")
            if continuation_turn_id is not None
            else None
        )
        prepared_calls: list[PreparedPlanBatchCall] = []
        for index, call in enumerate(calls):
            selected_question = (
                apply_control
                and index == selected_call_index
                and kind is PlanToolControlKind.QUESTION
            )
            prepared_calls.append(
                PreparedPlanBatchCall(
                    block_id=call.block_id,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    result_id=(
                        None
                        if selected_question
                        else _stable_id(
                            "tool-result", assistant_entry_id, call.tool_call_id
                        )
                    ),
                    result_entry_id=(
                        None
                        if selected_question
                        else _stable_id(
                            "tool-result-entry", assistant_entry_id, call.tool_call_id
                        )
                    ),
                )
            )
        candidate = PreparedPlanToolBatch(
            session_id=self._writer_lease.guard.session_id,
            workspace_id=await self._resolved_workspace_id(),
            origin_turn_id=canonical_facts.canonical_input.identity.turn_id,
            assistant_entry_id=assistant_entry_id,
            selected_call_ordinal=selected_call_index,
            control_kind=kind,
            selected_arguments=selected.arguments,
            request_binding=request_binding,
            permission_snapshot=canonical_facts.run_permission_snapshot,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_revision,
            interaction_id=interaction_id,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
            calls=tuple(prepared_calls),
            occurred_at=datetime.now(timezone.utc),
            actor_id="plan-runtime",
            idempotent_existing=(
                apply_control
                and kind is PlanToolControlKind.ENTER
                and workflow_fact is not None
            ),
            selected_disposition=disposition,
        )
        waiter: PlanQuestionWaiter | None = None
        if apply_control and kind is PlanToolControlKind.QUESTION:
            assert self._plan_interactions is not None
            assert interaction_id is not None
            waiter = await self._plan_interactions.prepare_question(
                interaction_id=interaction_id,
                origin_turn_id=candidate.origin_turn_id,
            )
        try:
            if (
                apply_control
                and kind is PlanToolControlKind.ENTER
                and not candidate.idempotent_existing
            ):
                assert self._automatic_plan_continuation is not None
                # The Host callback installs its own continuation task before
                # its first await.  Calling it in the ROOT run-chain task is
                # essential: an extra shield-created wrapper would become the
                # observed origin task and could never exact-join Host's ROOT
                # slot.  The callback itself shields the installed owner.
                outcome = await self._automatic_plan_continuation(candidate, deadline)
            else:
                try:
                    outcome = await self._io.run(
                        self._repository.accept_plan_tool_batch,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    outcome = await self._io.run(
                        self._repository.confirm_plan_tool_batch_winner,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if outcome is None:
                        raise
            if waiter is not None:
                if outcome.question is None:
                    raise RuntimeError("accepted Plan question lacks typed content")
                await self._plan_interactions.publish_open(waiter, outcome.question)
                await self._plan_interactions.wait(waiter)
            return outcome
        except BaseException as error:
            if waiter is not None and self._plan_interactions is not None:
                await self._plan_interactions.abandon(waiter, error)
            raise

    async def accept_subagent_result(
        self,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        child_result_id: str,
        command_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        return await self._io.run(
            self._safe_point.accept_subagent_result,
            turn_id=turn_id,
            new_context_binding_revision_id=new_context_binding_revision_id,
            child_result_id=child_result_id,
            command_id=command_id,
            actor_id=actor_id,
            deadline_monotonic=deadline_monotonic,
        )

    async def accept_job_result(
        self,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        job_id: str,
        command_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        return await self._io.run(
            self._safe_point.accept_job_result,
            turn_id=turn_id,
            new_context_binding_revision_id=new_context_binding_revision_id,
            job_id=job_id,
            command_id=command_id,
            actor_id=actor_id,
            deadline_monotonic=deadline_monotonic,
        )

    async def install_terminal_observation(
        self,
        *,
        coordinator: TerminalMonitorCoordinator,
        monitor_id: str,
        target: PreparedInstallationTarget,
        workspace_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        return await self._io.run(
            self._safe_point.install_terminal_observation,
            coordinator=coordinator,
            monitor_id=monitor_id,
            target=target,
            workspace_id=workspace_id,
            actor_id=actor_id,
            deadline_monotonic=deadline_monotonic,
        )

    async def _collect_model(
        self,
        request: KernelModelExecutionRequest,
        *,
        execution: PreparedKernelModelExecution,
        permit: ProcessLocalProviderInputInstallPermit,
        proposed_entry_id: str,
    ) -> CompletedAssistantMessage:
        assembler = ProviderStreamAssembler(
            session_id=request.session_id,
            turn_id=request.turn_id,
            live_bus=self._live_bus,
            proposed_entry_id=proposed_entry_id,
            conversation_scope_kind=(
                request.compiled_input.canonical_input_identity.conversation_scope_kind.value
            ),
            scope_subagent_task_id=(
                request.compiled_input.canonical_input_identity.scope_subagent_task_id
            ),
        )
        try:
            async for item in execution.open_once(permit):
                assembler.apply(item)
            return assembler.complete()
        except BaseException as exc:
            self._live_bus.offer_settlement_nowait(
                kind=LiveSettlementKind.ABORTED,
                session_id=request.session_id,
                turn_id=request.turn_id,
                draft_identity=proposed_entry_id,
                reason_code=f"MODEL_STREAM_{type(exc).__name__.upper()}",
                scope_kind=(
                    request.compiled_input.canonical_input_identity.conversation_scope_kind.value
                ),
                scope_subagent_task_id=(
                    request.compiled_input.canonical_input_identity.scope_subagent_task_id
                ),
                channel_kind=LiveChannelKind.MODEL_OUTPUT,
                generation_id=f"model-output:{proposed_entry_id}",
                proposed_entry_id=proposed_entry_id,
            )
            raise

    def _offer_compile_observation(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        compiled: FrozenCompiledModelInput,
    ) -> None:
        if self._extensions is None:
            return
        try:
            projection = project_model_input_compile_observation(
                model_call_index=model_call_index,
                compiled=compiled,
            )
            self._extensions.offer_operational_nowait(
                OperationalHookOffer(
                    event_type=OperationalHookType.MODEL_INPUT_COMPILE_OBSERVED,
                    session_id=self._writer_lease.guard.session_id,
                    turn_id=turn_id,
                    public_payload=projection.public_payload(),
                )
            )
        except Exception:
            # Operational observation is intentionally best-effort.  Its owner
            # cannot veto a compiled provider call or canonical transition.
            return

    def _offer_operational_best_effort(self, offer: OperationalHookOffer) -> None:
        if self._extensions is None:
            return
        try:
            self._extensions.offer_operational_nowait(offer)
        except Exception:
            return

    async def _canonical_blocks(
        self,
        completed: CompletedAssistantMessage,
    ) -> tuple[AssistantBlock, ...]:
        result: list[AssistantBlock] = []
        for block in completed.blocks:
            if isinstance(block, CompletedTextBlock):
                result.append(
                    AssistantTextBlock(
                        block_id=block.block_id,
                        text=await self._content(
                            block.text.encode("utf-8"),
                            deadline=self._canonical_deadline(),
                        ),
                    )
                )
            elif isinstance(block, CompletedDataBlock):
                result.append(
                    AssistantDataBlock(
                        block_id=block.block_id,
                        data=await self._content(
                            block.data.encode("utf-8"),
                            media_type=block.media_type,
                            deadline=self._canonical_deadline(),
                        ),
                    )
                )
            elif isinstance(block, CompletedToolCallBlock):
                result.append(
                    AssistantToolCallBlock(
                        block_id=block.block_id,
                        tool_call_id=block.tool_call_id,
                        tool_name=block.tool_name,
                        arguments=block.arguments,
                    )
                )
        if not result:
            result.append(
                AssistantTextBlock(
                    block_id=_id("block"),
                    text=await self._content(
                        b"", deadline=self._canonical_deadline()
                    ),
                )
            )
        return tuple(result)

    @staticmethod
    def _block_manifest(block: AssistantBlock) -> Mapping[str, object]:
        if isinstance(block, AssistantTextBlock):
            return {
                "kind": "TEXT",
                "digest": block.text.digest,
                "size": block.text.size,
            }
        if isinstance(block, AssistantDataBlock):
            return {
                "kind": "DATA",
                "digest": block.data.digest,
                "size": block.data.size,
                "media_type": block.data.media_type,
            }
        assert isinstance(block, AssistantToolCallBlock)
        return {
            "kind": "TOOL_CALL",
            "tool_call_id": block.tool_call_id,
            "tool_name": block.tool_name,
            "arguments_digest": _json_digest(block.arguments),
        }

    async def _content(
        self,
        value: bytes,
        *,
        deadline: float,
        media_type: str = "text/plain",
        codec: str = "utf-8",
    ) -> CanonicalContent:
        return await self._io.run(
            self._content_publisher.materialize,
            session_id=self._writer_lease.guard.session_id,
            content=value,
            media_type=media_type,
            codec=codec,
            deadline_monotonic=deadline,
        )

    async def _resolved_workspace_id(self) -> str:
        if self._workspace_id is None:
            self._workspace_id = await self._io.run(
                self._repository.read_session_workspace_id,
                self._writer_lease.guard,
                deadline_monotonic=self._canonical_deadline(),
            )
        return self._workspace_id

    async def _settle_known_tool_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_entry_id: str,
        tool_name: str,
        tool_call_id: str,
        invocation_arguments: Mapping[str, object],
        result_id: str,
        result_entry_id: str,
        attempt_id: str | None,
        workspace_id: str,
        result: KernelToolResult,
        observed_at: datetime,
        observation_origin: ToolObservationOrigin,
        live_sink: _ToolResultLiveSink | None,
        tool_result_block_id: str | None,
        live_attribution: Mapping[str, object] | None,
        continuity_scope: ProviderInputContinuityScope,
        memory_citation_visibility: MemoryCitationVisibility,
        memory_citation_evidence_kind: MemoryCitationEvidenceKind,
        execution_binding_fingerprint: str,
    ) -> _KnownToolResultSettlementOutcome:
        if live_sink is not None:
            await asyncio.shield(live_sink.close())
        if attempt_id is not None and result.remote_identity is not None:
            remote_identity_candidate = (
                build_prepared_tool_remote_identity_publication(
                    session_id=session_id,
                    attempt_id=attempt_id,
                    remote_identity=result.remote_identity,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id=tool_name,
                )
            )
            await self._publish_tool_remote_identity_exact(
                remote_identity_candidate
            )
        prepared_output = await self._io.run(
            self._tool_output_processor.prepare,
            workspace_id=workspace_id,
            result_entry_id=result_entry_id,
            public_output=result.content.decode("utf-8"),
            candidate=result.output_artifact_candidate,
            artifact_source_read=result.artifact_source_read,
            deadline_monotonic=self._canonical_deadline(),
        )
        result_text = prepared_output.canonical_preview.canonical_bytes.decode("utf-8")
        if attempt_id is not None:
            if tool_result_block_id is None or live_attribution is None:
                raise RuntimeError("physical tool settlement lost live attribution")
            if result_text and (live_sink is None or not live_sink.emitted):
                self._live_bus.offer_nowait(
                    event_type=LiveEventType.TOOL_RESULT_DELTA,
                    session_id=session_id,
                    turn_id=turn_id,
                    draft_identity=result_entry_id,
                    payload=ToolResultDeltaPayload(tool_result_block_id, result_text),
                    block_id=tool_result_block_id,
                    block_ordinal=0,
                    block_kind=LiveBlockKind.TOOL_RESULT,
                    **live_attribution,
                )
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TOOL_RESULT_END,
                session_id=session_id,
                turn_id=turn_id,
                draft_identity=result_entry_id,
                payload=ToolResultEndPayload(
                    tool_result_block_id,
                    result.state,
                    result_text,
                    len(result_text.encode("utf-8")),
                    live_digest(result_text),
                ),
                block_id=tool_result_block_id,
                block_ordinal=0,
                block_kind=LiveBlockKind.TOOL_RESULT,
                **live_attribution,
            )
        prepared_acceptance = build_prepared_tool_result_acceptance(
            guard=self._writer_lease.guard,
            workspace_id=workspace_id,
            result_id=result_id,
            result_entry_id=result_entry_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_state=result.state,
            canonical_preview_content=prepared_output.canonical_preview,
            artifact_disposition=prepared_output.artifact_disposition,
            artifact_id=prepared_output.artifact_id,
            artifact_blob_descriptor=prepared_output.artifact_blob,
            source_coverage=prepared_output.source_coverage,
            display_kind=prepared_output.display_kind,
            source_coverage_reason=prepared_output.source_coverage_reason,
            artifact_unavailability_reason=(
                prepared_output.artifact_unavailability_reason
            ),
            observed_at=observed_at,
            observation_duration_microseconds=(
                None
                if result.physical_observation is None
                else result.physical_observation.elapsed_microseconds
            ),
            observation_origin_kind=observation_origin,
            trusted_tool_reported_duration_microseconds=(
                None
                if result.trusted_observation is None
                else result.trusted_observation.duration_microseconds
            ),
            actor_id=tool_name,
            memory_candidate=result.memory_candidate,
            model_visible_memory_fact_ids=result.model_visible_memory_fact_ids,
        )
        try:
            accepted = await self._accept_tool_result_exact(prepared_acceptance)
        except StaleHostWriter:
            # A process-local exact return is never handed to a replacement
            # Host.  The durable attempt remains result-less and readers derive
            # the unknown outcome.
            self._offer_operational_best_effort(
                OperationalHookOffer(
                    event_type=OperationalHookType.TOOL_INVOCATION_OBSERVED,
                    session_id=session_id,
                    turn_id=turn_id,
                    public_payload={
                        "tool_name": tool_name,
                        "effect_class": _tool_effect_class(
                            tool_name, invocation_arguments, result.effect_class
                        ),
                        "physical_timing": result.physical_timing,
                        "outcome": "EXACT_RETURN_STALE_WRITER",
                    },
                )
            )
            raise
        if result.memory_candidate is not None and self._memory_projection is not None:
            self._memory_projection.offer_candidate_wake(
                result.memory_candidate.candidate_id
            )
        epoch = self._continuity.current_view(continuity_scope)
        if epoch is None:
            raise RuntimeError("accepted ToolResult lost its provider-input epoch")
        self._memory_contexts.register_result(
            scope=continuity_scope,
            epoch_nonce=epoch.epoch_nonce,
            result_id=result_id,
            result_entry_sequence=accepted.entry_sequence,
            visibility=memory_citation_visibility,
            evidence_kind=memory_citation_evidence_kind,
            execution_binding_fingerprint=execution_binding_fingerprint,
        )
        effect_committed = False
        if result.process_local_settlement is not None:
            await self._tools.settle_process_local_effect(
                result.process_local_settlement,
                ProcessLocalEffectSettlementDisposition.COMMITTED,
            )
            effect_committed = True
        if attempt_id is not None:
            assert live_attribution is not None
            self._live_bus.offer_settlement_nowait(
                kind=LiveSettlementKind.COMMITTED,
                session_id=session_id,
                turn_id=turn_id,
                draft_identity=result_entry_id,
                committed_entry_id=accepted.entry_id,
                **live_attribution,
            )
        if result.physical_timing != "ON_TIME":
            self._offer_operational_best_effort(
                OperationalHookOffer(
                    event_type=OperationalHookType.TOOL_INVOCATION_OBSERVED,
                    session_id=session_id,
                    turn_id=turn_id,
                    public_payload={
                        "tool_name": tool_name,
                        "effect_class": _tool_effect_class(
                            tool_name, invocation_arguments, result.effect_class
                        ),
                        "physical_timing": result.physical_timing,
                        "outcome": "RETURNED_EXACT",
                    },
                )
            )
        return _KnownToolResultSettlementOutcome(
            accepted=accepted,
            process_local_effect_committed=effect_committed,
        )

    async def _publish_tool_remote_identity_exact(
        self,
        candidate: PreparedToolRemoteIdentityPublication,
    ) -> None:
        """Settle one immutable remote identity without losing a known result."""

        while True:
            try:
                await self._io.run(
                    self._repository.publish_tool_remote_identity,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                return
            except StaleHostWriter:
                raise
            except BaseException:
                pass
            while True:
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_tool_remote_identity,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except StaleHostWriter:
                    raise
                except BaseException:
                    await asyncio.sleep(0.05)
            if confirmation is ToolRemoteIdentityConfirmationKind.FULL:
                return
            if confirmation is ToolRemoteIdentityConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "tool remote identity has a conflicting winner"
                )
            # NONE: retry the exact prepared candidate.  The physical tool has
            # already returned and is never invoked by this settlement loop.

    async def _accept_tool_result_exact(
        self,
        candidate: PreparedToolResultAcceptance,
    ) -> AcceptedEntry:
        try:
            return await self._io.run(
                self._repository.accept_tool_result,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
        except Exception:
            winner = await self._io.run(
                self._repository.confirm_tool_result_winner,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
            if winner is not None:
                return winner
        # The first write was proven absent.  Reissue only the exact frozen
        # canonical candidate; the physical tool is never invoked again.
        try:
            return await self._io.run(
                self._repository.accept_tool_result,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
        except Exception:
            winner = await self._io.run(
                self._repository.confirm_tool_result_winner,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
            if winner is None:
                raise
            return winner


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _root_cancellation_terminal_reason(
    intent: ActiveTurnCancellationIntent | None,
) -> str:
    cause = None if intent is None else intent.cause
    if cause is ForegroundCancellationCause.USER_REQUEST:
        return "USER_STOPPED"
    if cause is ForegroundCancellationCause.HOST_SESSION_CLOSE:
        return "SESSION_CLOSED"
    return "FOREGROUND_EXECUTION_INTERRUPTED"


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _json_digest(value: FrozenJsonObjectFact) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


def _tool_effect_class(
    tool_name: str,
    arguments: Mapping[str, object],
    result_effect_class: str | None = None,
) -> str:
    if result_effect_class is not None:
        return result_effect_class
    if tool_name == "terminal_process":
        action = arguments.get("action")
        if action in {"list", "log", "poll", "wait"}:
            return "TERMINAL_OBSERVATION"
        if action in {"write", "submit", "close_stdin", "kill"}:
            return "TERMINAL_EFFECT"
        raise RuntimeError("terminal_process action escaped its closed catalog")
    if tool_name == "terminal":
        return "TERMINAL_EFFECT"
    return builtin_tool_catalog_entry(tool_name).recovery_contract.severity


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


def _compile_structured_append(
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


def _canonical_frontier(
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
        "tool_observation_freshness_fact": (
            base.tool_observation_freshness_fact
        ),
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


def _provider_input_compatibility(
    *,
    prepared_call: PreparedKernelModelCall,
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
        provider_message_lowering_contract=PROVIDER_MESSAGE_LOWERING_CONTRACT,
        context_base_semantic_identity=(
            canonical_facts.context_binding_fact.context_base_semantic_identity
        ),
    )


def _prepared_append_candidate(
    *,
    planning: FrozenProviderInputAppendPlanningInput,
    compatibility: ProviderInputEpochCompatibility,
    compiled_result: FrozenProviderInputAppendCompileResult,
) -> PreparedProviderInputAppendCandidate:
    predecessor = planning.predecessor_view
    epoch_nonce = (
        f"provider-input-epoch:{uuid4().hex}"
        if predecessor is None or compiled_result.reset_reason is not None
        else predecessor.epoch_nonce
    )
    expected_revision = 0 if predecessor is None else predecessor.epoch_revision
    predecessor_fingerprint = (
        None if predecessor is None else predecessor.semantic_prefix_fingerprint
    )
    candidate_fingerprint = prepared_provider_input_append_candidate_fingerprint(
        scope=planning.scope,
        epoch_nonce=epoch_nonce,
        expected_epoch_revision=expected_revision,
        predecessor_prefix_fingerprint=predecessor_fingerprint,
        dispatch_anchor=planning.dispatch_anchor,
        resulting_compiled_input=compiled_result.compiled_input,
        resulting_canonical_frontier=compiled_result.canonical_frontier,
        resulting_source_heads=compiled_result.source_heads,
        appended_message_count=compiled_result.appended_message_count,
        reset_reason=compiled_result.reset_reason,
        compatibility=compatibility,
        planning_fingerprint=planning.planning_fingerprint,
    )
    return PreparedProviderInputAppendCandidate(
        scope=planning.scope,
        epoch_nonce=epoch_nonce,
        expected_epoch_revision=expected_revision,
        predecessor_prefix_fingerprint=predecessor_fingerprint,
        dispatch_anchor=planning.dispatch_anchor,
        resulting_compiled_input=compiled_result.compiled_input,
        resulting_canonical_frontier=compiled_result.canonical_frontier,
        resulting_source_heads=compiled_result.source_heads,
        appended_message_count=compiled_result.appended_message_count,
        reset_reason=compiled_result.reset_reason,
        compatibility=compatibility,
        planning_fingerprint=planning.planning_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
    )


async def _await_started_settlement(
    task: asyncio.Task[_T],
) -> _T:
    """Keep one admitted canonical settlement attached through cancellation.

    Once a steer mutation worker exists, cancelling the API/runner waiter may
    not leave that worker detached behind a ROOT slot that is about to retire.
    asyncio cancellation is therefore only observed after the exact worker has
    reached a terminal state.  A settlement error wins over the detached
    caller cancellation so the normal turn-terminalization path can fail
    closed with the real conflict.
    """

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
        except BaseException:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _await_turn_admission_settlement(
    task: asyncio.Task[AcceptedEntry | None],
    attempt: _TurnAdmissionSettlementAttempt,
) -> tuple[AcceptedEntry | None, asyncio.CancelledError | None]:
    """Join one admission owner and turn waiter cancellation into no-reissue."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            attempt.cancellation_requested = True
            cancellation = exc
            continue
        except BaseException:
            break
    return task.result(), cancellation


async def _await_tool_result_settlement(
    task: asyncio.Task[_KnownToolResultSettlementOutcome],
) -> tuple[_KnownToolResultSettlementOutcome, asyncio.CancelledError | None]:
    """Join a known-result settlement while retaining caller cancellation.

    Unlike the generic helper above, the caller must first observe whether the
    process-local effect token was committed so its outer failure cleanup cannot
    incorrectly discard that token after the canonical ToolResult won.
    """

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
        except BaseException:
            break
    return task.result(), cancellation


__all__ = [
    "ConversationKernelRunner",
    "KernelModelPort",
    "KernelRunResult",
    "KernelToolPort",
    "KernelToolAuthorization",
    "KernelToolAuthorizationKind",
    "KernelToolPhysicalInvocationError",
    "KernelToolResult",
]
