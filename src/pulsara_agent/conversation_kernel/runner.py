"""Fresh Stage 2 foreground conversation runner.

The runner owns only one live Host activation.  It never resumes a provider,
coroutine, interaction, terminal process, or subagent execution after a crash.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from hashlib import sha256
import json
from time import monotonic
from typing import Mapping, Protocol
from uuid import uuid4

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
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceCollectorPort,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionTrigger,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    CompactionCoordinator,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    KernelColdEpochInputAssembler,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentResultPublicFact,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    stable_subagent_turn_id,
)
from pulsara_agent.conversation_kernel.direct_model import (
    CompletedProviderModelExecution,
    KernelModelExecutionRequest,
    PreparedKernelModelCall,
    PreparedKernelModelExecution,
)
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.llm.input import LLMToolCall
from pulsara_agent.llm.request import (
    provider_assistant_public_projection_fingerprint,
)
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
    ProviderReplayDisposition,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderOutputIncompleteReason,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
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
    TodoRunAdmissionFinalizer,
    TurnAdmissionCoordinator,
    root_cancellation_terminal_reason,
)
from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.conversation_kernel.extensions import (
    KernelExtensionHost,
    OperationalHookOffer,
    OperationalHookType,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.memory.citations import (
    ProcessLocalMemoryCallContextOwner,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolInvocationPort,
    ToolSurfacePlanningPort,
)
from pulsara_agent.conversation_kernel.subagents.runtime_port import (
    SubagentRuntimePort,
)
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    ConversationKernelConflict,
    build_prepared_root_turn_admission,
    build_prepared_subagent_turn_admission,
)
from pulsara_agent.conversation_kernel.plan_runtime import (
    AutomaticPlanContinuationPort,
    KernelPlanInteractionCoordinator,
    PlanToolBatchCoordinator,
)
from pulsara_agent.conversation_kernel.memory.dispatch import (
    MemoryContextProjectionPort,
    MemoryDispatchSupport,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    KernelModelPort,
    PreparedProviderDispatch,
    ProviderDispatchCoordinator,
)
from pulsara_agent.conversation_kernel.steer_consumption import (
    PreparedSteerPlanStale,
)
from pulsara_agent.primitives.plan_workflow import (
    PlanInteractionKind,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.safe_point import (
    ProviderSafePointCoordinator,
)
from pulsara_agent.ports.terminal_observation import PreparedInstallationTarget
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator
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
    ProcessLocalProviderInputInstallPermit,
    ProviderInputContinuityScope,
)

from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE, PermissionMode
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    thaw_json,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    ProcessLocalToolSurfaceBorrow,
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
    memory_reflection_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CollectedModelResponse:
    completed: CompletedAssistantMessage
    provider_completion: CompletedProviderModelExecution
    provider_wire_api: str
    provider_replay_disposition: ProviderReplayDisposition
    provider_replay: PreparedDurableProviderAssistantReplay | None = dataclass_field(
        default=None, repr=False
    )


class _RunnerToolCompositionPort(ToolSurfacePlanningPort, ToolInvocationPort, Protocol):
    """Require one Tool owner to satisfy the runner's two narrow consumers."""


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
        maximum_input_tokens_per_call: int | None = None,
        maximum_output_tokens_per_call: int = STAGE2_LIMITS.provider_output_tokens_per_call_hard,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        memory_projection: MemoryContextProjectionPort | None = None,
        assistant_settlement_owner: AssistantMessageSettlementOwner | None = None,
        todo_admission_finalizer: TodoRunAdmissionFinalizer | None = None,
        compaction_owner: HostCompactionRuntimeOwner | None = None,
        subagent_runtime: SubagentRuntimePort | None = None,
    ) -> None:
        if maximum_output_tokens_per_call < 1 or (
            maximum_input_tokens_per_call is not None
            and maximum_input_tokens_per_call < 1
        ):
            raise ValueError("runner limits must be finite and positive")
        self._writer_lease = writer_lease
        self._live_bus = live_bus
        resolved_input_reader = input_reader or CanonicalProviderInputReader(
            repository.connection_provider,
            blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
        )
        blob_store = PostgresCanonicalBlobStore(repository.connection_provider)
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
        )
        resolved_compiler = compiler or StructuredModelInputCompiler()
        cold_epoch_assembler = KernelColdEpochInputAssembler(resolved_compiler)
        capability_planner = KernelToolCapabilityPlanner()
        self._continuity = continuity_owner or HostProviderInputContinuityOwner(
            session_id=writer_lease.guard.session_id
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
        self._memory_contexts = ProcessLocalMemoryCallContextOwner(
            session_id=writer_lease.guard.session_id
        )
        self._extensions = extensions
        self._memory_dispatch = MemoryDispatchSupport(
            compiler=resolved_compiler,
            io_owner=self._io,
            memory_context_owner=self._memory_contexts,
            memory_projection=memory_projection,
            input_reader=resolved_input_reader,
            deadline_factory=self._deadlines,
        )
        self._root_memory_use_policy = MemoryUsePolicy.ENABLED
        self._turn_admission = TurnAdmissionCoordinator(
            repository=repository,
            io_owner=self._io,
            writer_lease=writer_lease,
            deadline_factory=self._deadlines,
            todo_finalizer=todo_admission_finalizer,
        )
        self._subagent_runtime = subagent_runtime
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
            maximum_output_tokens_per_call=maximum_output_tokens_per_call,
            workspace_resolver=self._workspace_resolver,
            compaction_owner=compaction_owner,
            subagent_runtime=subagent_runtime,
        )
        self.compaction = CompactionCoordinator(
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
        )
        self._tool_batches = ToolBatchExecutor(
            repository=repository,
            writer_lease=writer_lease,
            tools=tools,
            live_bus=live_bus,
            io_owner=self._io,
            content_publisher=self._content_publisher,
            tool_output_processor=self._tool_output_processor,
            continuity_owner=self._continuity,
            memory_context_owner=self._memory_contexts,
            memory_projection=memory_projection,
            extensions=extensions,
            subagent_runtime=subagent_runtime,
            workspace_resolver=self._workspace_resolver,
            deadline_factory=self._deadlines,
        )

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _planning_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING)

    async def _resolved_workspace_id(self) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline()
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
            await self._turn_admission.accept_root(
                candidate, cancellation_intent=intent
            )
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
            await self._turn_admission.accept_subagent(
                candidate, cancellation_intent=intent
            )
        return await self.run_accepted_turn(turn_id, cancellation_intent=intent)

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
        successor_dispatch: PreparedProviderDispatch | None = None
        completed_tool_batch = False
        try:
            while True:
                if (
                    intent.scope_kind is ModelInputScopeKind.SUBAGENT_TASK
                    and intent.scope_subagent_task_id is not None
                    and self._subagent_runtime is not None
                ):
                    await self._subagent_runtime.consume_mailbox_safe_point(
                        intent.scope_subagent_task_id
                    )
                # A successful active compaction already owns the next exact
                # provider dispatch.  Consume that immutable handoff before
                # polling a later manual request; otherwise a back-to-back
                # request can overwrite the only process-local owner of its
                # installed handle and surface borrow.  A later request stays
                # with HostCompactionRuntimeOwner until the next safe loop, or
                # the Host hands it to idle settlement when this turn exits.
                manual_request = None
                if successor_dispatch is None:
                    manual_request = await self.compaction.take_manual(
                        scope_kind=intent.scope_kind,
                        scope_subagent_task_id=intent.scope_subagent_task_id,
                        turn_id=turn_id,
                    )
                if manual_request is not None:
                    compaction = await self.compaction.execute_active(
                        turn_id=turn_id,
                        model_call_index=model_call_count + 1,
                        inherited_memory_use_policy=current_memory_use_policy,
                        trigger=CompactionTrigger.MANUAL,
                        force=manual_request.force,
                        manual_request=manual_request,
                        scope_kind=intent.scope_kind,
                        scope_subagent_task_id=intent.scope_subagent_task_id,
                    )
                    successor_dispatch = compaction.successor_dispatch
                    completed_tool_batch = False
                    continue
                model_call_count += 1
                planning_deadline = self._planning_deadline()
                steer_plan_retries = 0
                dispatch = successor_dispatch
                successor_dispatch = None
                if dispatch is None:
                    while True:
                        try:
                            dispatch = await self._provider_dispatch.prepare(
                                turn_id=turn_id,
                                model_call_index=model_call_count,
                                inherited_memory_use_policy=(current_memory_use_policy),
                                deadline=planning_deadline,
                            )
                            break
                        except PreparedSteerPlanStale:
                            steer_plan_retries += 1
                            if (
                                steer_plan_retries >= 3
                                or monotonic() >= planning_deadline
                            ):
                                raise
                            await asyncio.sleep(0)
                else:
                    if (
                        dispatch.canonical_facts.canonical_input.identity.turn_id
                        != turn_id
                    ):
                        dispatch.handle.close()
                        dispatch.close_surface_borrow()
                        raise ConversationKernelConflict(
                            "compaction successor belongs to another turn"
                        )
                auto_trigger = (
                    CompactionTrigger.MID_TURN_FOLLOWUP
                    if completed_tool_batch
                    else CompactionTrigger.AUTO_ACTIVE_CONTEXT
                )
                if self.compaction.automatic_allowed(
                    scope_kind=intent.scope_kind,
                    scope_subagent_task_id=intent.scope_subagent_task_id,
                ) and self.compaction.dispatch_crosses_threshold(dispatch):
                    dispatch.handle.close()
                    dispatch.close_surface_borrow()
                    model_call_count -= 1
                    compaction = await self.compaction.execute_active(
                        turn_id=turn_id,
                        model_call_index=model_call_count + 1,
                        inherited_memory_use_policy=current_memory_use_policy,
                        trigger=auto_trigger,
                        force=False,
                        manual_request=None,
                        scope_kind=intent.scope_kind,
                        scope_subagent_task_id=intent.scope_subagent_task_id,
                    )
                    successor_dispatch = compaction.successor_dispatch
                    completed_tool_batch = False
                    continue
                completed_tool_batch = False
                prepared = dispatch.handle
                if dispatch.surface_borrow is None or not isinstance(
                    dispatch.prepared_call, PreparedKernelModelCall
                ):
                    dispatch.handle.close()
                    dispatch.close_surface_borrow()
                    raise RuntimeError(
                        "provider execution lacks an execution-backed surface"
                    )
                active_surface_borrow = dispatch.surface_borrow
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
                        provider_open = (
                            await self._provider_dispatch.install_provider_open(
                                dispatch=dispatch,
                                turn_id=turn_id,
                                model_call_index=model_call_count,
                                deadline=planning_deadline,
                            )
                        )
                    request = provider_open.request
                    execution = provider_open.execution
                    permit = provider_open.permit
                    entry_id = _id("entry")
                    collected = await self._collect_model(
                        request,
                        execution=execution,
                        permit=permit,
                        proposed_entry_id=entry_id,
                    )
                    completed = collected.completed
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
                    completion_prepared: (
                        tuple[object, FrozenSubagentResultPublicFact] | None
                    ) = None
                    complete_turn = not calls
                    if (
                        complete_turn
                        and identity.conversation_scope_kind
                        is ModelInputScopeKind.SUBAGENT_TASK
                        and identity.scope_subagent_task_id is not None
                        and self._subagent_runtime is not None
                    ):
                        completion_prepared = (
                            await self._subagent_runtime.prepare_inferred_completion(
                                task_id=identity.scope_subagent_task_id,
                                entry_id=entry_id,
                                public_text=completed.public_text,
                            )
                        )
                        complete_turn = completion_prepared is not None
                    subagent_result = (
                        None if completion_prepared is None else completion_prepared[1]
                    )
                    settlement = PreparedAssistantMessageSettlement(
                        guard=self._writer_lease.guard,
                        cut=request.cut,
                        entry_id=entry_id,
                        parent_content=parent_content,
                        blocks=canonical_blocks,
                        complete_turn=complete_turn,
                        occurred_at=occurred_at,
                        actor_id="model:foreground",
                        continuity_scope=permit.scope,
                        continuity_epoch_nonce=permit.epoch_nonce,
                        continuity_epoch_revision=permit.epoch_revision,
                        provider_wire_api=collected.provider_wire_api,
                        provider_replay_disposition=(
                            collected.provider_replay_disposition
                        ),
                        provider_replay=collected.provider_replay,
                        subagent_result=subagent_result,
                    )
                    try:
                        accepted = await self._assistant_settlements.settle(settlement)
                    except BaseException:
                        if completion_prepared is not None:
                            await self._subagent_runtime.finish_completion(
                                completion_prepared[0], committed=False
                            )
                        raise
                    if completion_prepared is not None:
                        await self._subagent_runtime.finish_completion(
                            completion_prepared[0], committed=accepted.turn_completed
                        )
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
                    reflection_token = await self._memory_dispatch.prepare_reflection(
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
                    outcome = await self._plan_batches.accept_batch(
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
                    subagent_parent_context_subject=(
                        provider_open.subagent_parent_context_subject
                    ),
                    continuity_scope=planning.scope,
                    surface_borrow=batch_borrow,
                )
                tool_call_count += batch.tool_call_count
                remember_requested = remember_requested or batch.remember_requested
                if batch.terminal is not None:
                    return KernelRunResult(
                        turn_id=turn_id,
                        final_entry_id=batch.terminal.final_entry_id,
                        final_text=batch.terminal.final_text,
                        model_call_count=model_call_count,
                        tool_call_count=tool_call_count,
                    )
                completed_tool_batch = True
        except BaseException as error:
            if successor_dispatch is not None:
                successor_dispatch.handle.close()
                successor_dispatch.close_surface_borrow()
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
            cause = intent.cause
            if (
                intent.scope_kind is ModelInputScopeKind.SUBAGENT_TASK
                and cause is not None
            ):
                # The child manager owns the atomic turn+task settlement.
                raise
            reason = _provider_incomplete_terminal_reason(error)
            if reason is None:
                reason = _model_input_terminal_reason(error)
            if reason is None:
                reason = (
                    root_cancellation_terminal_reason(intent)
                    if isinstance(error, asyncio.CancelledError) and cause is not None
                    else "FOREGROUND_EXECUTION_INTERRUPTED"
                )
            await self._turn_admission.interrupt_turn(turn_id, reason=reason)
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
    ) -> _CollectedModelResponse:
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
            replay_disposition, provider_replay = (
                provider_completion.bind_durable_assistant_entry(
                    session_id=request.session_id,
                    workspace_id=await self._resolved_workspace_id(),
                    assistant_entry_id=proposed_entry_id,
                    public_projection_fingerprint=public_projection_fingerprint,
                )
            )
            return _CollectedModelResponse(
                completed=completed,
                provider_completion=provider_completion,
                provider_wire_api=provider_completion.replay_target.wire_api,
                provider_replay_disposition=replay_disposition,
                provider_replay=provider_replay,
            )
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
                    text=await self._content(b"", deadline=self._canonical_deadline()),
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

        # NONE retries only this immutable composite.  The model and tool
        # attempt are never reopened or redispatched.

        # NONE: retry the exact prepared candidate.  The physical tool has
        # already returned and is never invoked by this settlement loop.


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


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
    if not isinstance(error, StructuredModelInputCompileError):
        return None
    if error.kind in {
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }:
        return "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
    return None


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _json_digest(value: FrozenJsonObjectFact) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "ConversationKernelRunner",
    "KernelRunResult",
]
