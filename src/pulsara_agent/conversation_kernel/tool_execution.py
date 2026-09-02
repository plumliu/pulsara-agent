"""Physical Tool batch execution and exact canonical result settlement."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass

from datetime import datetime, timezone

from hashlib import sha256


from threading import Lock

from typing import Mapping

from uuid import uuid4

from psycopg import InterfaceError, OperationalError


from pulsara_agent.conversation_kernel.assembler import (
    CompletedToolCallBlock,
)


from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
)


from pulsara_agent.conversation_kernel.subagents.contracts import (
    ExplicitSubagentResultConfirmationKind,
    FrozenSubagentParentContextCallSubject,
    FrozenSubagentResultPublicFact,
    build_explicit_subagent_result_settlement,
)


from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
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
    LiveBlockKind,
    LiveChannelKind,
    LiveSettlementKind,
)

from pulsara_agent.conversation_kernel.io import KernelSessionIO

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)


from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver

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


from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryCitationEvidenceKind,
    MemoryCitationVisibility,
)

from pulsara_agent.conversation_kernel.memory.citations import (
    ProcessLocalMemoryCallContextOwner,
)

from pulsara_agent.conversation_kernel.tool_artifacts import (
    ToolOutputArtifactProcessor,
)

from pulsara_agent.conversation_kernel.tool_contracts import (
    AcceptedCanonicalToolResultSettlement,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementToken,
    PreparedResolvedToolInvocation,
    PreparedPermissionRequest,
    PreparedToolPreparationRejection,
    build_accepted_canonical_tool_result_settlement,
)

from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    ConversationKernelRepository,
    ConversationKernelConflict,
    PreparedToolRemoteIdentityPublication,
    PreparedToolResultAcceptance,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
)


from pulsara_agent.conversation_kernel.memory.dispatch import (
    MemoryContextProjectionPort,
)

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry


from pulsara_agent.primitives.tool_observation import ToolObservationOrigin


from pulsara_agent.conversation_kernel.vocabulary import LiveEventType


from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCanonicalCompileSnapshot,
    ModelInputScopeKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.lowering import project_tool_result_public_value

from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
)


from pulsara_agent.primitives.context import (
    freeze_json,
    FrozenJsonObjectFact,
    thaw_json,
)

from pulsara_agent.conversation_kernel.tool_surface import (
    ProcessLocalToolSurfaceBorrow,
    tool_observation_origin_for_binding,
)


from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolInvocationPort,
)
from pulsara_agent.conversation_kernel.subagents.runtime_port import (
    PreparedExplicitSubagentCompletion,
    SubagentRuntimePort,
)
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.contracts import (
    GateDecision,
    FrozenHookDefinitionView,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    PermissionDecision,
    PermissionRef,
    PermissionRequestInput,
    PostToolRef,
    PostToolUseInput,
    PreToolRef,
    PreToolUseInput,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject, tool_matcher_subject


_RETRYABLE_EXPLICIT_RESULT_SETTLEMENT_ERRORS = (
    TimeoutError,
    ConnectionError,
    InterfaceError,
    OperationalError,
)


@dataclass(frozen=True, slots=True)
class _KnownToolResultSettlementOutcome:
    settlement: AcceptedCanonicalToolResultSettlement
    process_local_effect_committed: bool


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


@dataclass(frozen=True, slots=True)
class ToolBatchTerminalResult:
    final_entry_id: str
    final_text: str


@dataclass(frozen=True, slots=True)
class ToolBatchExecutionResult:
    tool_call_count: int
    terminal: ToolBatchTerminalResult | None = None


class ToolBatchExecutor:
    """Own authorize → attempt → physical invoke → exact result settlement."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        tools: ToolInvocationPort,
        live_bus: LiveAgentEventBus,
        io_owner: KernelSessionIO,
        content_publisher: CanonicalContentPublisher,
        tool_output_processor: ToolOutputArtifactProcessor,
        continuity_owner: HostProviderInputContinuityOwner,
        memory_context_owner: ProcessLocalMemoryCallContextOwner,
        memory_projection: MemoryContextProjectionPort | None,
        extensions: KernelExtensionHost | None,
        subagent_runtime: SubagentRuntimePort | None,
        workspace_resolver: SessionWorkspaceResolver,
        deadline_factory: KernelExecutionDeadlineFactory,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
    ) -> None:
        self._repository = repository
        self._writer_lease = writer_lease
        self._tools = tools
        self._live_bus = live_bus
        self._io = io_owner
        self._content_publisher = content_publisher
        self._tool_output_processor = tool_output_processor
        self._continuity = continuity_owner
        self._memory_contexts = memory_context_owner
        self._memory_projection = memory_projection
        self._extensions = extensions
        self._subagent_runtime = subagent_runtime
        self._workspace_resolver = workspace_resolver
        self._deadlines = deadline_factory
        self._hooks = hook_dispatcher
        self._hook_context = hook_context_owner

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def _resolved_workspace_id(self) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline()
        )

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

    def _offer_operational_best_effort(self, offer: OperationalHookOffer) -> None:
        if self._extensions is None:
            return
        try:
            self._extensions.offer_operational(offer)
        except Exception:
            return

    def _hook_model_id(self, request: KernelModelExecutionRequest) -> str:
        return request.prepared_call.call.target.fact.model_id

    async def _dispatch_pre_tool(
        self,
        *,
        view: FrozenHookDefinitionView,
        scope: HookDispatchScopeRef,
        prepared: PreparedResolvedToolInvocation,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        request: KernelModelExecutionRequest,
        canonical_facts: FrozenCanonicalCompileSnapshot,
    ):
        assert self._hooks is not None
        subject = tool_matcher_subject(
            prepared.requested_tool_name,
            resolved_remote_identity=(
                prepared.canonical_tool_name
                if prepared.canonical_tool_name != prepared.requested_tool_name
                else None
            ),
        )
        arguments = thaw_json(prepared.resolved_arguments)
        causal_ref = PreToolRef(
            turn_id,
            assistant_entry_id,
            tool_call_id,
            prepared.canonical_tool_name,
        )
        public_input = PreToolUseInput(
            session_id=request.session_id,
            cwd=str(self._tools.snapshot_terminal_cwd()),
            model=self._hook_model_id(request),
            turn_id=turn_id,
            tool_name=subject.external_primary,
            tool_use_id=tool_call_id,
            tool_input=arguments,
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
            pulsara_tool_name=subject.pulsara_tool_name,
        )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                scope,
                public_input,
                causal_ref,
                self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, tool=subject
            ),
        )
        return outcome, causal_ref

    async def _dispatch_permission_request(
        self,
        *,
        view: FrozenHookDefinitionView,
        scope: HookDispatchScopeRef,
        prepared: PreparedResolvedToolInvocation,
        permission_request: PreparedPermissionRequest,
        turn_id: str,
        tool_call_id: str,
        request: KernelModelExecutionRequest,
        canonical_facts: FrozenCanonicalCompileSnapshot,
    ):
        assert self._hooks is not None
        subject = tool_matcher_subject(
            prepared.requested_tool_name,
            resolved_remote_identity=(
                prepared.canonical_tool_name
                if prepared.canonical_tool_name != prepared.requested_tool_name
                else None
            ),
        )
        public_input = PermissionRequestInput(
            session_id=request.session_id,
            cwd=str(self._tools.snapshot_terminal_cwd()),
            model=self._hook_model_id(request),
            turn_id=turn_id,
            tool_name=subject.external_primary,
            tool_input=thaw_json(prepared.resolved_arguments),
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
            pulsara_tool_name=subject.pulsara_tool_name,
        )
        return await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                scope,
                public_input,
                PermissionRef(
                    turn_id, tool_call_id, permission_request.request_nonce
                ),
                self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, tool=subject
            ),
        )

    async def _dispatch_post_tool(
        self,
        *,
        view: FrozenHookDefinitionView,
        scope: HookDispatchScopeRef,
        prepared: PreparedResolvedToolInvocation,
        settlement: AcceptedCanonicalToolResultSettlement,
        request: KernelModelExecutionRequest,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        continuity_scope: ProviderInputContinuityScope,
    ) -> None:
        if self._hooks is None:
            return
        epoch = self._continuity.current_view(continuity_scope)
        if epoch is None:
            raise RuntimeError("PostToolUse lost its provider-input epoch")
        subject = tool_matcher_subject(
            prepared.requested_tool_name,
            resolved_remote_identity=(
                prepared.canonical_tool_name
                if prepared.canonical_tool_name != prepared.requested_tool_name
                else None
            ),
        )
        projection_input = settlement.public_projection
        item = FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.TOOL_RESULT,
            source_entry_id=settlement.result_entry_id,
            source_entry_sequence=settlement.accepted_entry_sequence,
            source_turn_id=settlement.turn_id,
            text=projection_input.canonical_body,
            tool_call_id=settlement.tool_call_id,
            tool_request_entry_id=settlement.assistant_entry_id,
            tool_result_context=projection_input.metadata,
            tool_result_body_text=projection_input.canonical_body,
            tool_result_delivery=projection_input.delivery,
        )
        projected = project_tool_result_public_value(
            item, artifact_read_available=True
        )
        causal_ref = PostToolRef(
            settlement.turn_id,
            settlement.tool_call_id,
            settlement.result_id,
            settlement.result_entry_id,
            settlement.result_state,
            epoch.epoch_nonce,
        )
        public_input = PostToolUseInput(
            session_id=request.session_id,
            cwd=str(self._tools.snapshot_terminal_cwd()),
            model=self._hook_model_id(request),
            turn_id=settlement.turn_id,
            tool_name=subject.external_primary,
            tool_use_id=settlement.tool_call_id,
            tool_input=thaw_json(settlement.public_arguments),
            tool_response=projected.value,
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
            pulsara_tool_name=subject.pulsara_tool_name,
        )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                scope,
                public_input,
                causal_ref,
                self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, tool=subject
            ),
        )
        if self._hook_context is not None:
            self._hook_context.accept_sync(
                scope=scope,
                causal_ref=causal_ref,
                entries=outcome.context_entries,
            )

    async def _finish_hooked_tool_result(
        self,
        *,
        view: FrozenHookDefinitionView | None,
        scope: HookDispatchScopeRef | None,
        prepared: PreparedResolvedToolInvocation,
        settlement: AcceptedCanonicalToolResultSettlement,
        pre_context_reservation,
        request: KernelModelExecutionRequest,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        continuity_scope: ProviderInputContinuityScope,
    ) -> None:
        if pre_context_reservation is not None:
            pre_context_reservation.commit()
        if view is None or scope is None:
            return
        await self._dispatch_post_tool(
            view=view,
            scope=scope,
            prepared=prepared,
            settlement=settlement,
            request=request,
            canonical_facts=canonical_facts,
            continuity_scope=continuity_scope,
        )

    async def execute(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        calls: tuple[CompletedToolCallBlock, ...],
        canonical_facts: FrozenCanonicalCompileSnapshot,
        canonical_identity: CanonicalModelInputIdentity,
        request: KernelModelExecutionRequest,
        subagent_parent_context_subject: FrozenSubagentParentContextCallSubject | None,
        continuity_scope: ProviderInputContinuityScope,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        last_assistant_message: str | None,
        hook_scope: HookDispatchScopeRef | None = None,
    ) -> ToolBatchExecutionResult:
        tool_call_count = 0
        unsettled_process_local_effect: ProcessLocalEffectSettlementToken | None = None
        pending_hook_context_reservations = []
        pending_completion_permit: object | None = None
        try:
            report_call_count = sum(
                call.tool_name == "report_agent_result" for call in calls
            )
            if report_call_count and (len(calls) != 1 or report_call_count != 1):
                # A report is a terminal protocol choice, not an ordinary
                # sibling.  Reject the complete batch before authorization
                # or attempt admission so no physical effect can escape.
                workspace_id = await self._resolved_workspace_id()
                for call_ordinal, call in enumerate(calls):
                    tool_call_count += 1
                    binding = surface_borrow.execution_binding(call.tool_name)
                    rejected_arguments = thaw_json(call.arguments)
                    if not isinstance(rejected_arguments, dict):
                        raise RuntimeError("rejected Tool arguments are not an object")
                    rejected_view = (
                        self._hooks.capture_view()
                        if self._hooks is not None and hook_scope is not None
                        else None
                    )
                    rejected_invocation = PreparedResolvedToolInvocation(
                        call.tool_name,
                        call.tool_name,
                        call.tool_name,
                        None,
                        call.arguments,
                    )
                    rejected = await self._settle_known_tool_result(
                        session_id=request.session_id,
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        invocation_arguments=rejected_arguments,
                        call_ordinal=call_ordinal,
                        scope_kind=canonical_identity.conversation_scope_kind,
                        scope_subagent_task_id=(
                            canonical_identity.scope_subagent_task_id
                        ),
                        result_id=_id("tool-result"),
                        result_entry_id=_id("entry"),
                        attempt_id=None,
                        workspace_id=workspace_id,
                        result=KernelToolResult(
                            state="INVALID_ARGUMENTS",
                            content=(
                                "report_agent_result must be the only tool call "
                                "in its assistant response"
                            ).encode("utf-8"),
                        ),
                        observed_at=datetime.now(timezone.utc),
                        observation_origin=ToolObservationOrigin.POLICY,
                        live_sink=None,
                        tool_result_block_id=None,
                        live_attribution=None,
                        continuity_scope=continuity_scope,
                        memory_citation_visibility=MemoryCitationVisibility(
                            getattr(
                                binding,
                                "memory_citation_visibility",
                                "CURRENT_CONTEXT_BOUND",
                            )
                        ),
                        memory_citation_evidence_kind=MemoryCitationEvidenceKind(
                            getattr(
                                binding,
                                "memory_citation_evidence_kind",
                                "PRIMARY_OBSERVATION",
                            )
                        ),
                        execution_binding_fingerprint=(
                            binding.executor_binding_fingerprint
                        ),
                    )
                    if rejected_view is not None and hook_scope is not None:
                        await self._dispatch_post_tool(
                            view=rejected_view,
                            scope=hook_scope,
                            prepared=rejected_invocation,
                            settlement=rejected.settlement,
                            request=request,
                            canonical_facts=canonical_facts,
                            continuity_scope=continuity_scope,
                        )
                return ToolBatchExecutionResult(
                    tool_call_count=tool_call_count,
                )
            for call_ordinal, call in enumerate(calls):
                tool_call_count += 1
                observation_origin = ToolObservationOrigin.POLICY
                invocation_arguments = thaw_json(call.arguments)
                if not isinstance(invocation_arguments, dict):
                    raise RuntimeError(
                        "canonical tool-call arguments did not thaw as an object"
                    )
                result_entry_id = _id("entry")
                result_id = _id("tool-result")
                if surface_borrow is None:
                    raise RuntimeError("model response lost its tool surface borrow")
                binding = surface_borrow.execution_binding(call.tool_name)
                hook_view = (
                    self._hooks.capture_view()
                    if self._hooks is not None and hook_scope is not None
                    else None
                )
                prepared_invocation = (
                    self._tools.prepare_resolved_invocation(
                        tool_name=call.tool_name,
                        arguments=invocation_arguments,
                        surface_borrow=surface_borrow,
                    )
                    if hook_view is not None
                    else PreparedResolvedToolInvocation(
                        call.tool_name,
                        call.tool_name,
                        call.tool_name,
                        None,
                        call.arguments,
                    )
                )
                pre_context_reservation = None
                hook_blocked = False
                if isinstance(
                    prepared_invocation, PreparedToolPreparationRejection
                ):
                    authorization = prepared_invocation.authorization
                    post_invocation = PreparedResolvedToolInvocation(
                        prepared_invocation.requested_tool_name,
                        prepared_invocation.post_tool_name,
                        prepared_invocation.post_external_tool_name,
                        prepared_invocation.post_pulsara_tool_name,
                        prepared_invocation.post_arguments,
                    )
                else:
                    post_invocation = prepared_invocation
                    if hook_view is not None and hook_scope is not None:
                        pre_outcome, pre_causal_ref = await self._dispatch_pre_tool(
                            view=hook_view,
                            scope=hook_scope,
                            prepared=prepared_invocation,
                            turn_id=turn_id,
                            assistant_entry_id=assistant_entry_id,
                            tool_call_id=call.tool_call_id,
                            request=request,
                            canonical_facts=canonical_facts,
                        )
                        if self._hook_context is not None:
                            pre_context_reservation = (
                                self._hook_context.prepare_sync(
                                    scope=hook_scope,
                                    causal_ref=pre_causal_ref,
                                    entries=pre_outcome.context_entries,
                                )
                            )
                            if pre_context_reservation is not None:
                                pending_hook_context_reservations.append(
                                    pre_context_reservation
                                )
                        if pre_outcome.decision is GateDecision.BLOCK:
                            hook_blocked = True
                            authorization = KernelToolAuthorization(
                                KernelToolAuthorizationKind.PERMISSION_DENIED,
                                "hook:pre-tool-block",
                                pre_outcome.reason or "HOOK_BLOCKED",
                            )
                        else:
                            authorization = await self._tools.authorize(
                                tool_name=call.tool_name,
                                arguments=invocation_arguments,
                                tool_call_id=call.tool_call_id,
                                turn_id=turn_id,
                                assistant_entry_id=assistant_entry_id,
                                permission_snapshot=(
                                    canonical_facts.run_permission_snapshot
                                ),
                                surface_borrow=surface_borrow,
                                memory_context=request.memory_context,
                            )
                    else:
                        authorization = await self._tools.authorize(
                            tool_name=call.tool_name,
                            arguments=invocation_arguments,
                            tool_call_id=call.tool_call_id,
                            turn_id=turn_id,
                            assistant_entry_id=assistant_entry_id,
                            permission_snapshot=(
                                canonical_facts.run_permission_snapshot
                            ),
                            surface_borrow=surface_borrow,
                            memory_context=request.memory_context,
                        )
                machine_policy_kind = authorization.kind
                if hook_blocked:
                    # PreTool BLOCK owns a no-attempt ToolResult, not a
                    # machine permission-decision row.
                    machine_policy_kind = KernelToolAuthorizationKind.INVALID_ARGUMENTS
                capability_decision_id = _stable_id(
                    "capability-decision", assistant_entry_id, call.tool_call_id
                )
                if (
                    authorization.kind
                    is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
                ):
                    permission_request = self._tools.prepare_permission_request(
                        tool_call_id=call.tool_call_id,
                        turn_id=turn_id,
                        surface_borrow=surface_borrow,
                    )
                    permission_decision = PermissionDecision.ABSTAIN
                    if (
                        hook_view is not None
                        and hook_scope is not None
                        and isinstance(
                            prepared_invocation, PreparedResolvedToolInvocation
                        )
                    ):
                        permission_outcome = (
                            await self._dispatch_permission_request(
                                view=hook_view,
                                scope=hook_scope,
                                prepared=prepared_invocation,
                                permission_request=permission_request,
                                turn_id=turn_id,
                                tool_call_id=call.tool_call_id,
                                request=request,
                                canonical_facts=canonical_facts,
                            )
                        )
                        permission_decision = permission_outcome.decision
                    if permission_decision is PermissionDecision.ALLOW:
                        authorization = self._tools.resolve_hook_permission(
                            prepared_request=permission_request, allow=True
                        )
                    elif permission_decision is PermissionDecision.DENY:
                        authorization = self._tools.resolve_hook_permission(
                            prepared_request=permission_request, allow=False
                        )
                    else:
                        await self._io.run(
                            self._repository.accept_tool_capability_decision,
                            self._writer_lease.guard,
                            decision_id=capability_decision_id,
                            assistant_entry_id=assistant_entry_id,
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
                            prepared_request=permission_request,
                            tool_name=call.tool_name,
                            assistant_entry_id=assistant_entry_id,
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
                explicit_completion: PreparedExplicitSubagentCompletion | None = None
                if authorization.kind is KernelToolAuthorizationKind.ALLOW:
                    try:
                        advertised_binding = surface_borrow.execution_binding(
                            call.tool_name
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
                        # no-attempt result.  Consume the exact process-local
                        # FULL carrier without a repository read or second write.
                        if any(
                            value is None
                            for value in (
                                authorization.accepted_result_id,
                                authorization.accepted_result_entry_sequence,
                                authorization.accepted_result_observed_at,
                                authorization.accepted_result_public_body,
                            )
                        ):
                            raise RuntimeError(
                                "accepted human denial lost ToolResult facts"
                            )
                        human_denial = (
                            build_accepted_canonical_tool_result_settlement(
                                session_id=request.session_id,
                                scope_kind=(
                                    canonical_identity.conversation_scope_kind
                                ),
                                scope_subagent_task_id=(
                                    canonical_identity.scope_subagent_task_id
                                ),
                                turn_id=turn_id,
                                assistant_entry_id=assistant_entry_id,
                                call_ordinal=call_ordinal,
                                tool_name=post_invocation.canonical_tool_name,
                                tool_call_id=call.tool_call_id,
                                public_arguments=(
                                    post_invocation.resolved_arguments
                                ),
                                result_id=authorization.accepted_result_id,
                                result_entry_id=(
                                    authorization.accepted_result_entry_id
                                ),
                                accepted_entry_sequence=(
                                    authorization.accepted_result_entry_sequence
                                ),
                                result_state="PERMISSION_DENIED",
                                result_origin_kind="POLICY_NO_ATTEMPT",
                                canonical_body=(
                                    authorization.accepted_result_public_body
                                ),
                                observed_at=(
                                    authorization.accepted_result_observed_at
                                ),
                                observation_origin=ToolObservationOrigin.POLICY,
                            )
                        )
                        await self._finish_hooked_tool_result(
                            view=hook_view,
                            scope=hook_scope,
                            prepared=post_invocation,
                            settlement=human_denial,
                            pre_context_reservation=pre_context_reservation,
                            request=request,
                            canonical_facts=canonical_facts,
                            continuity_scope=continuity_scope,
                        )
                        if pre_context_reservation is not None:
                            pending_hook_context_reservations.remove(
                                pre_context_reservation
                            )
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
                        denial_observed_at = datetime.now(timezone.utc)
                        accepted_denial = await self._io.run(
                            self._repository.accept_tool_capability_decision,
                            self._writer_lease.guard,
                            decision_id=capability_decision_id,
                            assistant_entry_id=assistant_entry_id,
                            tool_call_id=call.tool_call_id,
                            decision="DENY",
                            authorization_reference=authorization.reference,
                            redacted_subject=f"tool:{call.tool_name}",
                            attempt_id=None,
                            result_id=result_id,
                            result_entry_id=result_entry_id,
                            denial_content=denial_content,
                            denial_result_state="PERMISSION_DENIED",
                            occurred_at=denial_observed_at,
                            actor_id="tool-dispatch-policy",
                            permission_snapshot_fingerprint=(
                                canonical_facts.run_permission_snapshot.snapshot_fingerprint
                            ),
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        if accepted_denial.result_entry_sequence is None:
                            raise RuntimeError(
                                "accepted policy denial lost its entry sequence"
                            )
                        policy_denial = (
                            build_accepted_canonical_tool_result_settlement(
                                session_id=request.session_id,
                                scope_kind=(
                                    canonical_identity.conversation_scope_kind
                                ),
                                scope_subagent_task_id=(
                                    canonical_identity.scope_subagent_task_id
                                ),
                                turn_id=turn_id,
                                assistant_entry_id=assistant_entry_id,
                                call_ordinal=call_ordinal,
                                tool_name=post_invocation.canonical_tool_name,
                                tool_call_id=call.tool_call_id,
                                public_arguments=(
                                    post_invocation.resolved_arguments
                                ),
                                result_id=result_id,
                                result_entry_id=result_entry_id,
                                accepted_entry_sequence=(
                                    accepted_denial.result_entry_sequence
                                ),
                                result_state="PERMISSION_DENIED",
                                result_origin_kind="POLICY_NO_ATTEMPT",
                                canonical_body=result.content.decode("utf-8"),
                                observed_at=denial_observed_at,
                                observation_origin=ToolObservationOrigin.POLICY,
                            )
                        )
                        await self._finish_hooked_tool_result(
                            view=hook_view,
                            scope=hook_scope,
                            prepared=post_invocation,
                            settlement=policy_denial,
                            pre_context_reservation=pre_context_reservation,
                            request=request,
                            canonical_facts=canonical_facts,
                            continuity_scope=continuity_scope,
                        )
                        if pre_context_reservation is not None:
                            pending_hook_context_reservations.remove(
                                pre_context_reservation
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
                            surface_borrow.binding_fingerprint(call.tool_name)
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
                            assistant_entry_id=assistant_entry_id,
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
                            raise RuntimeError("accepted tool attempt identity drifted")
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
                        "scope_kind": canonical_identity.conversation_scope_kind.value,
                        "scope_subagent_task_id": canonical_identity.scope_subagent_task_id,
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
                        assistant_entry_id=assistant_entry_id,
                        tool_call_id=call.tool_call_id,
                        attempt_id=attempt_id,
                        result_entry_id=result_entry_id,
                        conversation_scope_kind=(
                            canonical_identity.conversation_scope_kind.value
                        ),
                        scope_subagent_task_id=canonical_identity.scope_subagent_task_id,
                        host_owner_epoch=(self._writer_lease.guard.writer_generation),
                        authorization_reference=authorization.reference,
                        permission_snapshot_fingerprint=(
                            canonical_facts.run_permission_snapshot.snapshot_fingerprint
                        ),
                        effective_permission_mode=(
                            canonical_facts.run_permission_snapshot.effective_mode
                        ),
                        attempt_permission_snapshot_fingerprint=(
                            attempt_permission_snapshot_fingerprint
                        ),
                        subagent_parent_context_subject=(
                            subagent_parent_context_subject
                        ),
                        surface_borrow=surface_borrow,
                        memory_context=request.memory_context,
                    )
                    try:
                        if call.tool_name == "report_agent_result":
                            task_id = canonical_identity.scope_subagent_task_id
                            if task_id is None or self._subagent_runtime is None:
                                raise RuntimeError(
                                    "report_agent_result escaped its child runtime"
                                )
                            explicit_completion = await self._subagent_runtime.prepare_explicit_completion(
                                task_id=task_id,
                                result_entry_id=result_entry_id,
                                arguments=invocation_arguments,
                                last_assistant_message=last_assistant_message,
                                model_id=request.prepared_call.call.target.fact.model_id,
                                permission_snapshot=(
                                    canonical_facts.run_permission_snapshot
                                ),
                            )
                            if explicit_completion is None:
                                result = KernelToolResult(
                                    state="APPLICATION_ERROR",
                                    content=(
                                        "result payload is invalid or the task has "
                                        "pending collaboration input"
                                    ).encode("utf-8"),
                                )
                            else:
                                result = explicit_completion.tool_result
                                pending_completion_permit = (
                                    explicit_completion.permit
                                )
                        else:
                            result = await self._tools.invoke(
                                tool_name=call.tool_name,
                                arguments=invocation_arguments,
                                tool_call_id=call.tool_call_id,
                                attempt_id=attempt_id,
                                turn_id=turn_id,
                                assistant_entry_id=assistant_entry_id,
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
                if (
                    explicit_completion is not None
                    and explicit_completion.result is not None
                ):
                    permit_token = explicit_completion.permit
                    result_fact = explicit_completion.result
                    acknowledgement = explicit_completion.tool_result
                    explicit_task = asyncio.create_task(
                        self._settle_explicit_subagent_result(
                            session_id=request.session_id,
                            task_id=result_fact.task_id,
                            turn_id=turn_id,
                            assistant_entry_id=assistant_entry_id,
                            tool_call_id=call.tool_call_id,
                            attempt_id=attempt_id,
                            result_id=result_id,
                            result_entry_id=result_entry_id,
                            call_ordinal=call_ordinal,
                            scope_kind=canonical_identity.conversation_scope_kind,
                            scope_subagent_task_id=(
                                canonical_identity.scope_subagent_task_id
                            ),
                            public_tool_name=post_invocation.canonical_tool_name,
                            public_arguments=post_invocation.resolved_arguments,
                            workspace_id=workspace_id,
                            acknowledgement=acknowledgement,
                            result_fact=result_fact,
                            observed_at=outcome_observed_at,
                            observation_origin=observation_origin,
                        ),
                        name=f"kernel-explicit-subagent-result:{result_entry_id}",
                    )
                    cancellation: asyncio.CancelledError | None = None
                    while not explicit_task.done():
                        try:
                            await asyncio.shield(explicit_task)
                        except asyncio.CancelledError as exc:
                            cancellation = exc
                        except BaseException:
                            break
                    try:
                        explicit_accepted, explicit_settlement = (
                            explicit_task.result()
                        )
                    except BaseException:
                        await self._subagent_runtime.finish_completion(
                            permit_token, committed=False
                        )
                        raise
                    await self._subagent_runtime.finish_completion(
                        permit_token, committed=True
                    )
                    pending_completion_permit = None
                    await self._finish_hooked_tool_result(
                        view=hook_view,
                        scope=hook_scope,
                        prepared=post_invocation,
                        settlement=explicit_settlement,
                        pre_context_reservation=pre_context_reservation,
                        request=request,
                        canonical_facts=canonical_facts,
                        continuity_scope=continuity_scope,
                    )
                    if pre_context_reservation is not None:
                        pending_hook_context_reservations.remove(
                            pre_context_reservation
                        )
                    if cancellation is not None:
                        raise cancellation
                    return ToolBatchExecutionResult(
                        tool_call_count=tool_call_count,
                        terminal=ToolBatchTerminalResult(
                            final_entry_id=explicit_accepted.entry_id,
                            final_text=result_fact.summary,
                        ),
                    )
                settlement_task = asyncio.create_task(
                    self._settle_known_tool_result(
                        session_id=request.session_id,
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        invocation_arguments=invocation_arguments,
                        public_tool_name=post_invocation.canonical_tool_name,
                        public_arguments=post_invocation.resolved_arguments,
                        call_ordinal=call_ordinal,
                        scope_kind=canonical_identity.conversation_scope_kind,
                        scope_subagent_task_id=(
                            canonical_identity.scope_subagent_task_id
                        ),
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
                        continuity_scope=continuity_scope,
                        memory_citation_visibility=(
                            MemoryCitationVisibility(
                                getattr(
                                    binding,
                                    "memory_citation_visibility",
                                    "CURRENT_CONTEXT_BOUND",
                                )
                            )
                        ),
                        memory_citation_evidence_kind=(
                            MemoryCitationEvidenceKind.MEMORY_READ_EXPOSURE
                            if call.tool_name == "artifact_read"
                            and result.model_visible_memory_fact_ids
                            else MemoryCitationEvidenceKind(
                                getattr(
                                    binding,
                                    "memory_citation_evidence_kind",
                                    "PRIMARY_OBSERVATION",
                                )
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
                if pending_completion_permit is not None:
                    await self._subagent_runtime.finish_completion(
                        pending_completion_permit, committed=True
                    )
                    pending_completion_permit = None
                await self._finish_hooked_tool_result(
                    view=hook_view,
                    scope=hook_scope,
                    prepared=post_invocation,
                    settlement=settlement.settlement,
                    pre_context_reservation=pre_context_reservation,
                    request=request,
                    canonical_facts=canonical_facts,
                    continuity_scope=continuity_scope,
                )
                if pre_context_reservation is not None:
                    pending_hook_context_reservations.remove(
                        pre_context_reservation
                    )
                if cancellation is not None:
                    raise cancellation
                if result.caller_cancelled_while_running:
                    # Preserve the unique known result first, then honor
                    # the user/Host cancellation by interrupting the turn.
                    raise asyncio.CancelledError
            return ToolBatchExecutionResult(
                tool_call_count=tool_call_count,
            )

        finally:
            if pending_completion_permit is not None:
                try:
                    await asyncio.shield(
                        self._subagent_runtime.finish_completion(
                            pending_completion_permit, committed=False
                        )
                    )
                except BaseException:
                    pass
            for reservation in pending_hook_context_reservations:
                reservation.retire()
            surface_borrow.close()
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

    async def _settle_explicit_subagent_result(
        self,
        *,
        session_id: str,
        task_id: str,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        attempt_id: str,
        result_id: str,
        result_entry_id: str,
        call_ordinal: int,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        public_tool_name: str,
        public_arguments: FrozenJsonObjectFact,
        workspace_id: str,
        acknowledgement: KernelToolResult,
        result_fact: FrozenSubagentResultPublicFact,
        observed_at: datetime,
        observation_origin: ToolObservationOrigin,
    ) -> tuple[AcceptedEntry, AcceptedCanonicalToolResultSettlement]:
        """Settle the sole-call report result as one exact canonical composite."""

        prepared_output = await self._io.run(
            self._tool_output_processor.prepare,
            workspace_id=workspace_id,
            result_entry_id=result_entry_id,
            public_output=acknowledgement.content.decode("utf-8"),
            candidate=None,
            artifact_source_read=None,
            deadline_monotonic=self._canonical_deadline(),
        )
        tool_candidate = build_prepared_tool_result_acceptance(
            guard=self._writer_lease.guard,
            workspace_id=workspace_id,
            result_id=result_id,
            result_entry_id=result_entry_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_state="SUCCESS",
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
            observation_duration_microseconds=None,
            observation_origin_kind=observation_origin,
            trusted_tool_reported_duration_microseconds=None,
            actor_id="report_agent_result",
        )
        candidate = build_explicit_subagent_result_settlement(
            task_id=task_id,
            tool_result=tool_candidate,
            result=result_fact,
        )
        accepted: AcceptedEntry
        while True:
            try:
                accepted = await self._io.run(
                    self._repository.accept_explicit_subagent_result,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                break
            except StaleHostWriter:
                raise
            except (ConversationKernelConflict, TypeError, ValueError):
                raise
            except _RETRYABLE_EXPLICIT_RESULT_SETTLEMENT_ERRORS:
                pass
            while True:
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_explicit_subagent_result,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except StaleHostWriter:
                    raise
                except (ConversationKernelConflict, TypeError, ValueError):
                    raise
                except _RETRYABLE_EXPLICIT_RESULT_SETTLEMENT_ERRORS:
                    await asyncio.sleep(0.05)
            if confirmation.kind is ExplicitSubagentResultConfirmationKind.FULL:
                assert confirmation.accepted_entry_id is not None
                assert confirmation.entry_sequence is not None
                assert confirmation.event_sequence is not None
                accepted = AcceptedEntry(
                    entry_id=confirmation.accepted_entry_id,
                    turn_id=turn_id,
                    entry_sequence=confirmation.entry_sequence,
                    event_sequence=confirmation.event_sequence,
                    turn_completed=True,
                )
                break
            if confirmation.kind is ExplicitSubagentResultConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "explicit subagent result has a conflicting canonical winner"
                )
        canonical_body = prepared_output.canonical_preview.canonical_bytes.decode(
            "utf-8"
        )
        settlement = build_accepted_canonical_tool_result_settlement(
            session_id=session_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            call_ordinal=call_ordinal,
            tool_name=public_tool_name,
            tool_call_id=tool_call_id,
            public_arguments=public_arguments,
            result_id=result_id,
            result_entry_id=result_entry_id,
            accepted_entry_sequence=accepted.entry_sequence,
            result_state=tool_candidate.result_state,
            result_origin_kind="PHYSICAL_ATTEMPT",
            canonical_body=canonical_body,
            observed_at=tool_candidate.observed_at,
            observation_duration_microseconds=(
                tool_candidate.observation_duration_microseconds
            ),
            tool_reported_duration_microseconds=(
                tool_candidate.trusted_tool_reported_duration_microseconds
            ),
            observation_origin=tool_candidate.observation_origin_kind,
            display_kind=tool_candidate.display_kind,
            artifact_disposition=tool_candidate.artifact_disposition,
            artifact_id=tool_candidate.artifact_id,
            source_coverage=tool_candidate.source_coverage,
            source_coverage_reason=tool_candidate.source_coverage_reason,
            artifact_unavailability_reason=(
                tool_candidate.artifact_unavailability_reason
            ),
            model_visible_memory_fact_ids=(
                tool_candidate.model_visible_memory_fact_ids
            ),
        )
        return accepted, settlement

    async def _settle_known_tool_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_entry_id: str,
        tool_name: str,
        tool_call_id: str,
        invocation_arguments: Mapping[str, object],
        public_tool_name: str | None = None,
        public_arguments: FrozenJsonObjectFact | None = None,
        call_ordinal: int,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
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
            remote_identity_candidate = build_prepared_tool_remote_identity_publication(
                session_id=session_id,
                attempt_id=attempt_id,
                remote_identity=result.remote_identity,
                occurred_at=datetime.now(timezone.utc),
                actor_id=tool_name,
            )
            await self._publish_tool_remote_identity_exact(remote_identity_candidate)
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
            local_settlement = await self._tools.settle_process_local_effect(
                result.process_local_settlement,
                ProcessLocalEffectSettlementDisposition.COMMITTED,
            )
            if (
                local_settlement.outcome
                is not ProcessLocalEffectSettlementOutcome.INSTALLED
            ):
                raise RuntimeError(
                    "canonical ToolResult did not install its process-local effect"
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
        selected_arguments = public_arguments
        if selected_arguments is None:
            selected_arguments = freeze_json(dict(invocation_arguments))
        if not isinstance(selected_arguments, FrozenJsonObjectFact):
            raise TypeError("accepted Tool arguments did not freeze to an object")
        selected_tool_name = public_tool_name or tool_name
        settlement = build_accepted_canonical_tool_result_settlement(
            session_id=session_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            call_ordinal=call_ordinal,
            tool_name=selected_tool_name,
            tool_call_id=tool_call_id,
            public_arguments=selected_arguments,
            result_id=result_id,
            result_entry_id=result_entry_id,
            accepted_entry_sequence=accepted.entry_sequence,
            result_state=prepared_acceptance.result_state,
            result_origin_kind=(
                "PHYSICAL_ATTEMPT"
                if prepared_acceptance.attempt_id is not None
                else "POLICY_NO_ATTEMPT"
            ),
            canonical_body=result_text,
            observed_at=prepared_acceptance.observed_at,
            observation_duration_microseconds=(
                prepared_acceptance.observation_duration_microseconds
            ),
            tool_reported_duration_microseconds=(
                prepared_acceptance.trusted_tool_reported_duration_microseconds
            ),
            observation_origin=prepared_acceptance.observation_origin_kind,
            display_kind=prepared_acceptance.display_kind,
            artifact_disposition=prepared_acceptance.artifact_disposition,
            artifact_id=prepared_acceptance.artifact_id,
            source_coverage=prepared_acceptance.source_coverage,
            source_coverage_reason=prepared_acceptance.source_coverage_reason,
            artifact_unavailability_reason=(
                prepared_acceptance.artifact_unavailability_reason
            ),
            model_visible_memory_fact_ids=(
                prepared_acceptance.model_visible_memory_fact_ids
            ),
        )
        return _KnownToolResultSettlementOutcome(
            settlement=settlement,
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


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


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
    "ToolBatchExecutionResult",
    "ToolBatchExecutor",
    "ToolBatchTerminalResult",
]
