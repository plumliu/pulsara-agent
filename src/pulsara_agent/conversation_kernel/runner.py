"""Fresh Stage 2 foreground conversation runner.

The runner owns only one live Host activation.  It never resumes a provider,
coroutine, interaction, terminal process, or subagent execution after a crash.
"""

from __future__ import annotations


from pulsara_agent.identity import derive_id

from pulsara_agent.identity import random_id

import asyncio
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Awaitable, Callable, Protocol


from pulsara_agent.conversation_kernel.assembler import (
    CompletedAssistantMessage,
    CompletedDataBlock,
    CompletedTextBlock,
    CompletedToolCallBlock,
    ProviderStreamAssembler,
)
from pulsara_agent.conversation_kernel.assistant_settlement import (
    AssistantMessageSettlementOwner,
    PreparedAssistantMessageSettlement,
)
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence,
    assistant_display_body,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceCollectorPort,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    FrozenCanonicalPrompt,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    CompactionOutcome,
    CompactionTrigger,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    AutomaticCompactionTriggerCandidate,
    CompactionAttemptToken as CompactionAttemptToken,
    CompactionCoordinator,
    CompactionExecutionResult,
    ModelSwitchCompactionTriggerCandidate,
    ModelSwitchDirectPrecompileDecision,
    OrdinaryPrecompileDecision,
    PreparedCompactSessionStart as PreparedCompactSessionStart,
    PreparedCompactSessionStartFacts as PreparedCompactSessionStartFacts,
    SessionStartCompactBoundaryPort,
    SessionStartCompactPort,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    KernelColdEpochInputAssembler,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
)
from pulsara_agent.conversation_kernel.direct_model import (
    AcceptedProviderFinalSource,
    CompletedProviderModelExecution,
    KernelModelExecutionRequest,
    PreparedKernelModelCall,
    PreparedKernelModelTarget,
    PreparedKernelModelExecution,
    KernelProviderModelExecutionFailed,
)
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.llm.input import (
    LLMMessage,
    LLMToolCall,
)
from pulsara_agent.llm.request import (
    FrozenProviderWireInputQuote,
    provider_assistant_public_projection_fingerprint,
)
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderOutputIncompleteReason,
    ProviderGenerationRetry,
    ProviderRetryReason,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    _issue_root_bootstrap_lease_source,
)
from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    WriterLease,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveChannelKind,
    LiveSettlementKind,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.turn_admission import (
    RootWorkAdmissionFinalizer,
    TurnAdmissionCoordinator,
    root_cancellation_terminal_reason,
)
from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.conversation_kernel.extensions import (
    KernelExtensionHost,
    OperationalHookOffer,
    OperationalHookType,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.tool_outputs import (
    ToolOutputProcessor,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolInvocationPort,
    ToolSurfacePlanningPort,
)
from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.conversation_kernel.repository import (
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    ConversationKernelConflict,
    StaleHostWriter,
    build_prepared_root_turn_intent,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.conversation_kernel.plan_runtime import (
    AutomaticPlanContinuationPort,
    KernelPlanInteractionCoordinator,
    PlanToolBatchCoordinator,
)
from pulsara_agent.conversation_kernel.memory.dispatch import (
    MemoryObservationProjectionPort,
    MemoryDispatchSupport,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    KernelModelPort,
    PreparedProviderDispatch,
    PreparedProspectiveActiveRootInput,
    PreparedWireMeasurementDecision,
    ProviderDispatchCoordinator,
)
from pulsara_agent.conversation_kernel.steer_consumption import (
    PreparedSteerPlanStale,
)
from pulsara_agent.conversation_kernel.repository_errors import (
    PreparedCompletionSuffixStale,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedActiveRootInputCandidate,
    StartedTurnInputGroup,
    build_direct_root_turn_identity,
)
from pulsara_agent.primitives.plan_workflow import (
    PlanInteractionKind,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    ProviderSafePointCoordinator,
)
from pulsara_agent.model_input.compiler import (
    StructuredModelInputCompiler,
)
from pulsara_agent.model_input.diagnostics import (
    project_model_input_compile_observation,
)
from pulsara_agent.model_input.contracts import (
    FrozenCompiledModelInput,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import (
    RuntimeObservationItem,
    ProcessLocalProviderInputInstallPermit,
)

from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE, PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    thaw_json,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.hooks.context import (
    HookContextOwner,
    PendingHookContextReservation,
)
from pulsara_agent.hooks.contracts import (
    ContinuationDecision,
    ContinuationOutcome as ContinuationOutcome,
    GateDecision,
    GateOutcome as GateOutcome,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    SessionStartInput,
    SessionStartRef,
    SubagentStartInput as SubagentStartInput,
    StopInput,
    SubagentStopInput as SubagentStopInput,
    StopRef,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject as event_matcher_subject

from pulsara_agent.conversation_kernel.runner_hooks import (
    _SessionStartAttemptToken as _SessionStartAttemptToken,
    _PendingSessionStartBoundary as _PendingSessionStartBoundary,
    _SessionStartColdBoundaryOwner as _SessionStartColdBoundaryOwner,
    SessionStartBlocked as SessionStartBlocked,
    CompactionContinuationBlocked as CompactionContinuationBlocked,
    _dispatch_session_start_hooks as _dispatch_session_start_hooks,
    _dispatch_session_stop_hooks as _dispatch_session_stop_hooks,
    _RootSessionStartCompactPort as _RootSessionStartCompactPort,
)


def _is_http413_failure(error: ProviderModelExecutionFailed) -> bool:
    summary = error.error.retry_summary
    return summary is not None and summary.final_status_code == 413


def _http413_execution_closed_without_output(
    error: ProviderModelExecutionFailed,
    execution: PreparedKernelModelExecution,
) -> bool:
    summary = error.error.retry_summary
    if summary is None or summary.has_semantic_output:
        return False
    # An owned SDK client can fail to close after an HTTP rejection. The
    # normalized generator may have exited, but the adapter's close fact still
    # forbids replay. These are existing structured decision codes.
    close_failures = {"physical_close_unconfirmed", "completed_stream_close_failed"}
    return (
        execution.state == "PHYSICALLY_CLOSED"
        and summary.final_reason not in close_failures
        and summary.skipped_reason not in close_failures
    )


def _http413_recovery_failed(
    error: ProviderModelExecutionFailed, detail: str,
) -> KernelProviderModelExecutionFailed:
    original_detail = (
        error.public_detail
        if isinstance(error, KernelProviderModelExecutionFailed)
        else f"{error.error.code.value}: {error.error.message}"
    )
    return KernelProviderModelExecutionFailed(
        error.error,
        "服务端拒绝了过大的请求体，压缩恢复未完成。 "
        + detail + "\n" + original_detail,
    )


def _require_http413_retry_shrink(
    failed_quote: FrozenProviderWireInputQuote | None,
    failed_error: ProviderModelExecutionFailed | None,
    actual_quote: FrozenProviderWireInputQuote,
) -> None:
    if failed_quote is not None and (
        actual_quote.final_wire_utf8_bytes >= failed_quote.final_wire_utf8_bytes
    ):
        assert failed_error is not None
        raise _http413_recovery_failed(
            failed_error, "最终请求没有减少实际请求字节。",
        )


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
    final_source: AcceptedProviderFinalSource | None = None






@dataclass(frozen=True, slots=True)
class _CollectedModelResponse:
    completed: CompletedAssistantMessage
    provider_completion: CompletedProviderModelExecution
    provider_replay: PreparedDurableProviderAssistantReplay | None = dataclass_field(
        default=None, repr=False
    )










class _RunnerToolCompositionPort(ToolSurfacePlanningPort, ToolInvocationPort, Protocol):
    """Require one Tool owner to satisfy the runner's two narrow consumers."""

    async def prepare_visualizations(
        self,
        body: str,
        *,
        io: KernelSessionIO,
        workspace_id: str,
        deadline_monotonic: float,
    ) -> tuple[FrozenVisualizationOccurrence, ...]: ...




class ConversationKernelRunner:
    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        model: KernelModelPort,
        tools: _RunnerToolCompositionPort,
        live_bus: LiveAgentEventBus,
        input_reader: CanonicalProviderInputReader | None = None,
        safe_point: ProviderSafePointCoordinator | None = None,
        before_provider_preparation: Callable[[], Awaitable[bool]] | None = None,
        workspace_gate: WorkspaceExecutionGate | None = None,
        take_user_actions: Callable[[], Awaitable[tuple[RuntimeObservationItem, ...] | None]] | None = None,
        content_publisher: CanonicalContentPublisher | None = None,
        io_owner: KernelSessionIO | None = None,
        context_source_collector: ContextSourceCollectorPort,
        model_resolution_snapshot_provider: (
            Callable[[], FrozenModelResolutionSnapshot] | None
        ) = None,
        compiler: StructuredModelInputCompiler | None = None,
        continuity_owner: HostProviderInputContinuityOwner | None = None,
        extensions: KernelExtensionHost | None = None,
        workspace_id: str | None = None,
        tool_output_processor: ToolOutputProcessor | None = None,
        plan_interactions: KernelPlanInteractionCoordinator | None = None,
        automatic_plan_continuation: AutomaticPlanContinuationPort | None = None,
        launch_permission_mode: PermissionMode = DEFAULT_PERMISSION_MODE,
        maximum_input_tokens_per_call: int | None = None,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        memory_projection: MemoryObservationProjectionPort | None = None,
        assistant_settlement_owner: AssistantMessageSettlementOwner | None = None,
        root_work_admission_finalizer: RootWorkAdmissionFinalizer | None = None,
        compaction_owner: HostCompactionRuntimeOwner | None = None,
        root_session_id: str | None = None,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_source: str = "startup",
        presentation_notice_sink: Callable[[str], None] | None = None,
    ) -> None:
        if (
            maximum_input_tokens_per_call is not None
            and maximum_input_tokens_per_call < 1
        ):
            raise ValueError("runner limits must be finite and positive")
        self._repository = repository
        self._session_scope_kind = (
            ModelInputScopeKind.ROOT
            if root_session_id is None
            else ModelInputScopeKind.SUBAGENT
        )
        self._writer_lease = writer_lease
        self._live_bus = live_bus
        self._active_execution: PreparedKernelModelExecution | None = None
        self._tools = tools
        self._model_resolution_snapshot_provider = model_resolution_snapshot_provider
        resolved_input_reader = input_reader or CanonicalProviderInputReader(
            repository.connection_provider,
            blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
        )
        blob_store = PostgresCanonicalBlobStore(repository.connection_provider)
        self._workspace_gate = workspace_gate
        self._before_provider_preparation = before_provider_preparation
        self._safe_point = safe_point or ProviderSafePointCoordinator(
            repository=repository,
            guard=writer_lease.guard,
        )
        self._content_publisher = content_publisher or CanonicalContentPublisher(
            repository.connection_provider
        )
        self._tool_output_processor = (
            tool_output_processor
            or ToolOutputProcessor()
        )
        self._launch_permission_mode = launch_permission_mode
        self._io = io_owner or KernelSessionIO()
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._workspace_resolver = SessionWorkspaceResolver(
            repository=repository,
            writer_lease=writer_lease,
            io_owner=self._io,
            workspace_id=workspace_id,
        )
        self._plan_batches = PlanToolBatchCoordinator(
            repository=repository,
            writer_lease=writer_lease,
            io_owner=self._io,
            interactions=plan_interactions,
            automatic_continuation=automatic_plan_continuation,
            workspace_resolver=self._workspace_resolver,
            deadline_factory=self._deadlines,
            hook_dispatcher=hook_dispatcher,
            hook_context_owner=hook_context_owner,
            hook_scope=hook_scope,
        )
        resolved_compiler = compiler or StructuredModelInputCompiler()
        cold_epoch_assembler = KernelColdEpochInputAssembler(resolved_compiler)
        capability_planner = KernelToolCapabilityPlanner()
        self._continuity = continuity_owner or HostProviderInputContinuityOwner(
            root_lease_source=_issue_root_bootstrap_lease_source(writer_lease)
        )
        self._assistant_settlements = (
            assistant_settlement_owner
            or AssistantMessageSettlementOwner(
                repository=repository,
                io_owner=self._io,
                continuity_owner=self._continuity,
                deadline_factory=self._deadlines,
            )
        )
        self._extensions = extensions
        self._memory_dispatch = MemoryDispatchSupport(
            compiler=resolved_compiler,
            io_owner=self._io,
            memory_projection=memory_projection,
            input_reader=resolved_input_reader,
            deadline_factory=self._deadlines,
        )
        self._root_memory_use_policy = MemoryUsePolicy.ENABLED
        self._turn_admission = TurnAdmissionCoordinator(
            workspace_gate=workspace_gate,
            repository=repository,
            io_owner=self._io,
            writer_lease=writer_lease,
            deadline_factory=self._deadlines,
            root_work_finalizer=root_work_admission_finalizer,
        )
        self._root_session_id = root_session_id
        self._context_source_collector = context_source_collector
        self._hook_dispatcher = hook_dispatcher
        self._hook_context_owner = hook_context_owner
        self._hook_scope = hook_scope
        self._presentation_notice_sink = presentation_notice_sink
        self._session_start_boundary = _SessionStartColdBoundaryOwner(
            session_start_source
        )
        self._provider_dispatch = ProviderDispatchCoordinator(
            repository=repository,
            writer_lease=writer_lease,
            model=model,
            tools=tools,
            input_reader=resolved_input_reader,
            blob_store=blob_store,
            safe_point=self._safe_point,
            io_owner=self._io,
            context_source_collector=context_source_collector,
            compiler=resolved_compiler,
            cold_epoch_assembler=cold_epoch_assembler,
            capability_planner=capability_planner,
            continuity_owner=self._continuity,
            memory_support=self._memory_dispatch,
            deadline_factory=self._deadlines,
            maximum_input_tokens_per_call=maximum_input_tokens_per_call,
            workspace_resolver=self._workspace_resolver,
            compaction_owner=compaction_owner,
            take_user_actions=take_user_actions,
        )
        self.compaction = CompactionCoordinator(
            workspace_gate=workspace_gate,
            owner=compaction_owner,
            repository=repository,
            writer_lease=writer_lease,
            model=model,
            tools=tools,
            provider_dispatch=self._provider_dispatch,
            input_reader=resolved_input_reader,
            io_owner=self._io,
            compiler=resolved_compiler,
            continuity_owner=self._continuity,
            safe_point=self._safe_point,
            content_publisher=self._content_publisher,
            workspace_resolver=self._workspace_resolver,
            deadline_factory=self._deadlines,
            hook_dispatcher=hook_dispatcher,
            hook_root_scope=hook_scope,
            session_kind=self._session_scope_kind,
            presentation_notice_sink=self._presentation_notice_sink,
        )
        self._tool_batches = ToolBatchExecutor(
            workspace_gate=workspace_gate,
            repository=repository,
            writer_lease=writer_lease,
            tools=tools,
            live_bus=live_bus,
            io_owner=self._io,
            content_publisher=self._content_publisher,
            tool_output_processor=self._tool_output_processor,
            continuity_owner=self._continuity,
            memory_projection=memory_projection,
            extensions=extensions,
            workspace_resolver=self._workspace_resolver,
            deadline_factory=self._deadlines,
            hook_dispatcher=hook_dispatcher,
            hook_context_owner=hook_context_owner,
        )

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _planning_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING)

    def _compact_session_start_port(
        self, intent: ActiveTurnCancellationIntent
    ) -> SessionStartCompactPort | None:
        return _RootSessionStartCompactPort(
            self._hook_dispatcher,
            self._hook_context_owner,
            self._hook_scope,
            self._session_start_boundary,
            intent,
            self._writer_lease.guard.session_id,
            (
                str(self._tools.snapshot_workspace_root())
                if self._hook_dispatcher is not None
                else ""
            ),
        )

    def _compact_session_start_boundary_port(
        self, intent: ActiveTurnCancellationIntent
    ) -> SessionStartCompactBoundaryPort | None:
        return self._session_start_boundary

    @staticmethod
    def _require_active_compaction_continuation(
        execution: CompactionExecutionResult,
    ) -> None:
        if execution.active_continuation_blocked_reason is not None:
            raise CompactionContinuationBlocked(
                execution.active_continuation_blocked_reason
            )
        if (
            execution.outcome.disposition is CompactionDisposition.COMPACTED
            and execution.successor_dispatch is None
        ):
            raise ConversationKernelConflict(
                "active compaction adopted without a runnable continuation"
            )

    async def _resolved_workspace_id(self) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline()
        )

    async def compact_idle_turn(
        self,
        *,
        turn_id: str,
        command_id: str,
        prepared_target: PreparedKernelModelTarget,
    ) -> CompactionOutcome:
        return await self.compaction.compact_idle_turn(
            turn_id=turn_id,
            command_id=command_id,
            prepared_target=prepared_target,
            session_start_boundary_port=self._session_start_boundary,
        )

    async def run_turn(
        self,
        canonical_prompt: FrozenCanonicalPrompt,
        *,
        command_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        expected_permission_snapshot: FrozenRunPermissionSnapshot | None = None,
        hook_context_reservation: PendingHookContextReservation | None = None,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None = None,
    ) -> KernelRunResult:
        return await self._run_turn(
            canonical_prompt,
            command_id=command_id,
            requested_permission_mode=(
                requested_permission_mode or self._launch_permission_mode
            ),
            expected_permission_snapshot=expected_permission_snapshot,
            hook_context_reservation=hook_context_reservation,
            cancellation_intent=cancellation_intent,
            model_resolution_snapshot=model_resolution_snapshot,
        )

    async def _run_turn(
        self,
        canonical_prompt: FrozenCanonicalPrompt,
        *,
        command_id: str | None,
        requested_permission_mode: PermissionMode | None,
        expected_permission_snapshot: FrozenRunPermissionSnapshot | None,
        hook_context_reservation: PendingHookContextReservation | None,
        cancellation_intent: ActiveTurnCancellationIntent | None,
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None,
    ) -> KernelRunResult:
        if not isinstance(canonical_prompt, FrozenCanonicalPrompt):
            raise TypeError("ROOT turn requires a frozen canonical prompt")
        stable_command_id = command_id or random_id("command")
        identity = build_direct_root_turn_identity(
            self._writer_lease.guard.session_id, stable_command_id
        )
        turn_id = identity.turn_id
        frozen_model_resolution = model_resolution_snapshot
        if frozen_model_resolution is None:
            provider = self._model_resolution_snapshot_provider
            if provider is None:
                raise RuntimeError(
                    "ROOT turn admission requires a frozen model resolution snapshot"
                )
            frozen_model_resolution = provider()
        occurred_at = datetime.now(timezone.utc)
        candidate = build_prepared_root_turn_intent(
            session_id=self._writer_lease.guard.session_id,
            command_id=stable_command_id,
            turn_id=turn_id,
            entry_id=identity.entry_id,
            context_binding_revision_id=identity.context_revision_id,
            permission_snapshot_id=identity.permission_snapshot_id,
            requested_permission_mode=(
                requested_permission_mode or self._launch_permission_mode
            ),
            canonical_prompt=canonical_prompt,
            occurred_at=occurred_at,
            expected_permission_snapshot=expected_permission_snapshot,
        )
        intent = cancellation_intent or ActiveTurnCancellationIntent(
            turn_id,
            ModelInputScopeKind.ROOT
            if self._root_session_id is None
            else ModelInputScopeKind.SUBAGENT,
        )
        intent.require_exact(
            turn_id=turn_id,
            scope_kind=self._session_scope_kind,
        )
        try:
            outcome = await self._turn_admission.accept_root_intent(
                candidate,
                model_resolution_snapshot=frozen_model_resolution,
                cancellation_intent=intent,
            )
            if hook_context_reservation is not None:
                hook_context_reservation.commit()
        except BaseException:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            raise
        try:
            return await self.run_accepted_turn(
                turn_id,
                cancellation_intent=intent,
                started_input_group=outcome.started_input_group,
            )
        except BaseException:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            raise

    async def prepare_plan_question_resolution_input(
        self,
        candidate: PreparedActiveRootInputCandidate,
        *,
        cancellation_intent: ActiveTurnCancellationIntent | None,
        admitted_writer: CompactionWriteReservation,
    ) -> PreparedProspectiveActiveRootInput:
        """Prepare the exact question result before its Plan writer runs."""

        return await self._prepare_active_root_input(
            candidate,
            deadline=self._planning_deadline(),
            cancellation_intent=cancellation_intent,
            admitted_writer=admitted_writer,
        )

    async def confirm_published_active_root_input(
        self,
        prepared: PreparedProspectiveActiveRootInput,
        *,
        publication_handle: PreparedProviderInputHandle,
        deadline_monotonic: float,
    ) -> None:
        await self._provider_dispatch.confirm_published_active_root_input(
            prepared,
            publication_handle=publication_handle,
            deadline=deadline_monotonic,
        )

    async def _prepare_active_root_input(
        self,
        candidate: PreparedActiveRootInputCandidate,
        *,
        deadline: float,
        cancellation_intent: ActiveTurnCancellationIntent | None,
        admitted_writer: CompactionWriteReservation,
    ) -> PreparedProspectiveActiveRootInput:
        """Prepare one active suffix, compacting before publication if needed."""

        try:
            return await self._provider_dispatch.prepare_prospective_active_root_input(
                candidate=candidate,
                inherited_memory_use_policy=self._root_memory_use_policy,
                deadline=deadline,
            )
        except StructuredModelInputCompileError as failure:
            if cancellation_intent is None:
                raise
            cut = candidate.expected_provider_input_cut
            cancellation_intent.require_exact(
                turn_id=cut.turn_id,
                scope_kind=self._session_scope_kind,
            )
            return await self.compaction.recover_pending_active_root_input(
                candidate=candidate,
                failure=failure,
                inherited_memory_use_policy=self._root_memory_use_policy,
                hook_scope=self._hook_scope,
                session_start_compact_port=(
                    self._compact_session_start_port(cancellation_intent)
                ),
                session_start_boundary_port=(
                    self._compact_session_start_boundary_port(cancellation_intent)
                ),
                admitted_writer=admitted_writer,
            )


    async def run_accepted_turn(
        self,
        turn_id: str,
        *,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
        started_input_group: StartedTurnInputGroup | None = None,
    ) -> KernelRunResult:
        """Execute a ROOT/task turn whose user entry is already canonical."""

        intent = cancellation_intent or ActiveTurnCancellationIntent(
            turn_id,
            ModelInputScopeKind.ROOT
            if self._root_session_id is None
            else ModelInputScopeKind.SUBAGENT,
        )
        intent.require_exact(
            turn_id=turn_id,
            scope_kind=self._session_scope_kind,
        )

        if started_input_group is not None and (
            started_input_group.turn_id != turn_id
            or started_input_group.session_id != self._writer_lease.guard.session_id
        ):
            raise ValueError("started input group belongs to another execution")
        first_call_history_through_sequence = (
            None if started_input_group is None
            else started_input_group.predecessor_through_sequence
        )
        model_call_count = 0
        tool_call_count = 0
        current_memory_use_policy = (
            self._root_memory_use_policy
            if intent.scope_kind is ModelInputScopeKind.ROOT
            else MemoryUsePolicy.ALL_DISABLED_BY_USER
        )
        active_surface_borrow: ProcessLocalToolSurfaceBorrow | None = None
        successor_dispatch: PreparedProviderDispatch | None = None
        successor_wire_decision: PreparedWireMeasurementDecision | None = None
        completed_tool_batch = False
        stop_continuation_used = False
        # One rejected business operation owns this qualification across
        # successor preparation, steer and transport-generation retries.
        http413_failed_wire_quote: FrozenProviderWireInputQuote | None = None
        http413_recovery_source: ProviderModelExecutionFailed | None = None
        try:
            await self._io.run(
                self._repository.require_provider_safe_turn,
                self._writer_lease.guard,
                turn_id=turn_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if intent.cause is not None:
                raise asyncio.CancelledError
            while True:
                if self._workspace_gate is not None:
                    await self._workspace_gate.wait_available()
                if (
                    successor_dispatch is None
                    and self._before_provider_preparation is not None
                ):
                    # No input/surface handle exists here. A transferred base
                    # keeps its frozen roots until the common suffix safe point.
                    await self._before_provider_preparation()
                if successor_dispatch is None:
                    # No handle or surface borrow exists yet. Network startup
                    # is outside the short snapshot/compilation watchdog.
                    await self._provider_dispatch.wait_for_cold_epoch_readiness(
                        scope_kind=self._session_scope_kind,
                    )
                if intent.cause is not None:
                    raise asyncio.CancelledError
                model_call_count += 1
                planning_deadline = self._planning_deadline()
                dispatch = successor_dispatch
                successor_dispatch = None
                automatic_compaction_decided = dispatch is not None
                reusable_wire_observation = None
                direct_switch_admission = None
                direct_switch_candidate = None
                wire_decision = successor_wire_decision
                successor_wire_decision = None
                if dispatch is not None:
                    unbound_dispatch = dispatch
                    dispatch = None
                    try:
                        if wire_decision is None:
                            wire_decision = await self.compaction.measure_dispatch_wire(
                                unbound_dispatch, deadline=planning_deadline,
                            )
                    except BaseException:
                        unbound_dispatch.close()
                        raise
                    # This API consumes the old owner even when it raises.
                    # Only its returned dispatch remains ours to retire.
                    dispatch, wire_decision = (
                        await self._provider_dispatch.finalize_compaction_input(
                            unbound_dispatch, wire_decision,
                            deadline=planning_deadline,
                            inherited_memory_use_policy=current_memory_use_policy,
                        )
                    )
                if dispatch is None:
                    headroom_admission = (
                        await self.compaction.prepare_precompile_admission(
                            turn_id=turn_id,
                            model_call_index=model_call_count,
                            deadline=planning_deadline,
                        )
                    )
                    try:
                        allow_model_switch = (
                            model_call_count == 1 and not completed_tool_batch
                        ) or self.compaction.context_allowance_change_pending(
                            scope_kind=intent.scope_kind,
                            destination=headroom_admission.prepared_target,
                        )
                        precompile_needed = self.compaction.precompile_needed(
                            scope_kind=intent.scope_kind,
                            allow_model_switch=allow_model_switch,
                        )
                    except BaseException:
                        headroom_admission.close()
                        raise
                    if precompile_needed:
                        auto_trigger = (
                            CompactionTrigger.MID_TURN_FOLLOWUP
                            if completed_tool_batch
                            else CompactionTrigger.AUTO_ACTIVE_CONTEXT
                        )
                        precompile = await self.compaction.prepare_precompile(
                            turn_id=turn_id,
                            first_call_history_through_sequence=first_call_history_through_sequence,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            trigger=auto_trigger,
                            scope_kind=intent.scope_kind,
                            headroom_admission=headroom_admission,
                            deadline=planning_deadline,
                            allow_model_switch=allow_model_switch,
                        )
                        if isinstance(
                            precompile,
                            (
                                OrdinaryPrecompileDecision,
                                ModelSwitchDirectPrecompileDecision,
                            ),
                        ):
                            headroom_admission = precompile.ordinary_admission
                            if isinstance(
                                precompile, ModelSwitchDirectPrecompileDecision
                            ):
                                direct_switch_candidate = precompile.switch_candidate
                                automatic_compaction_decided = True
                            else:
                                reusable_wire_observation = (
                                    precompile.reusable_wire_observation
                                )
                        else:
                            headroom_admission = None
                        if isinstance(
                            precompile,
                            AutomaticCompactionTriggerCandidate,
                        ):
                            automatic_compaction_decided = True
                            compaction = await self.compaction.execute_active(
                                turn_id=turn_id,
                                first_call_history_through_sequence=first_call_history_through_sequence,
                                model_call_index=model_call_count,
                                inherited_memory_use_policy=(current_memory_use_policy),
                                trigger=precompile.trigger,
                                force=False,
                                scope_kind=intent.scope_kind,
                                hook_scope=self._hook_scope,
                                session_start_compact_port=(
                                    self._compact_session_start_port(intent)
                                ),
                                session_start_boundary_port=(
                                    self._compact_session_start_boundary_port(intent)
                                ),
                            )
                            self._require_active_compaction_continuation(compaction)
                            if compaction.successor_dispatch is not None:
                                model_call_count -= 1
                                successor_dispatch = compaction.successor_dispatch
                                successor_wire_decision = compaction.successor_wire_decision
                                completed_tool_batch = False
                                continue
                        if isinstance(
                            precompile,
                            ModelSwitchCompactionTriggerCandidate,
                        ):
                            automatic_compaction_decided = True
                            compaction = (
                                await self.compaction.execute_model_switch_active(
                                    turn_id=turn_id,
                                    first_call_history_through_sequence=first_call_history_through_sequence,
                                    model_call_index=model_call_count,
                                    inherited_memory_use_policy=(
                                        current_memory_use_policy
                                    ),
                                    candidate=precompile,
                                    scope_kind=intent.scope_kind,
                                    hook_scope=self._hook_scope,
                                    session_start_compact_port=(
                                        self._compact_session_start_port(intent)
                                    ),
                                    session_start_boundary_port=(
                                        self._compact_session_start_boundary_port(
                                            intent
                                        )
                                    ),
                                )
                            )
                            self._require_active_compaction_continuation(compaction)
                            if compaction.successor_dispatch is not None:
                                model_call_count -= 1
                                successor_dispatch = compaction.successor_dispatch
                                successor_wire_decision = compaction.successor_wire_decision
                                completed_tool_batch = False
                                continue
                    session_start_context = None
                    if self._session_start_boundary.has_pending:
                        if headroom_admission is None:
                            headroom_admission = (
                                await self.compaction.prepare_precompile_admission(
                                    turn_id=turn_id,
                                    model_call_index=model_call_count,
                                    deadline=planning_deadline,
                                )
                            )
                        try:
                            session_start_facts = (
                                await self._provider_dispatch.read_compile_snapshot(
                                    headroom_admission.cut,
                                    deadline=planning_deadline,
                                )
                            )
                            session_start_context = await self._dispatch_initial_session_start(
                                intent,
                                permission_snapshot=(
                                    session_start_facts.run_permission_snapshot
                                ),
                                model_id=(
                                    headroom_admission.prepared_target.target.fact.model_id
                                ),
                                deadline_monotonic=planning_deadline,
                            )
                        except BaseException:
                            headroom_admission.close()
                            if reusable_wire_observation is not None:
                                reusable_wire_observation.discard()
                                reusable_wire_observation = None
                            raise
                    while True:
                        try:
                            admission_handle = (
                                None
                                if headroom_admission is None
                                else headroom_admission.take_handle()
                            )
                            prepared_dispatch = await self._provider_dispatch.prepare(
                                turn_id=turn_id,
                                model_call_index=model_call_count,
                                inherited_memory_use_policy=(current_memory_use_policy),
                                deadline=planning_deadline,
                                existing_handle=admission_handle,
                                headroom_preflight_override=(
                                    None
                                    if headroom_admission is None
                                    else headroom_admission.preflight
                                ),
                                prepared_target_override=(
                                    None
                                    if headroom_admission is None
                                    else headroom_admission.prepared_target
                                ),
                            )
                            if not isinstance(
                                prepared_dispatch, PreparedProviderDispatch
                            ):
                                raise RuntimeError(
                                    "normal provider preparation returned a projection"
                                )
                            dispatch = prepared_dispatch
                            headroom_admission = None
                            session_start_context = None
                            break
                        except (PreparedSteerPlanStale, PreparedCompletionSuffixStale):
                            headroom_admission = None
                            if direct_switch_candidate is not None:
                                raise ConversationKernelConflict(
                                    "direct-switch admission became stale"
                                )
                            if reusable_wire_observation is not None:
                                reusable_wire_observation.discard()
                                reusable_wire_observation = None
                            if monotonic() >= planning_deadline:
                                raise
                            await asyncio.sleep(0)
                        except BaseException:
                            if session_start_context is not None:
                                session_start_context.retire()
                            if reusable_wire_observation is not None:
                                reusable_wire_observation.discard()
                                reusable_wire_observation = None
                            raise
                else:
                    if (
                        dispatch.canonical_facts.canonical_input.identity.turn_id
                        != turn_id
                    ):
                        dispatch.close()
                        raise ConversationKernelConflict(
                            "compaction successor belongs to another turn"
                        )
                try:
                    auto_trigger = (
                        CompactionTrigger.MID_TURN_FOLLOWUP
                        if completed_tool_batch
                        else CompactionTrigger.AUTO_ACTIVE_CONTEXT
                    )
                    if (
                        dispatch.installed_provider_open is None
                        and wire_decision is None
                    ):
                        wire_decision = await self.compaction.measure_dispatch_wire(
                            dispatch,
                            deadline=planning_deadline,
                            reusable_observation=reusable_wire_observation,
                        )
                        reusable_wire_observation = None
                    if wire_decision is not None and (
                        (direct_switch_candidate is not None and not self.compaction.direct_switch_wire_admitted(wire_decision))
                        or (not automatic_compaction_decided and self.compaction.wire_decision_crosses_automatic_threshold(dispatch, wire_decision))
                    ):
                        reduced = await self._provider_dispatch.measure_without_user_actions(
                            dispatch, deadline=planning_deadline
                        )
                        if reduced is not None:
                            dispatch.select_wire_candidate(reduced.candidate)
                            wire_decision = reduced
                    if direct_switch_candidate is not None:
                        if wire_decision is None:
                            raise RuntimeError(
                                "direct switch lost its final wire decision"
                            )
                        if self.compaction.direct_switch_wire_admitted(wire_decision):
                            direct_switch_admission, _ = (
                                self._provider_dispatch.freeze_direct_switch_admission(
                                    candidate=wire_decision.candidate,
                                    decision=wire_decision,
                                )
                            )
                        else:
                            dispatch.close_for_canonical_replan()
                            compaction = await self.compaction.execute_model_switch_active(
                                turn_id=turn_id,
                                first_call_history_through_sequence=first_call_history_through_sequence,
                                model_call_index=model_call_count,
                                inherited_memory_use_policy=current_memory_use_policy,
                                candidate=direct_switch_candidate,
                                scope_kind=intent.scope_kind,
                                hook_scope=self._hook_scope,
                                session_start_compact_port=self._compact_session_start_port(
                                    intent
                                ),
                                session_start_boundary_port=self._compact_session_start_boundary_port(
                                    intent
                                ),
                            )
                            self._require_active_compaction_continuation(compaction)
                            if compaction.successor_dispatch is None:
                                raise RuntimeError(
                                    "model switch did not prepare its successor"
                                )
                            model_call_count -= 1
                            successor_dispatch = compaction.successor_dispatch
                            successor_wire_decision = compaction.successor_wire_decision
                            completed_tool_batch = False
                            continue
                    if (
                        not automatic_compaction_decided
                        and wire_decision is not None
                        and self.compaction.wire_decision_crosses_automatic_threshold(
                            dispatch, wire_decision
                        )
                    ):
                        dispatch.close_for_canonical_replan()
                        compaction = await self.compaction.execute_active(
                            turn_id=turn_id,
                            first_call_history_through_sequence=first_call_history_through_sequence,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            trigger=auto_trigger,
                            force=False,
                            scope_kind=intent.scope_kind,
                            hook_scope=self._hook_scope,
                            session_start_compact_port=(
                                self._compact_session_start_port(intent)
                            ),
                            session_start_boundary_port=(
                                self._compact_session_start_boundary_port(intent)
                            ),
                        )
                        self._require_active_compaction_continuation(compaction)
                        if compaction.successor_dispatch is not None:
                            model_call_count -= 1
                            successor_dispatch = compaction.successor_dispatch
                            successor_wire_decision = compaction.successor_wire_decision
                            completed_tool_batch = False
                            continue
                        planning_deadline = self._planning_deadline()
                        prepared_dispatch = await self._provider_dispatch.prepare(
                            turn_id=turn_id,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            deadline=planning_deadline,
                        )
                        if not isinstance(prepared_dispatch, PreparedProviderDispatch):
                            raise RuntimeError(
                                "ordinary post-compaction decision is not installable"
                            )
                        dispatch = prepared_dispatch
                        wire_decision = await self.compaction.measure_dispatch_wire(
                            dispatch,
                            deadline=planning_deadline,
                        )
                    if wire_decision is not None and wire_decision.wire_input_plan is None:
                        unavailable = await self._provider_dispatch.measure_with_unavailable_tool_pages(
                            dispatch, deadline=planning_deadline
                        )
                        if unavailable is not None:
                            dispatch.select_wire_candidate(unavailable.candidate)
                            wire_decision = unavailable
                except BaseException:
                    if dispatch.owns_execution_authority:
                        dispatch.close()
                    raise
                completed_tool_batch = False
                if not isinstance(dispatch.prepared_call, PreparedKernelModelCall):
                    dispatch.close()
                    raise RuntimeError(
                        "provider execution lacks an execution-backed surface"
                    )
                provider_replay_reservation = None
                pending_http413_failure: ProviderModelExecutionFailed | None = None
                execution = None
                business_output_collected = False
                try:
                    canonical_facts = dispatch.canonical_facts
                    canonical_input = canonical_facts.canonical_input
                    identity = canonical_input.identity
                    planning = dispatch.planning
                    memory_context = dispatch.memory_context
                    current_memory_use_policy = memory_context.memory_use_policy
                    if identity.conversation_scope_kind is ModelInputScopeKind.ROOT:
                        self._root_memory_use_policy = current_memory_use_policy
                    append_result = dispatch.append_result
                    compiled_input = append_result.compiled_input
                    self._offer_compile_observation(
                        turn_id=turn_id,
                        model_call_index=model_call_count,
                        compiled=compiled_input,
                    )
                    provider_open = dispatch.installed_provider_open
                    if provider_open is None:
                        try:
                            if wire_decision is None:
                                wire_decision = (
                                    await self.compaction.measure_dispatch_wire(
                                        dispatch,
                                        deadline=planning_deadline,
                                    )
                                )
                            prepared_wire = self._provider_dispatch.bind_prepared_executable_wire_input(
                                owner_dispatch=dispatch,
                                decision=wire_decision,
                                deadline=planning_deadline,
                                direct_switch_admission=direct_switch_admission,
                            )
                            _require_http413_retry_shrink(
                                http413_failed_wire_quote,
                                http413_recovery_source,
                                prepared_wire.quote,
                            )
                            if intent.cause is not None:
                                raise asyncio.CancelledError
                            provider_open = (
                                await self._provider_dispatch.install_provider_open(
                                    dispatch=dispatch,
                                    prepared_wire=prepared_wire,
                                    turn_id=turn_id,
                                    model_call_index=model_call_count,
                                    deadline=planning_deadline,
                                )
                            )
                        finally:
                            dispatch.retire_hook_context_reservation()
                    request = provider_open.request
                    _require_http413_retry_shrink(
                        http413_failed_wire_quote,
                        http413_recovery_source,
                        request.wire_input_plan.quote,
                    )
                    execution = provider_open.execution
                    permit = provider_open.permit
                    entry_id = random_id("entry")
                    if self._workspace_gate is not None:
                        await self._workspace_gate.wait_available()
                    if intent.cause is not None:
                        raise asyncio.CancelledError
                    collected = await self._collect_model(
                        request,
                        execution=execution,
                        permit=permit,
                        proposed_entry_id=entry_id,
                    )
                    business_output_collected = True
                    completed = collected.completed
                    entry_id = completed.draft_identity
                    calls = tuple(
                        item
                        for item in completed.blocks
                        if isinstance(item, CompletedToolCallBlock)
                    )
                    provider_replay_reservation = (
                        None
                        if collected.provider_replay is None
                        else self._assistant_settlements.prepare_replay_reservation(
                            scope=permit.scope,
                            epoch_nonce=permit.epoch_nonce,
                            epoch_revision=permit.epoch_revision,
                            provider_replay=collected.provider_replay,
                        )
                    )
                    canonical_blocks = await self._canonical_blocks(completed)
                    occurred_at = datetime.now(timezone.utc)
                    complete_turn = not calls
                    stop_continuation = None
                    stop_causal_ref = None
                    if (
                        complete_turn
                        and self._hook_dispatcher is not None
                        and self._hook_context_owner is not None
                        and self._hook_scope is not None
                    ):
                        stop_public_input = StopInput(
                            session_kind=identity.conversation_scope_kind.value,
                            input_origin=None,
                            session_id=self._writer_lease.guard.session_id,
                            cwd=str(self._tools.snapshot_workspace_root()),
                            model=request.prepared_call.call.target.fact.model_id,
                            turn_id=turn_id,
                            stop_hook_active=stop_continuation_used,
                            last_assistant_message=completed.public_text,
                            permission_mode=external_permission_mode(
                                canonical_facts.run_permission_snapshot.effective_mode.value,
                                active_plan_workflow=(
                                    canonical_facts.run_permission_snapshot.plan_workflow_id
                                    is not None
                                ),
                            ),
                        )
                        stop_causal_ref = StopRef(
                            turn_id=turn_id,
                            assistant_entry_id=entry_id,
                            epoch_nonce=permit.epoch_nonce,
                            binding_revision_id=request.cut.context_binding_revision_id,
                        )
                        stop_continuation = await _dispatch_session_stop_hooks(
                            self._hook_dispatcher,
                            HookDispatchEnvelope(
                                self._hook_dispatcher.capture_view(),
                                self._hook_scope,
                                stop_public_input,
                                stop_causal_ref,
                                self._planning_deadline(),
                            ),
                            intent,
                        )
                        complete_turn = (
                            stop_continuation.decision
                            is ContinuationDecision.TERMINALIZE
                        )
                    frozen_visualizations = ()
                    if complete_turn and not calls:
                        frozen_visualizations = (
                            await self._tools.prepare_visualizations(
                                assistant_display_body(
                                    block.text
                                    for block in completed.blocks
                                    if isinstance(block, CompletedTextBlock)
                                ),
                                io=self._io,
                                workspace_id=await self._resolved_workspace_id(),
                                deadline_monotonic=self._canonical_deadline(),
                            )
                        )
                    settlement = PreparedAssistantMessageSettlement(
                        guard=self._writer_lease.guard,
                        cut=request.cut,
                        entry_id=entry_id,
                        blocks=canonical_blocks,
                        complete_turn=complete_turn,
                        occurred_at=occurred_at,
                        actor_id="model:foreground",
                        continuity_scope=permit.scope,
                        continuity_epoch_nonce=permit.epoch_nonce,
                        continuity_epoch_revision=permit.epoch_revision,
                        visualizations=tuple(frozen_visualizations),
                        provider_replay=collected.provider_replay,
                        provider_replay_reservation=(provider_replay_reservation),
                    )
                    # The settlement owner now owns promotion/release across
                    # exact-confirm and caller cancellation.
                    provider_replay_reservation = None
                    accepted = await self._assistant_settlements.settle(settlement)
                    # Canonical assistant acceptance, rather than transport
                    # completion, ends both first-call and 413 recovery scope.
                    first_call_history_through_sequence = None
                    http413_failed_wire_quote = None
                    http413_recovery_source = None
                    if (
                        stop_continuation is not None
                        and stop_continuation.decision
                        is ContinuationDecision.CONTINUE_ONCE
                        and not accepted.pending_steer_at_settlement
                    ):
                        if stop_continuation.continuation_source is None:
                            raise RuntimeError(
                                "Stop continuation lacks its exact Hook source"
                            )
                        if stop_causal_ref is None:
                            raise RuntimeError("Stop continuation lacks its causal ref")
                        stop_continuation_used = True
                        self._hook_context_owner.accept_continuation(
                            scope=self._hook_scope,
                            causal_ref=stop_causal_ref,
                            source_entry=stop_continuation.continuation_source,
                            reason=stop_continuation.reason,
                        )
                    self._live_bus.offer_settlement_nowait(
                        kind=LiveSettlementKind.COMMITTED,
                        session_id=request.session_id,
                        turn_id=turn_id,
                        draft_identity=entry_id,
                        committed_entry_id=accepted.entry_id,
                        scope_kind=identity.conversation_scope_kind.value,
                        channel_kind=LiveChannelKind.MODEL_OUTPUT,
                        generation_id=derive_id("generation", "draft", entry_id),
                        proposed_entry_id=entry_id,
                    )
                except ProviderModelExecutionFailed as error:
                    if not _is_http413_failure(error):
                        raise
                    if intent.cause is not None:
                        raise asyncio.CancelledError from error
                    if (
                        business_output_collected
                        or execution is None
                        or not _http413_execution_closed_without_output(error, execution)
                    ):
                        raise
                    if http413_failed_wire_quote is not None:
                        raise KernelProviderModelExecutionFailed(
                            error.error,
                            "服务端拒绝了过大的请求体；压缩后重试仍失败。"
                            "请缩小本次输入或媒体读取范围后再试。\n"
                            + (error.public_detail if isinstance(
                                error, KernelProviderModelExecutionFailed,
                            ) else error.error.message),
                        ) from error
                    if not self.compaction.automatic_allowed(scope_kind=intent.scope_kind):
                        raise _http413_recovery_failed(
                            error, "当前自动上下文压缩不可用。",
                        ) from error
                    pending_http413_failure = error
                finally:
                    if provider_replay_reservation is not None:
                        self._continuity.release_assistant_replay_fragment_reservation(
                            provider_replay_reservation
                        )
                    active_surface_borrow = dispatch.finish_model_operation()
                if pending_http413_failure is not None:
                    # Retire the failed dispatch before taking a new canonical
                    # compaction fence. Neither handle nor surface may survive.
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    if intent.cause is not None:
                        raise asyncio.CancelledError
                    http413_recovery_source = pending_http413_failure
                    http413_failed_wire_quote = request.wire_input_plan.quote
                    await self._io.run(
                        self._repository.require_provider_safe_turn,
                        self._writer_lease.guard,
                        turn_id=turn_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    compaction = None
                    try:
                        compaction = await self.compaction.execute_active(
                            turn_id=turn_id,
                            model_call_index=model_call_count + 1,
                            inherited_memory_use_policy=current_memory_use_policy,
                            trigger=CompactionTrigger.HTTP_413_RECOVERY,
                            force=True,
                            scope_kind=intent.scope_kind,
                            hook_scope=self._hook_scope,
                            session_start_compact_port=self._compact_session_start_port(intent),
                            session_start_boundary_port=self._compact_session_start_boundary_port(intent),
                            first_call_history_through_sequence=first_call_history_through_sequence,
                            failed_wire_quote=http413_failed_wire_quote,
                        )
                        self._require_active_compaction_continuation(compaction)
                        if compaction.successor_dispatch is None:
                            raise _http413_recovery_failed(
                                pending_http413_failure,
                                compaction.outcome.public_code or "未采用可执行后继。",
                            )
                    except (asyncio.CancelledError, StaleHostWriter):
                        if compaction is not None and compaction.successor_dispatch is not None:
                            compaction.successor_dispatch.close()
                        raise
                    except Exception as error:
                        if compaction is not None and compaction.successor_dispatch is not None:
                            compaction.successor_dispatch.close()
                        if intent.cause is not None:
                            raise asyncio.CancelledError from error
                        if isinstance(error, KernelProviderModelExecutionFailed) and (
                            error.error is pending_http413_failure.error
                        ):
                            raise
                        raise _http413_recovery_failed(
                            pending_http413_failure,
                            execution.scrub_provider_detail(f"{type(error).__name__}: {error}"),
                        ) from error
                    successor_dispatch = compaction.successor_dispatch
                    successor_wire_decision = compaction.successor_wire_decision
                    continue
                if not calls and accepted.turn_completed:
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    return KernelRunResult(
                        turn_id=turn_id,
                        final_entry_id=accepted.entry_id,
                        final_text=completed.public_text,
                        model_call_count=model_call_count,
                        tool_call_count=tool_call_count,
                        final_source=(
                            AcceptedProviderFinalSource(
                                call_target=request.prepared_call.epoch_call_target.physical_call_target,
                                compiled_input=request.compiled_input,
                                wire_input_plan=request.wire_input_plan,
                                final_entry_id=accepted.entry_id,
                                final_message=LLMMessage.assistant(
                                    completed.public_text
                                ),
                                provider_replay=None
                                if collected.provider_replay is None
                                else collected.provider_replay.fragment(),
                                usage_anchor=self._continuity.current_usage_anchor(
                                    permit.scope
                                ),
                            )
                            if identity.conversation_scope_kind
                            is ModelInputScopeKind.ROOT
                            else None
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
                    plan_epoch = self._continuity.current_view(planning.scope)
                    if plan_epoch is None:
                        raise RuntimeError("Plan batch lost continuity epoch")
                    outcome = await self._plan_batches.accept_batch(
                        calls=calls,
                        selected_call_index=plan_call_indexes[0],
                        assistant_entry_id=accepted.entry_id,
                        canonical_facts=canonical_facts,
                        surface_borrow=active_surface_borrow,
                        deadline=self._canonical_deadline(),
                        hook_model=request.prepared_call.call.target.fact.model_id,
                        hook_cwd=str(self._tools.snapshot_workspace_root()),
                        continuity_epoch_nonce=plan_epoch.epoch_nonce,
                    )
                    tool_call_count += len(calls)
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    if outcome.interaction_kind is PlanInteractionKind.QUESTION:
                        # The Host resolution command installs the actual answer.
                        continue
                    if not outcome.origin_turn_completed:
                        # Idempotent enter_plan keeps this exact run.
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
                if active_surface_borrow is None:
                    raise RuntimeError("Tool batch lost its execution-backed borrow")
                batch_borrow = active_surface_borrow
                active_surface_borrow = None
                batch = await self._tool_batches.execute(
                    turn_id=turn_id,
                    assistant_entry_id=accepted.entry_id,
                    calls=calls,
                    canonical_facts=canonical_facts,
                    canonical_identity=identity,
                    request=request,
                    agent_invocation_snapshot=provider_open.agent_invocation_snapshot,
                    continuity_scope=planning.scope,
                    surface_borrow=batch_borrow,
                    hook_scope=self._hook_scope,
                )
                tool_call_count += batch.tool_call_count
                completed_tool_batch = True
        except BaseException as error:
            try:
                if successor_dispatch is not None:
                    successor_dispatch.close()
                if active_surface_borrow is not None:
                    active_surface_borrow.close()
                    active_surface_borrow = None
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
                    failure_payload: dict[str, object] = {
                        "failure_code": "FOREGROUND_EXECUTION_INTERRUPTED"
                    }
                    if isinstance(error, ProviderModelExecutionFailed):
                        failure_payload["provider_error_code"] = error.error.code.value
                        failure_payload["provider_error_fingerprint"] = (
                            error.error.error_fingerprint
                        )
                    self._offer_operational_best_effort(
                        OperationalHookOffer(
                            event_type=OperationalHookType.FOREGROUND_TURN_FAILED,
                            session_id=self._writer_lease.guard.session_id,
                            turn_id=turn_id,
                            public_payload=failure_payload,
                        )
                    )
            finally:
                cause = intent.cause
                # The installed logical stop/close cause remains the ROOT winner
                # even if physical cancellation surfaces a typed provider error.
                reason = (
                    root_cancellation_terminal_reason(intent)
                    if cause is not None
                    else _provider_incomplete_terminal_reason(error)
                )
                if reason is None:
                    reason = _model_input_terminal_reason(error)
                if reason is None:
                    reason = "FOREGROUND_EXECUTION_INTERRUPTED"
                public_detail = None
                if isinstance(error, ProviderModelExecutionFailed) and cause is None:
                    reason = "PROVIDER_REQUEST_FAILED"
                    public_detail = (
                        error.public_detail if isinstance(error, KernelProviderModelExecutionFailed)
                        else f"{error.error.code.value}: {error.error.message}")
                if public_detail is None:
                    await self._turn_admission.interrupt_turn(turn_id, reason=reason)
                else:
                    await self._turn_admission.interrupt_turn(
                        turn_id, reason=reason, public_detail=public_detail
                    )
            raise

    async def _dispatch_initial_session_start(
        self,
        intent: ActiveTurnCancellationIntent,
        *,
        permission_snapshot: FrozenRunPermissionSnapshot,
        model_id: str,
        deadline_monotonic: float,
    ) -> PendingHookContextReservation | None:
        boundary = await self._session_start_boundary.consume_any()
        if boundary is None:
            return None
        dispatcher = self._hook_dispatcher
        context_owner = self._hook_context_owner
        scope = self._hook_scope
        if dispatcher is None or context_owner is None or scope is None:
            return None
        view = dispatcher.capture_view()
        source = boundary.source
        token = _SessionStartAttemptToken(
            self._writer_lease.guard.session_id,
            source,
            boundary.boundary_token,
        )
        cwd = self._tools.snapshot_workspace_root()
        public_input = SessionStartInput(
            session_kind=self._session_scope_kind.value,
            input_origin=None,
            session_id=self._writer_lease.guard.session_id,
            cwd=str(cwd),
            model=model_id,
            source=source,
            permission_mode=external_permission_mode(
                permission_snapshot.effective_mode.value,
                active_plan_workflow=(permission_snapshot.plan_workflow_id is not None),
            ),
        )
        causal_ref = SessionStartRef(token, source)
        outcome = await _dispatch_session_start_hooks(
            dispatcher,
            HookDispatchEnvelope(
                view,
                scope,
                public_input,
                causal_ref,
                deadline_monotonic,
            ),
            intent,
        )
        if outcome.decision is GateDecision.BLOCK:
            raise SessionStartBlocked(outcome.reason or "SessionStart Hook blocked")
        if intent.cause is not None:
            raise asyncio.CancelledError
        reservation = context_owner.prepare_sync(
            scope=scope,
            causal_ref=causal_ref,
            entries=outcome.context_entries,
        )
        if reservation is not None:
            reservation.commit()
        return reservation


    def provider_retry_observation(self) -> dict[str, object] | None:
        execution = self._active_execution
        return None if execution is None else execution.provider_retry_observation()

    async def _collect_model(
        self,
        request: KernelModelExecutionRequest,
        *,
        execution: PreparedKernelModelExecution,
        permit: ProcessLocalProviderInputInstallPermit,
        proposed_entry_id: str,
    ) -> _CollectedModelResponse:
        def new_assembler() -> ProviderStreamAssembler:
            return ProviderStreamAssembler(
                session_id=request.session_id,
                turn_id=request.turn_id,
                live_bus=self._live_bus,
                proposed_entry_id=proposed_entry_id,
                conversation_scope_kind=(
                    request.compiled_input.canonical_input_identity.conversation_scope_kind.value
                ),
            )

        def abort_generation(reason: str) -> None:
            self._live_bus.offer_settlement_nowait(
                kind=LiveSettlementKind.ABORTED,
                session_id=request.session_id,
                turn_id=request.turn_id,
                draft_identity=proposed_entry_id,
                reason_code=reason,
                scope_kind=(
                    request.compiled_input.canonical_input_identity.conversation_scope_kind.value
                ),
                channel_kind=LiveChannelKind.MODEL_OUTPUT,
                generation_id=derive_id("generation", "draft", proposed_entry_id),
                proposed_entry_id=proposed_entry_id,
            )

        assembler = new_assembler()
        if self._active_execution is not None:
            raise RuntimeError("another provider execution is already active")
        self._active_execution = execution
        stream = execution.open_once(permit)
        collection_error: BaseException | None = None
        try:
            execution.set_draft_identity(proposed_entry_id)
            async for item in stream:
                if isinstance(item, ProviderGenerationRetry):
                    # No assistant settlement or tool dispatch has happened.
                    # Replace only disposable output, keeping the installed
                    # provider request and its one-shot authority intact.
                    reason_code = {
                        ProviderRetryReason.CONNECTION_RECOVERY: "PROVIDER_CONNECTION_RECOVERY",
                        ProviderRetryReason.PROVIDER_RECOVERY: "PROVIDER_RECOVERY",
                        ProviderRetryReason.TOOL_ARGUMENT_REGENERATION: "TOOL_ARGUMENTS_REGENERATING",
                    }[item.reason]
                    abort_generation(reason_code)
                    proposed_entry_id = random_id("entry")
                    execution.set_draft_identity(proposed_entry_id)
                    assembler = new_assembler()
                    continue
                assembler.apply(item)
            provider_completion = execution.take_completed_result_once()
            completed = assembler.complete()
            public_text = "".join(
                block.text
                if isinstance(block, CompletedTextBlock)
                else block.data
                if isinstance(block, CompletedDataBlock)
                else ""
                for block in completed.blocks
            )
            public_calls = tuple(
                LLMToolCall(
                    id=block.tool_call_id,
                    name=block.tool_name,
                    arguments=canonical_json_bytes(thaw_json(block.arguments)).decode(
                        "utf-8"
                    ),
                )
                for block in completed.blocks
                if isinstance(block, CompletedToolCallBlock)
            )
            public_blocks = tuple(
                ("TEXT", block.text)
                if isinstance(block, CompletedTextBlock)
                else ("DATA", block.media_type, block.data)
                if isinstance(block, CompletedDataBlock)
                else (
                    "TOOL_CALL",
                    block.tool_call_id,
                    block.tool_name,
                    canonical_json_bytes(thaw_json(block.arguments)).decode("utf-8"),
                )
                for block in completed.blocks
            )
            public_projection_fingerprint = (
                provider_assistant_public_projection_fingerprint(
                    text=public_text,
                    tool_calls=public_calls,
                    ordered_blocks=public_blocks,
                )
            )
            provider_replay = provider_completion.bind_durable_assistant_entry(
                session_id=request.session_id,
                workspace_id=await self._resolved_workspace_id(),
                assistant_entry_id=proposed_entry_id,
                public_projection_fingerprint=public_projection_fingerprint,
            )
            return _CollectedModelResponse(
                completed=completed,
                provider_completion=provider_completion,
                provider_replay=provider_replay,
            )
        except BaseException as exc:
            collection_error = exc
            abort_generation(f"MODEL_STREAM_{type(exc).__name__.upper()}")
            raise
        finally:
            # A consumer failure leaves open_once suspended at its yield. Own
            # its explicit close through repeated cancellation, rather than
            # leaving transport/borrow retirement to async-generator GC.
            closing = asyncio.create_task(stream.aclose(), name="provider-consumer-close")
            caller = asyncio.current_task()
            cancellation_baseline = 0 if caller is None else caller.cancelling()
            cancellation: asyncio.CancelledError | None = None
            close_error: BaseException | None = None
            try:
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError as exc:
                        if caller is not None and caller.cancelling() > cancellation_baseline:
                            cancellation = cancellation or exc
                    except BaseException:
                        break
                try:
                    closing.result()
                except BaseException as exc:
                    close_error = exc
            finally:
                try:
                    execution.clear_retry_observation()
                finally:
                    if self._active_execution is execution:
                        self._active_execution = None
            if cancellation is not None and (
                collection_error is None
                or isinstance(collection_error, ProviderModelExecutionFailed)
                and _is_http413_failure(collection_error)
            ):
                # Cancellation while joining the consumer close must not leave
                # an earlier HTTP rejection eligible for semantic recovery.
                if collection_error is None:
                    abort_generation("MODEL_STREAM_CANCELLEDERROR")
                raise cancellation from close_error or collection_error
            if collection_error is not None:
                if close_error is not None:
                    if collection_error.__cause__ is None and collection_error.__context__ is None:
                        raise collection_error from close_error
                    collection_error.add_note(execution.scrub_provider_detail(
                        f"Provider cleanup failure: {type(close_error).__name__}: {close_error}"
                    ))
            elif close_error is not None:
                abort_generation(f"MODEL_STREAM_{type(close_error).__name__.upper()}")
                raise close_error

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
                    block_id=derive_id(
                        "block",
                        "message",
                        completed.draft_identity,
                        "text",
                        random_id("segment"),
                    ),
                    text=await self._content(b"", deadline=self._canonical_deadline()),
                )
            )
        return tuple(result)

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

        # NONE retries only this immutable composite.  The model and tool
        # attempt are never reopened or redispatched.

        # NONE: retry the exact prepared candidate.  The physical tool has
        # already returned and is never invoked by this settlement loop.


def _provider_incomplete_terminal_reason(error: BaseException) -> str | None:
    if not isinstance(error, ProviderModelOutputIncomplete):
        return None
    return {
        ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT: (
            "MODEL_OUTPUT_TOKEN_LIMIT_REACHED"
        ),
        ProviderOutputIncompleteReason.CONTEXT_WINDOW_LIMIT_DURING_GENERATION: (
            "MODEL_OUTPUT_CONTEXT_LIMIT_REACHED"
        ),
        ProviderOutputIncompleteReason.CONTENT_FILTERED: (
            "MODEL_OUTPUT_CONTENT_FILTERED"
        ),
        ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE: (
            "MODEL_OUTPUT_INCOMPLETE"
        ),
    }[error.reason]


def _model_input_terminal_reason(error: BaseException) -> str | None:
    if isinstance(error, SessionStartBlocked):
        return "HOOK_SESSION_START_BLOCKED"
    if isinstance(error, CompactionContinuationBlocked):
        return "HOOK_COMPACTION_BLOCKED"
    if not isinstance(error, StructuredModelInputCompileError):
        return None
    if error.kind in {
        ModelInputCompileFailureKind.PROVIDER_INPUT_WIRE_BYTES_EXCEEDED,
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,

        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET,

    }:
        return "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
    return None


def _json_digest(value: FrozenJsonObjectFact) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "ConversationKernelRunner",
    "KernelRunResult",
]
