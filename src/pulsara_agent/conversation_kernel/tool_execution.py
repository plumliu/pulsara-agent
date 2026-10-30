"""Physical Tool batch execution and exact canonical result settlement."""

from __future__ import annotations
from pulsara_agent.identity import derive_id

from pulsara_agent.identity import random_id

from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate

import asyncio
from contextvars import ContextVar
from .tool_invocations import ToolInvocationSource, ToolInvocationSourceKind, current_tool_invocation
from .tool_surface import CodeModeToolDirectory
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.ports.session_content import SessionContentRange
from .memory.contracts import FrozenModelCallMemoryContext
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint, encode_tool_public_json

from dataclasses import dataclass, replace

from datetime import datetime, timezone

import json


from threading import Lock

from typing import Callable, Mapping


from psycopg import Error as PostgresError


from pulsara_agent.conversation_kernel.assembler import (
    CompletedToolCallBlock,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    frozen_tool_result_public_text,
)


from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
)




from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
)


from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)

from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    InlineContent,
    WriterLease,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_CODEC,
    PROMPT_BODY_MEDIA_TYPE,
    freeze_canonical_prompt,
)

from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
    LiveSettlementKind,
)

from pulsara_agent.conversation_kernel.io import KernelSessionIO, KernelSessionIOClosed
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS

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


from pulsara_agent.conversation_kernel.tool_artifacts import (
    ToolOutputArtifactProcessor,
)

from pulsara_agent.conversation_kernel.tool_contracts import (
    AcceptedCanonicalToolResultSettlement,
    FrozenAgentInvocationSnapshot,
    FrozenImageToolResourceAllowance,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementToken,
    PreparedResolvedToolInvocation,
    PreparedToolExecutionCall,
    PreparedPermissionRequest,
    PreparedToolPreparationRejection,
    build_accepted_canonical_tool_result_settlement,
)

from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AcceptedMemoryToolResult,
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
from pulsara_agent.tools.builtins.filesystem import parse_view_image_source


from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage


from pulsara_agent.conversation_kernel.vocabulary import LiveEventType


from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCanonicalCompileSnapshot,
    ModelInputScopeKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
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
    BuiltinExecutionPolicyRef,
    McpEffectKind,
    McpToolExecutionPolicyFact,
    PreparedToolExecutionBinding,
    ProcessLocalToolSurfaceBorrow,
    tool_observation_origin_for_binding,
)


from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolInvocationPort,
)
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.contracts import (
    GateDecision,
    FrozenHookDefinitionView,
    HookEventType,
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
from pulsara_agent.hooks.matcher import (
    definition_matches,
    event_matcher_subject,
    tool_matcher_subject,
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
class ToolBatchExecutionResult:
    tool_call_count: int


@dataclass(frozen=True, slots=True)
class CodeModeToolRequest:
    session_id: str
    model_id: str
    input_modalities: tuple[str, ...] | None
    memory_context: FrozenModelCallMemoryContext


@dataclass(frozen=True, slots=True)
class CodeModeCallFacts:
    run_permission_snapshot: FrozenRunPermissionSnapshot
    session_content_range: SessionContentRange


@dataclass(frozen=True, slots=True)
class CodeModeExecutionSnapshot:
    turn_id: str
    canonical_facts: CodeModeCallFacts
    canonical_identity: CanonicalModelInputIdentity
    request: CodeModeToolRequest
    agent_invocation_snapshot: FrozenAgentInvocationSnapshot | None
    continuity_scope: ProviderInputContinuityScope
    tool_directory: CodeModeToolDirectory
    hook_scope: HookDispatchScopeRef | None
    hook_epoch_nonce: object


_tool_hook_epoch: ContextVar[object | None] = ContextVar("tool_invocation_hook_epoch", default=None)
_code_snapshot: ContextVar[CodeModeExecutionSnapshot | None] = ContextVar("tool_code_snapshot", default=None)
_code_result: ContextVar[list | None] = ContextVar("tool_code_result", default=None)
_code_invocation: ContextVar[object | None] = ContextVar("code_mode_current_invocation", default=None)
_batch_reservations: ContextVar[list | None] = ContextVar("code_mode_batch_hook_reservations", default=None)


class ToolBatchExecutor:
    """Own authorize → attempt → physical invoke → exact result settlement."""

    @staticmethod
    def _session_content_range(facts):
        from pulsara_agent.ports.session_content import session_content_range
        return facts.session_content_range if isinstance(facts, CodeModeCallFacts) else session_content_range(facts)

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
        memory_projection: MemoryContextProjectionPort | None,
        extensions: KernelExtensionHost | None,
        workspace_resolver: SessionWorkspaceResolver,
        deadline_factory: KernelExecutionDeadlineFactory,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
        workspace_gate: WorkspaceExecutionGate | None = None,
    ) -> None:
        self._workspace_gate = workspace_gate
        self._repository = repository
        self._writer_lease = writer_lease
        self._tools = tools
        self._live_bus = live_bus
        self._io = io_owner
        self._content_publisher = content_publisher
        self._tool_output_processor = tool_output_processor
        self._continuity = continuity_owner
        self._memory_projection = memory_projection
        self._extensions = extensions
        self._workspace_resolver = workspace_resolver
        self._deadlines = deadline_factory
        self._hooks = hook_dispatcher
        self._hook_context = hook_context_owner
        self._admission_lock = asyncio.Lock()
        bind = getattr(tools, "bind_code_mode_executor", None)
        if bind is not None:
            bind(invoke=self.invoke_code_mode, freeze_source=self.freeze_code_mode_source,
                 archive_output=self.archive_code_mode_output,
                 read_source=self.read_code_mode_source)

    def freeze_code_mode_context(self, **values) -> CodeModeExecutionSnapshot:
        epoch = self._continuity.current_view(values["continuity_scope"])
        if epoch is None:
            raise RuntimeError("exec_cell admission requires its actual provider context")
        request = values.pop("request")
        facts = values.pop("canonical_facts")
        target = request.prepared_call.call.target.fact
        prepared = values.pop("prepared_surface")
        return CodeModeExecutionSnapshot(**values, tool_directory=CodeModeToolDirectory.freeze(prepared),
            request=CodeModeToolRequest(request.session_id, target.model_id,
                target.input_modalities, request.memory_context),
            canonical_facts=CodeModeCallFacts(facts.run_permission_snapshot, self._session_content_range(facts)),
            hook_epoch_nonce=epoch.epoch_nonce)

    async def freeze_code_mode_source(self, *, attempt_id: str, source: str) -> None:
        content = await self._content(source.encode("utf-8"), deadline=self._canonical_deadline(),
                                      media_type="text/javascript")
        await self._io.run(self._repository.freeze_code_mode_source,
            self._writer_lease.guard, attempt_id=attempt_id, content=content,
            deadline_monotonic=self._canonical_deadline())

    async def read_code_mode_source(self, *, session_id: str, attempt_id: str) -> str | None:
        return await self._io.run(
            self._repository.read_code_mode_source,
            session_id=session_id,
            attempt_id=attempt_id,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def archive_code_mode_output(self, *, context, record):
        from pulsara_agent.ports.tool_execution import ToolOutputArtifactCandidate
        text = encode_tool_public_json(dict(record)).decode("utf-8")
        prepared = await self._io.run(self._tool_output_processor.prepare,
            session_id=context.session_id, workspace_id=context.workspace_id,
            result_entry_id=context.result_entry_id, public_output=text,
            candidate=ToolOutputArtifactCandidate(role="OUTPUT", text=text,
                source_coverage=ToolOutputSourceCoverage.COMPLETE,
                source_format_hint=ToolOutputSourceFormatHint.TEXT),
            artifact_inline_result=False, deadline_monotonic=self._canonical_deadline())
        if prepared.artifact_id is None:
            from pulsara_agent.code_mode.contracts import CodeModeError
            raise CodeModeError("OUTPUT_ARTIFACT_UNAVAILABLE", str(prepared.artifact_unavailability_reason))
        return prepared

    async def invoke_code_mode(self, invocation):
        """Run one admitted program call through the native execution leaf."""
        from pulsara_agent.code_mode.contracts import CodeModeToolResult
        snapshot = invocation.snapshot
        if not isinstance(snapshot, CodeModeExecutionSnapshot):
            raise TypeError("Code Mode requires its source exec snapshot")
        arguments = freeze_json(invocation.arguments)
        if not isinstance(arguments, FrozenJsonObjectFact):
            raise TypeError("Code Mode tool arguments must be an object")
        call_id = derive_id("call", "code-mode-invocation", invocation.source_exec_attempt_id, str(invocation.sequence))
        source = ToolInvocationSource(
            invocation_id=call_id, kind=ToolInvocationSourceKind.CODE_MODE_CALL,
            turn_id=snapshot.turn_id, source_exec_attempt_id=invocation.source_exec_attempt_id,
            call_sequence=invocation.sequence, tool_name=invocation.tool_name,
            original_arguments=arguments,
        )
        borrow = self._tools.borrow_code_mode_surface(snapshot.tool_directory,
            cell_id=invocation.cell_id, tool_name=invocation.tool_name)
        captured = []
        source_token = current_tool_invocation.set(source)
        snapshot_token = _code_snapshot.set(snapshot)
        result_token = _code_result.set(captured)
        invocation_token = _code_invocation.set(invocation)
        reservations = []
        reservations_token = _batch_reservations.set(reservations)
        try:
            await self.execute(
                turn_id=snapshot.turn_id, assistant_entry_id=None,
                calls=(PreparedToolExecutionCall(call_id, invocation.tool_name, arguments),),
                canonical_facts=snapshot.canonical_facts,
                canonical_identity=snapshot.canonical_identity, request=snapshot.request,
                agent_invocation_snapshot=snapshot.agent_invocation_snapshot,
                continuity_scope=snapshot.continuity_scope, surface_borrow=borrow,
                hook_scope=snapshot.hook_scope,
                _partition_batch=False,
            )
            if len(captured) != 1:
                raise RuntimeError("program invocation did not settle exactly one result")
            result, settlement = captured[0]
            metadata = settlement.public_projection.metadata
            artifact = None
            if metadata.artifact_disposition is not ToolOutputArtifactDisposition.NOT_REQUIRED:
                artifact = {"disposition": metadata.artifact_disposition.value,
                    "source_coverage": metadata.source_coverage.value}
                if metadata.artifact_id is not None:
                    artifact["target"] = {"kind": "ARTIFACT", "artifact_id": metadata.artifact_id}
                if metadata.source_coverage_reason is not None:
                    artifact["source_coverage_reason"] = metadata.source_coverage_reason.value
                if metadata.artifact_unavailability_reason is not None:
                    artifact["reason"] = metadata.artifact_unavailability_reason.value
            value = result.public_value()
            async def delivered():
                callback = result.delivery_succeeded
                if callback is not None:
                    returned = await asyncio.to_thread(callback)
                    if returned is not None:
                        await returned
                install = getattr(self._tools, "install_code_cell_tool_result_delivery", None)
                if install is not None:
                    install(result_entry_id=settlement.result_entry_id,
                            cell_id=invocation.cell_id, borrow=borrow)
            accepted_result_id = settlement.result_id
            accepted_session_id = snapshot.request.session_id
            accepted_sequence = invocation.sequence
            accepted_tool_name = invocation.tool_name
            async def read_undelivered():
                payload = await self._io.run(self._repository.read_accepted_tool_result_observation,
                    session_id=accepted_session_id, result_id=accepted_result_id,
                    deadline_monotonic=self._canonical_deadline())
                return {"sequence": accepted_sequence, "tool_name": accepted_tool_name, **payload}
            success = settlement.result_state == "SUCCESS"
            failure_message = (value.get("message") or value.get("error") or settlement.result_state) if isinstance(value, dict) else value
            return CodeModeToolResult(value=value, state=settlement.result_state,
                result_id=settlement.result_id, result_entry_id=settlement.result_entry_id,
                artifact=artifact, error_code=None if success else settlement.result_state,
                error_message=None if success else str(failure_message),
                delivery_succeeded=delivered, read_undelivered=read_undelivered)
        finally:
            _code_result.reset(result_token)
            _code_invocation.reset(invocation_token)
            for reservation in reservations:
                reservation.retire()
            _batch_reservations.reset(reservations_token)
            _code_snapshot.reset(snapshot_token)
            current_tool_invocation.reset(source_token)
            borrow.close()

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

    @staticmethod
    def _hook_input_origin() -> str:
        source = current_tool_invocation.get()
        if source is None:
            raise RuntimeError("tool Hook requires its actual invocation source")
        return source.kind.value

    def _hook_model_id(self, request: KernelModelExecutionRequest) -> str:
        return request.model_id if isinstance(request, CodeModeToolRequest) else request.prepared_call.call.target.fact.model_id

    async def _dispatch_batch_pre_tool(self, *, view, scope, prepared, turn_id,
                                      assistant_entry_id, tool_call_id, request, canonical_facts):
        from pulsara_agent.hooks.contracts import GateOutcome
        from .subagents.collaboration_contracts import parse_batch_members, validate_batch_identities, agent_target, work_target
        arguments = thaw_json(prepared.resolved_arguments)
        members = parse_batch_members(arguments)
        peers = {key: {"agent": agent_target(member.agent_id), "work": work_target(member.work_id)}
                 for key, member in members}
        diagnostics = []
        blocked = None
        for (key, _), concrete in zip(members, arguments["members"]):
            frozen = freeze_json(concrete["parameters"])
            member_prepared = PreparedResolvedToolInvocation("run_agent", "run_agent", frozen)
            outcome, causal = await self._dispatch_pre_tool(
                view=view, scope=scope, prepared=member_prepared, turn_id=turn_id,
                assistant_entry_id=assistant_entry_id, tool_call_id=tool_call_id,
                request=request, canonical_facts=canonical_facts, _member_key=key)
            diagnostics.extend(outcome.diagnostics)
            if self._hook_context is not None:
                reservation = self._hook_context.prepare_sync(scope=scope, causal_ref=causal,
                    entries=outcome.context_entries)
                if reservation is not None:
                    _batch_reservations.get().append(reservation)
            if outcome.updated_input is not None:
                concrete["parameters"] = outcome.updated_input
            if outcome.decision is GateDecision.BLOCK:
                blocked = outcome.reason or "HOOK_BLOCKED"
                break
        if blocked is None:
            try:
                validate_batch_identities(arguments, peers)
            except ValueError as error:
                blocked = str(error)
        causal = PreToolRef(turn_id, assistant_entry_id, tool_call_id, "run_agent.batch",
            invocation_id=current_tool_invocation.get().invocation_id)
        return GateOutcome(decision=GateDecision.BLOCK if blocked is not None else GateDecision.PROCEED,
            reason=blocked, diagnostics=tuple(diagnostics), updated_input=arguments), causal

    async def _dispatch_pre_tool(
        self,
        *,
        view: FrozenHookDefinitionView,
        scope: HookDispatchScopeRef,
        prepared: PreparedResolvedToolInvocation,
        turn_id: str,
        assistant_entry_id: str | None,
        tool_call_id: str,
        request: KernelModelExecutionRequest,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        _member_key: str | None = None,
    ):
        assert self._hooks is not None
        if prepared.requested_tool_name == "run_agent.batch":
            return await self._dispatch_batch_pre_tool(view=view, scope=scope, prepared=prepared,
                turn_id=turn_id, assistant_entry_id=assistant_entry_id, tool_call_id=tool_call_id,
                request=request, canonical_facts=canonical_facts)
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
            invocation_id=(current_tool_invocation.get().invocation_id if current_tool_invocation.get() else None),
            member_key=_member_key,
        )
        public_input = PreToolUseInput(
            session_id=request.session_id,
            session_kind=scope.kind.value,
            input_origin=self._hook_input_origin(),
            cwd=str(self._tools.snapshot_workspace_root()),
            model=self._hook_model_id(request),
            turn_id=turn_id,
            tool_name=subject.canonical_subject,
            tool_use_id=tool_call_id,
            tool_input=arguments,
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
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
            session_kind=scope.kind.value,
            input_origin=self._hook_input_origin(),
            cwd=str(self._tools.snapshot_workspace_root()),
            model=self._hook_model_id(request),
            turn_id=turn_id,
            tool_name=subject.canonical_subject,
            tool_input=thaw_json(prepared.resolved_arguments),
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
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
        _member_key: str | None = None,
        _member_response: object = None,
    ) -> None:
        if self._hooks is None:
            return
        if prepared.requested_tool_name == "run_agent.batch":
            from .subagents.collaboration_contracts import parse_batch_members
            values = _code_result.get()
            business = values[0][0].public_value() if values else None
            for key, member in parse_batch_members(thaw_json(prepared.resolved_arguments)):
                member_args = freeze_json(next(item["parameters"] for item in thaw_json(prepared.resolved_arguments)["members"] if item["key"] == key))
                try:
                    await self._dispatch_post_tool(view=view, scope=scope,
                        prepared=PreparedResolvedToolInvocation("run_agent", "run_agent", member_args),
                        settlement=replace(settlement, tool_name="run_agent", public_arguments=member_args),
                        request=request, canonical_facts=canonical_facts, continuity_scope=continuity_scope,
                        _member_key=key, _member_response=business.get(key) if isinstance(business, dict) else business)
                except Exception as error:
                    self._offer_operational_best_effort(OperationalHookOffer(
                        event_type=OperationalHookType.TOOL_INVOCATION_OBSERVED, session_id=request.session_id,
                        turn_id=settlement.turn_id, public_payload={"tool_name":"run_agent", "member_key":key,
                            "outcome":"POST_TOOL_HOOK_FAILED", "diagnostic":str(error)}))
            return
        snapshot = _code_snapshot.get()
        epoch_nonce = snapshot.hook_epoch_nonce if snapshot is not None else _tool_hook_epoch.get()
        if epoch_nonce is None:
            raise RuntimeError("PostToolUse lost its invocation context")
        subject = tool_matcher_subject(
            prepared.requested_tool_name,
            resolved_remote_identity=(
                prepared.canonical_tool_name
                if prepared.canonical_tool_name != prepared.requested_tool_name
                else None
            ),
        )
        captured = _code_result.get()
        if snapshot is not None:
            response_value = (captured[0][0].public_value() if captured
                              else settlement.public_projection.canonical_body)
        else:
            projection_input = settlement.public_projection
            item = FrozenProviderInputItem(
                item_kind=FrozenProviderInputItemKind.TOOL_RESULT,
                source_entry_id=settlement.result_entry_id,
                source_entry_sequence=settlement.accepted_entry_sequence,
                source_turn_id=settlement.turn_id,
                content=(LLMTextPart(projection_input.canonical_body),),
                tool_call_id=settlement.tool_call_id,
                tool_request_entry_id=settlement.assistant_entry_id,
                tool_result_context=projection_input.metadata,
                tool_result_body_text=projection_input.canonical_body,
                tool_result_delivery=projection_input.delivery,
            )
            projected = project_tool_result_public_value(
                item, artifact_read_available=True
            )
            response_value = projected.value
        causal_ref = PostToolRef(
            settlement.turn_id,
            settlement.tool_call_id,
            settlement.result_id,
            settlement.result_entry_id,
            settlement.result_state,
            epoch_nonce,
            invocation_id=(current_tool_invocation.get().invocation_id if current_tool_invocation.get() else None),
            member_key=_member_key,
        )
        public_input = PostToolUseInput(
            session_id=request.session_id,
            session_kind=scope.kind.value,
            input_origin=self._hook_input_origin(),
            cwd=str(self._tools.snapshot_workspace_root()),
            model=self._hook_model_id(request),
            turn_id=settlement.turn_id,
            tool_name=subject.canonical_subject,
            tool_use_id=settlement.tool_call_id,
            tool_input=thaw_json(settlement.public_arguments),
            tool_response=_member_response if _member_key is not None else response_value,
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
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
        reservations = _batch_reservations.get()
        if reservations:
            for reservation in reservations:
                reservation.commit()
            reservations.clear()
        capture = _code_result.get()
        if capture is not None and not capture:
            capture.append((KernelToolResult(state=settlement.result_state,
                content=settlement.public_projection.canonical_body.encode("utf-8")), settlement))
        if pre_context_reservation is not None:
            pre_context_reservation.commit()
        if view is None or scope is None:
            return
        try:
            await self._dispatch_post_tool(
                view=view,
                scope=scope,
                prepared=prepared,
                settlement=settlement,
                request=request,
                canonical_facts=canonical_facts,
                continuity_scope=continuity_scope,
            )
        except Exception as error:
            source = current_tool_invocation.get()
            if source is None or source.kind is not ToolInvocationSourceKind.CODE_MODE_CALL:
                raise
            # The accepted business result is already canonical. A later Hook
            # failure cannot turn that effect into an unexecuted JS rejection.
            self._offer_operational_best_effort(OperationalHookOffer(
                event_type=OperationalHookType.TOOL_INVOCATION_OBSERVED,
                session_id=request.session_id, turn_id=settlement.turn_id,
                public_payload={"tool_name": prepared.requested_tool_name,
                    "outcome": "POST_TOOL_HOOK_FAILED", "diagnostic": str(error)},
            ))

    @staticmethod
    def _view_call_has_ordered_hook(
        call: CompletedToolCallBlock,
        view: FrozenHookDefinitionView | None,
    ) -> bool:
        if view is None:
            return False
        subject = event_matcher_subject(
            HookEventType.PRE_TOOL_USE_EVENT,
            tool=tool_matcher_subject(call.tool_name),
        )
        return any(
            definition_matches(definition, subject)
            for event_type in (
                HookEventType.PRE_TOOL_USE_EVENT,
                HookEventType.PERMISSION_REQUEST_EVENT,
                HookEventType.POST_TOOL_USE_EVENT,
            )
            for _, definition in view.selected_definitions(event_type)
        )

    @staticmethod
    def _validate_image_call_allowances(
        *,
        calls: tuple[CompletedToolCallBlock | PreparedToolExecutionCall, ...],
        call_ordinal_offset: int,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        allowances: tuple[FrozenImageToolResourceAllowance, ...],
    ) -> None:
        expected: list[tuple[int, str, str]] = []
        for call_ordinal, call in enumerate(calls, start=call_ordinal_offset):
            if call.tool_name != "view_image":
                continue
            try:
                binding = surface_borrow.execution_binding(call.tool_name)
            except KeyError:
                continue
            arguments = thaw_json(call.arguments)
            if not isinstance(binding, PreparedToolExecutionBinding) or not isinstance(
                binding.execution_policy, BuiltinExecutionPolicyRef
            ) or not isinstance(arguments, dict):
                continue
            try:
                parse_view_image_source(arguments)
            except ValueError:
                continue
            expected.append(
                (
                    call_ordinal,
                    call.tool_call_id,
                    binding.executor_binding_fingerprint,
                )
            )
        actual = [
            (
                item.call_ordinal,
                item.tool_call_id,
                item.executor_binding_fingerprint,
            )
            for item in allowances
        ]
        if actual != expected:
            raise RuntimeError("image Tool resource allowances do not exact-join calls")

    async def execute(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str | None,
        calls: tuple[CompletedToolCallBlock | PreparedToolExecutionCall, ...],
        canonical_facts: FrozenCanonicalCompileSnapshot,
        canonical_identity: CanonicalModelInputIdentity,
        request: KernelModelExecutionRequest,
        agent_invocation_snapshot: FrozenAgentInvocationSnapshot | None,
        continuity_scope: ProviderInputContinuityScope,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        hook_scope: HookDispatchScopeRef | None = None,
        image_call_allowances: tuple[FrozenImageToolResourceAllowance, ...] = (),
        _partition_batch: bool = True,
        _call_ordinal_offset: int = 0,
        _close_surface_borrow: bool = True,
        _settlement_gate: asyncio.Event | None = None,
        _settlement_release: asyncio.Event | None = None,
        _physical_complete: Callable[[], None] | None = None,
        _preauthorization: tuple[
            PreparedResolvedToolInvocation | PreparedToolPreparationRejection,
            KernelToolAuthorization,
        ]
        | None = None,
    ) -> ToolBatchExecutionResult:
        tool_call_count = 0
        admission_held = False
        source_context_token = None
        unsettled_process_local_effect: ProcessLocalEffectSettlementToken | None = None
        pending_hook_context_reservations = []
        source = current_tool_invocation.get()
        code_call = source is not None and source.kind is ToolInvocationSourceKind.CODE_MODE_CALL
        cell_cancel_event = (
            getattr(_code_invocation.get(), "cancelled", None) if code_call else None
        )
        hook_epoch_token = None
        if _close_surface_borrow and not code_call:
            epoch = self._continuity.current_view(continuity_scope)
            if epoch is not None:
                hook_epoch_token = _tool_hook_epoch.set(epoch.epoch_nonce)
        try:
            if (cell_cancel_event is not None and cell_cancel_event.is_set()) or (
                self._workspace_gate is not None and self._workspace_gate.observe().outcome != "AVAILABLE"
            ):
                _partition_batch = False
            if _partition_batch:
                self._validate_image_call_allowances(
                    calls=calls,
                    call_ordinal_offset=_call_ordinal_offset,
                    surface_borrow=surface_borrow,
                    allowances=image_call_allowances,
                )
            hook_view = (
                self._hooks.capture_view()
                if self._hooks is not None and hook_scope is not None
                else None
            )
            if _partition_batch and any(
                first.tool_name == "view_image"
                and second.tool_name == "view_image"
                and not self._view_call_has_ordered_hook(first, hook_view)
                and not self._view_call_has_ordered_hook(second, hook_view)
                for first, second in zip(calls, calls[1:])
            ):
                return await self._execute_partitioned_batch(
                    turn_id=turn_id,
                    assistant_entry_id=assistant_entry_id,
                    calls=calls,
                    canonical_facts=canonical_facts,
                    canonical_identity=canonical_identity,
                    request=request,
                    agent_invocation_snapshot=agent_invocation_snapshot,
                    continuity_scope=continuity_scope,
                    surface_borrow=surface_borrow,
                    image_call_allowances=image_call_allowances,
                    call_ordinal_offset=_call_ordinal_offset,
                    hook_scope=hook_scope,
                    hook_view=hook_view,
                )
            for call_ordinal, call in enumerate(
                calls, start=_call_ordinal_offset
            ):
                if not code_call:
                    if source_context_token is not None:
                        current_tool_invocation.reset(source_context_token)
                    source = ToolInvocationSource.model(request.session_id, turn_id,
                        assistant_entry_id, call.tool_call_id)
                    source_context_token = current_tool_invocation.set(source)
                await self._admission_lock.acquire()
                admission_held = True
                call_preauthorization = _preauthorization
                cell_cancelled = cell_cancel_event is not None and cell_cancel_event.is_set()
                if cell_cancelled:
                    call_preauthorization = (
                        PreparedResolvedToolInvocation(call.tool_name, call.tool_name, call.arguments),
                        KernelToolAuthorization(KernelToolAuthorizationKind.CANCELLED_BEFORE_DISPATCH,
                                                "code-mode:cell-stopped", "Cell 已停止，此工具未执行。"),
                    )
                workspace_blocked = self._workspace_gate is not None and self._workspace_gate.observe().outcome != "AVAILABLE"
                if workspace_blocked:
                    call_preauthorization = (
                        PreparedResolvedToolInvocation(call.tool_name, call.tool_name, call.arguments),
                        KernelToolAuthorization(KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                                               "workspace:unavailable", "工作目录不可用，此工具未执行。"))
                tool_call_count += 1
                observation_origin = ToolObservationOrigin.POLICY
                invocation_arguments = thaw_json(call.arguments)
                if not isinstance(invocation_arguments, dict):
                    raise RuntimeError(
                        "canonical tool-call arguments did not thaw as an object"
                    )
                result_id = random_id('result')
                result_entry_id = derive_id('entry', 'tool-result', result_id)
                if surface_borrow is None:
                    raise RuntimeError("model response lost its tool surface borrow")
                binding = None if cell_cancelled else surface_borrow.execution_binding(call.tool_name)
                hook_view = (
                    self._hooks.capture_view()
                    if not workspace_blocked and not cell_cancelled and self._hooks is not None and hook_scope is not None
                    else None
                )
                prepared_invocation = (
                    call_preauthorization[0]
                    if call_preauthorization is not None
                    else
                    self._tools.prepare_resolved_invocation(
                        tool_name=call.tool_name,
                        arguments=invocation_arguments,
                        surface_borrow=surface_borrow,
                    )
                )
                pre_context_reservation = None
                hook_blocked = False
                if call_preauthorization is not None:
                    authorization = call_preauthorization[1]
                    if (
                        prepared_invocation.requested_tool_name != call.tool_name
                        or (
                            prepared_invocation.resolved_arguments
                            if isinstance(
                                prepared_invocation, PreparedResolvedToolInvocation
                            )
                            else prepared_invocation.post_arguments
                        )
                        != call.arguments
                    ):
                        raise RuntimeError("preauthorized Tool call identity drifted")
                    post_invocation = (
                        prepared_invocation
                        if isinstance(
                            prepared_invocation, PreparedResolvedToolInvocation
                        )
                        else PreparedResolvedToolInvocation(
                            prepared_invocation.requested_tool_name,
                            prepared_invocation.post_tool_name,
                            prepared_invocation.post_arguments,
                        )
                    )
                elif isinstance(
                    prepared_invocation, PreparedToolPreparationRejection
                ):
                    authorization = prepared_invocation.authorization
                    post_invocation = PreparedResolvedToolInvocation(
                        prepared_invocation.requested_tool_name,
                        prepared_invocation.post_tool_name,
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
                        if pre_outcome.updated_input is not None and pre_outcome.decision is not GateDecision.BLOCK:
                            invocation_arguments = dict(pre_outcome.updated_input)
                            rewritten = self._tools.prepare_resolved_invocation(
                                tool_name=call.tool_name, arguments=invocation_arguments, surface_borrow=surface_borrow)
                            if isinstance(rewritten, PreparedToolPreparationRejection):
                                pre_outcome = replace(pre_outcome, decision=GateDecision.BLOCK,
                                    reason=rewritten.authorization.public_message)
                            else:
                                post_invocation = rewritten
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
                if code_call and authorization.kind in {
                    KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                    KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED,
                }:
                    authorization = KernelToolAuthorization(
                        KernelToolAuthorizationKind.PERMISSION_DENIED,
                        "code-mode:authorization-required",
                        "This background call requires authorization and was not dispatched. Use the native tool.",
                    )
                machine_policy_kind = authorization.kind
                if hook_blocked:
                    # PreTool BLOCK owns a no-attempt ToolResult, not a
                    # machine permission-decision row.
                    machine_policy_kind = KernelToolAuthorizationKind.INVALID_ARGUMENTS
                capability_decision_id = derive_id("decision", "tool-policy", self._writer_lease.guard.session_id, assistant_entry_id or "", call.tool_call_id)
                if authorization.kind is KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED:
                    management = authorization.capability_call
                    assert management is not None
                    if authorization.capability_permission_required:
                        permission_request = self._tools.prepare_permission_request(
                            tool_call_id=call.tool_call_id, turn_id=turn_id,
                            surface_borrow=surface_borrow,
                        )
                        permission_decision = PermissionDecision.ABSTAIN
                        if hook_view is not None and hook_scope is not None and isinstance(
                            prepared_invocation, PreparedResolvedToolInvocation
                        ):
                            permission_outcome = await self._dispatch_permission_request(
                                view=hook_view, scope=hook_scope, prepared=prepared_invocation,
                                permission_request=permission_request, turn_id=turn_id,
                                tool_call_id=call.tool_call_id, request=request, canonical_facts=canonical_facts,
                            )
                            permission_decision = permission_outcome.decision
                        if permission_decision is PermissionDecision.DENY:
                            management.discard()
                            authorization = KernelToolAuthorization(
                                KernelToolAuthorizationKind.PERMISSION_DENIED,
                                "hook:permission-deny", "Capability management was denied by the permission Hook")
                        elif permission_decision is PermissionDecision.ALLOW and not management.prepared.user_inputs:
                            authorization = replace(authorization, kind=KernelToolAuthorizationKind.ALLOW,
                                                    reference="hook:permission-allow")
                    if authorization.kind is KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED:
                        authorization = await self._tools.request_capability_form(
                            authorization=authorization, turn_id=turn_id,
                            assistant_entry_id=assistant_entry_id, tool_call_id=call.tool_call_id,
                            permission_snapshot=canonical_facts.run_permission_snapshot,
                            cell_cancel_event=cell_cancel_event,
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
                            cell_cancel_event=cell_cancel_event,
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
                if self._workspace_gate is not None and self._workspace_gate.observe().outcome != "AVAILABLE" and authorization.accepted_result_entry_id is None:
                    authorization = KernelToolAuthorization(
                        KernelToolAuthorizationKind.TOOL_UNAVAILABLE, "workspace:unavailable",
                        "工作目录不可用，此工具未执行。")
                if authorization.kind is not KernelToolAuthorizationKind.ALLOW:
                    if _physical_complete is not None:
                        _physical_complete()
                    if _settlement_gate is not None:
                        await _settlement_gate.wait()
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
                        if admission_held:
                            self._admission_lock.release()
                            admission_held = False
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
                        if admission_held:
                            self._admission_lock.release()
                            admission_held = False
                        continue
                    result = KernelToolResult(
                        state=authorization.kind.value,
                        content=(
                            authorization.public_message
                            or authorization.kind.value.lower().replace("_", " ")
                        ).encode("utf-8"),
                    )
                    if authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE:
                        result = replace(result, public_format=ToolOutputSourceFormatHint.JSON, content=json.dumps({
                            "error": "TOOL_UNAVAILABLE",
                            "guidance": result.content.decode("utf-8"),
                        }, ensure_ascii=False).encode("utf-8"))
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
                        if admission_held:
                            self._admission_lock.release()
                            admission_held = False
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
                        if admission_held:
                            self._admission_lock.release()
                            admission_held = False
                        continue
                else:
                    assert binding_fingerprint is not None
                    attempt_id = authorization.accepted_attempt_id or random_id('attempt')
                    if authorization.capability_user_submission:
                        if authorization.capability_call is None:
                            raise RuntimeError("user capability admission lost its call owner")
                        accepted_attempt = await self._io.run(
                            self._repository.accept_tool_attempt,
                            self._writer_lease.guard,
                            attempt_id=attempt_id, assistant_entry_id=assistant_entry_id,
                            tool_call_id=call.tool_call_id,
                            authorization_kind="human", authorization_reference=authorization.reference,
                            actor_kind="human", actor_id="capability-user-control-plane",
                            remote_idempotency_key=None, retry_of_attempt_id=None,
                            permission_snapshot_fingerprint=canonical_facts.run_permission_snapshot.snapshot_fingerprint,
                            occurred_at=datetime.now(timezone.utc), deadline_monotonic=self._canonical_deadline(),
                        )
                        authorization = replace(authorization,
                            accepted_attempt_id=accepted_attempt.attempt_id,
                            accepted_permission_snapshot_fingerprint=accepted_attempt.permission_snapshot_fingerprint)
                        attempt_permission_snapshot_fingerprint = accepted_attempt.permission_snapshot_fingerprint
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
                    tool_result_generation = derive_id("generation", "channel", "tool-result", result_entry_id)
                    tool_result_block_id = derive_id(
                        "block", "message", result_entry_id, "tool", call.tool_call_id
                    )
                    live_attribution = {
                        "scope_kind": canonical_identity.conversation_scope_kind.value,
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
                            assistant_entry_id,
                            invocation_id=(source.invocation_id if source is not None else None),
                            source_exec_attempt_id=(source.source_exec_attempt_id if source is not None else None),
                        ),
                        block_id=tool_result_block_id,
                        block_ordinal=0,
                        block_kind=LiveBlockKind.TOOL_RESULT,
                        **live_attribution,
                    )
                    terminal_streaming = call.tool_name == "exec_process" or (
                        call.tool_name == "manage_process"
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
                    image_allowance = next(
                        (
                            item
                            for item in image_call_allowances
                            if item.call_ordinal == call_ordinal
                            and item.tool_call_id == call.tool_call_id
                            and item.executor_binding_fingerprint
                            == binding.executor_binding_fingerprint
                        ),
                        None,
                    )
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
                        permission_confirmation_granted=(
                            machine_policy_kind
                            is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
                        ),
                        agent_invocation_snapshot=(
                            agent_invocation_snapshot
                        ),
                        surface_borrow=surface_borrow,
                        code_cell_id=surface_borrow.code_cell_id,
                        agent_message_cursors=(getattr(_code_invocation.get(), "agent_message_cursors", None)),
                        code_mode_snapshot=self.freeze_code_mode_context(
                            turn_id=turn_id, canonical_facts=canonical_facts,
                            canonical_identity=canonical_identity, request=request,
                            agent_invocation_snapshot=agent_invocation_snapshot,
                            continuity_scope=continuity_scope, prepared_surface=surface_borrow.prepared,
                            hook_scope=hook_scope,
                        ) if call.tool_name == "exec_cell" else None,
                        capability_call=authorization.capability_call,
                        cell_cancel_event=cell_cancel_event,
                        memory_context=request.memory_context,
                        input_modalities=(
                            request.input_modalities if isinstance(request, CodeModeToolRequest)
                            else request.prepared_call.call.target.fact.input_modalities
                        ),
                        image_resource_allowance=image_allowance,
                        session_content_range=(
                            self._session_content_range(canonical_facts)
                            if call.tool_name in SESSION_QUERY_TOOL_NAMES else None
                        ),
                    )
                    self._admission_lock.release()
                    admission_held = False
                    try:
                        if self._workspace_gate is not None and self._workspace_gate.observe().outcome != "AVAILABLE":
                            result = KernelToolResult(state="APPLICATION_ERROR", content="工作目录不可用，此工具未执行。".encode("utf-8"))
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
                        error = exc.physical_error
                        if not isinstance(error, Exception) or isinstance(
                            error,
                            (
                                StaleHostWriter,
                                ConversationKernelConflict,
                                KernelSessionIOClosed,
                                PostgresError,
                                MemoryError,
                            ),
                        ):
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
                            raise error
                        result = replace(
                            _tool_invocation_failure(error, exc.effect_class),
                            physical_timing=exc.timing,
                            caller_cancelled_while_running=exc.caller_cancelled,
                            physical_observation=exc.physical_observation,
                        )
                    except Exception as error:
                        if isinstance(error, (RuntimeError, PostgresError, MemoryError)):
                            if live_sink is not None:
                                await asyncio.shield(live_sink.close())
                            raise
                        if isinstance(binding.execution_policy, McpToolExecutionPolicyFact):
                            severity = (
                                "read_only"
                                if binding.execution_policy.effect_kind is McpEffectKind.READ_ONLY
                                else "unknown_effect"
                            )
                        else:
                            severity = _tool_effect_class(
                                call.tool_name, invocation_arguments
                            )
                        if (
                            severity not in {"read_only", "TERMINAL_OBSERVATION"}
                            and not isinstance(error, OSError)
                        ):
                            if live_sink is not None:
                                await asyncio.shield(live_sink.close())
                            raise
                        result = _tool_invocation_failure(error, severity)

                if admission_held:
                    self._admission_lock.release()
                    admission_held = False
                # Once a physical call has returned an exact outcome, this
                # process-local task owns every remaining settlement step.
                # Cancelling the turn detaches only its waiter; it cannot
                # erase the result or race a Terminal monitor token discard.
                if _physical_complete is not None:
                    _physical_complete()
                if _settlement_gate is not None:
                    await _settlement_gate.wait()
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
                        assistant_entry_id=assistant_entry_id,
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        invocation_arguments=invocation_arguments,
                        public_tool_name=post_invocation.canonical_tool_name,
                        public_arguments=post_invocation.resolved_arguments,
                        call_ordinal=call_ordinal,
                        scope_kind=canonical_identity.conversation_scope_kind,
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
                    ),
                    name=f"kernel-tool-result-settlement:{result_entry_id}",
                )
                settlement, cancellation = await _await_tool_result_settlement(
                    settlement_task
                )
                if settlement.process_local_effect_committed:
                    unsettled_process_local_effect = None
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
            if source_context_token is not None:
                current_tool_invocation.reset(source_context_token)
            if admission_held:
                self._admission_lock.release()
            if hook_epoch_token is not None:
                _tool_hook_epoch.reset(hook_epoch_token)
            if _settlement_release is not None:
                _settlement_release.set()
            for reservation in pending_hook_context_reservations:
                reservation.retire()
            if _close_surface_borrow:
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

    async def _execute_partitioned_batch(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str | None,
        calls: tuple[CompletedToolCallBlock | PreparedToolExecutionCall, ...],
        canonical_facts: FrozenCanonicalCompileSnapshot,
        canonical_identity: CanonicalModelInputIdentity,
        request: KernelModelExecutionRequest,
        agent_invocation_snapshot: FrozenAgentInvocationSnapshot
        | None,
        continuity_scope: ProviderInputContinuityScope,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        image_call_allowances: tuple[FrozenImageToolResourceAllowance, ...],
        call_ordinal_offset: int,
        hook_scope: HookDispatchScopeRef | None,
        hook_view: FrozenHookDefinitionView | None,
    ) -> ToolBatchExecutionResult:
        total = 0
        index = 0
        while index < len(calls):
            if (
                calls[index].tool_name != "view_image"
                or self._view_call_has_ordered_hook(calls[index], hook_view)
            ):
                result = await self.execute(
                    turn_id=turn_id,
                    assistant_entry_id=assistant_entry_id,
                    calls=(calls[index],),
                    canonical_facts=canonical_facts,
                    canonical_identity=canonical_identity,
                    request=request,
                    agent_invocation_snapshot=agent_invocation_snapshot,
                    continuity_scope=continuity_scope,
                    surface_borrow=surface_borrow,
                    hook_scope=hook_scope,
                    image_call_allowances=image_call_allowances,
                    _partition_batch=False,
                    _call_ordinal_offset=call_ordinal_offset + index,
                    _close_surface_borrow=False,
                )
                total += result.tool_call_count
                if result.terminal is not None:
                    return ToolBatchExecutionResult(total, result.terminal)
                index += 1
                continue
            end = index + 1
            while (
                end < len(calls)
                and calls[end].tool_name == "view_image"
                and not self._view_call_has_ordered_hook(calls[end], hook_view)
            ):
                end += 1
            segment = calls[index:end]
            allowed_calls: list[CompletedToolCallBlock] = []
            allowed_authorizations: list[
                tuple[
                    PreparedResolvedToolInvocation
                    | PreparedToolPreparationRejection,
                    KernelToolAuthorization,
                ]
            ] = []
            allowed_offset = index

            async def flush_allowed() -> ToolBatchExecutionResult | None:
                nonlocal allowed_calls, allowed_authorizations, allowed_offset
                if not allowed_calls:
                    return None
                result = await self._execute_concurrent_view_segment(
                    turn_id=turn_id,
                    assistant_entry_id=assistant_entry_id,
                    calls=tuple(allowed_calls),
                    preauthorizations=tuple(allowed_authorizations),
                    canonical_facts=canonical_facts,
                    canonical_identity=canonical_identity,
                    request=request,
                    agent_invocation_snapshot=agent_invocation_snapshot,
                    continuity_scope=continuity_scope,
                    surface_borrow=surface_borrow,
                    image_call_allowances=image_call_allowances,
                    call_ordinal_offset=call_ordinal_offset + allowed_offset,
                )
                allowed_calls = []
                allowed_authorizations = []
                return result

            for relative, call in enumerate(segment):
                invocation_arguments = thaw_json(call.arguments)
                if not isinstance(invocation_arguments, dict):
                    raise RuntimeError(
                        "canonical tool-call arguments did not thaw as an object"
                    )
                prepared = self._tools.prepare_resolved_invocation(
                    tool_name=call.tool_name,
                    arguments=invocation_arguments,
                    surface_borrow=surface_borrow,
                )
                authorization = (
                    prepared.authorization
                    if isinstance(prepared, PreparedToolPreparationRejection)
                    else await self._tools.authorize(
                        tool_name=call.tool_name,
                        arguments=invocation_arguments,
                        tool_call_id=call.tool_call_id,
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        permission_snapshot=canonical_facts.run_permission_snapshot,
                        surface_borrow=surface_borrow,
                        memory_context=request.memory_context,
                    )
                )
                if authorization.kind is KernelToolAuthorizationKind.ALLOW:
                    if not allowed_calls:
                        allowed_offset = index + relative
                    allowed_calls.append(call)
                    allowed_authorizations.append((prepared, authorization))
                    continue
                flushed = await flush_allowed()
                if flushed is not None:
                    total += flushed.tool_call_count
                result = await self.execute(
                    turn_id=turn_id,
                    assistant_entry_id=assistant_entry_id,
                    calls=(call,),
                    canonical_facts=canonical_facts,
                    canonical_identity=canonical_identity,
                    request=request,
                    agent_invocation_snapshot=agent_invocation_snapshot,
                    continuity_scope=continuity_scope,
                    surface_borrow=surface_borrow,
                    hook_scope=None,
                    image_call_allowances=image_call_allowances,
                    _partition_batch=False,
                    _call_ordinal_offset=call_ordinal_offset + index + relative,
                    _close_surface_borrow=False,
                    _preauthorization=(prepared, authorization),
                )
                total += result.tool_call_count
            flushed = await flush_allowed()
            if flushed is not None:
                total += flushed.tool_call_count
            index = end
        return ToolBatchExecutionResult(tool_call_count=total)

    async def _execute_concurrent_view_segment(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str | None,
        calls: tuple[CompletedToolCallBlock | PreparedToolExecutionCall, ...],
        preauthorizations: tuple[
            tuple[
                PreparedResolvedToolInvocation | PreparedToolPreparationRejection,
                KernelToolAuthorization,
            ],
            ...,
        ],
        canonical_facts: FrozenCanonicalCompileSnapshot,
        canonical_identity: CanonicalModelInputIdentity,
        request: KernelModelExecutionRequest,
        agent_invocation_snapshot: FrozenAgentInvocationSnapshot
        | None,
        continuity_scope: ProviderInputContinuityScope,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        image_call_allowances: tuple[FrozenImageToolResourceAllowance, ...],
        call_ordinal_offset: int,
    ) -> ToolBatchExecutionResult:
        if len(preauthorizations) != len(calls) or any(
            authorization.kind is not KernelToolAuthorizationKind.ALLOW
            for _, authorization in preauthorizations
        ):
            raise ValueError("concurrent view segment was not fully preauthorized")
        settlement_gates = tuple(asyncio.Event() for _ in range(len(calls) + 1))
        settlement_gates[0].set()
        physical_completions: asyncio.Queue[int] = asyncio.Queue()
        signalled: set[int] = set()
        tasks: dict[int, asyncio.Task[ToolBatchExecutionResult]] = {}

        def start(position: int) -> None:
            def physical_complete() -> None:
                if position in signalled:
                    return
                signalled.add(position)
                physical_completions.put_nowait(position)

            async def run() -> ToolBatchExecutionResult:
                try:
                    return await self.execute(
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        calls=(calls[position],),
                        canonical_facts=canonical_facts,
                        canonical_identity=canonical_identity,
                        request=request,
                        agent_invocation_snapshot=(
                            agent_invocation_snapshot
                        ),
                        continuity_scope=continuity_scope,
                        surface_borrow=surface_borrow,
                            hook_scope=None,
                        image_call_allowances=image_call_allowances,
                        _partition_batch=False,
                        _call_ordinal_offset=(call_ordinal_offset + position),
                        _close_surface_borrow=False,
                        _settlement_gate=settlement_gates[position],
                        _settlement_release=settlement_gates[position + 1],
                        _physical_complete=physical_complete,
                        _preauthorization=preauthorizations[position],
                    )
                finally:
                    physical_complete()

            tasks[position] = asyncio.create_task(
                run(),
                name=(
                    "kernel-view-image-call:"
                    f"{assistant_entry_id}:{call_ordinal_offset + position}"
                ),
            )

        next_position = 0
        initial = min(
            len(calls), STAGE2_LIMITS.foreground_io_hard_concurrency
        )
        for _ in range(initial):
            start(next_position)
            next_position += 1
        try:
            while next_position < len(calls):
                await physical_completions.get()
                start(next_position)
                next_position += 1
            results = []
            for position in range(len(calls)):
                results.append(await tasks[position])
        except BaseException:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            drain = asyncio.gather(*tasks.values(), return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            raise
        return ToolBatchExecutionResult(
            tool_call_count=sum(item.tool_call_count for item in results)
        )

    async def _settle_known_tool_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_entry_id: str | None,
        tool_name: str,
        tool_call_id: str,
        invocation_arguments: Mapping[str, object],
        public_tool_name: str | None = None,
        public_arguments: FrozenJsonObjectFact | None = None,
        call_ordinal: int,
        scope_kind: ModelInputScopeKind,
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
        canonical_prompt = None
        direct_memory = result.memory_mutation is not None
        if direct_memory:
            if result.state != "SUCCESS" or result.output_artifact_candidate is not None:
                raise ValueError("direct memory write cannot publish a provisional artifact")
            canonical_preview = InlineContent.from_bytes(
                b"{}", media_type="text/plain", codec="utf-8"
            )
            artifact_disposition = ToolOutputArtifactDisposition.NOT_REQUIRED
            artifact_id = None
            artifact_blob = None
            source_coverage = ToolOutputSourceCoverage.COMPLETE
            display_kind = ToolResultDisplayKind.COMPLETE
            source_coverage_reason = None
            artifact_unavailability_reason = None
            result_text = ""
        elif isinstance(result.content, FrozenPromptContent):
            if result.output_artifact_candidate is not None or result.artifact_inline_result:
                raise ValueError("image ToolResult cannot enter text artifact handling")
            canonical_prompt = freeze_canonical_prompt(result.content, workspace_id=workspace_id)
            canonical_preview = InlineContent.from_bytes(
                canonical_prompt.body,
                media_type=PROMPT_BODY_MEDIA_TYPE,
                codec=PROMPT_BODY_CODEC,
            )
            artifact_disposition = ToolOutputArtifactDisposition.NOT_REQUIRED
            artifact_id = None
            artifact_blob = None
            source_coverage = ToolOutputSourceCoverage.COMPLETE
            display_kind = ToolResultDisplayKind.COMPLETE
            source_coverage_reason = None
            artifact_unavailability_reason = None
            result_text = frozen_tool_result_public_text(canonical_prompt.content)
        else:
            prepared_output = result.prepared_output_projection or await self._io.run(
                self._tool_output_processor.prepare,
                session_id=session_id,
                workspace_id=workspace_id,
                result_entry_id=result_entry_id,
                public_output=result.content.decode("utf-8"),
                candidate=result.output_artifact_candidate,
                artifact_inline_result=result.artifact_inline_result,
                deadline_monotonic=self._canonical_deadline(),
            )
            canonical_preview = prepared_output.canonical_preview
            artifact_disposition = prepared_output.artifact_disposition
            artifact_id = prepared_output.artifact_id
            artifact_blob = prepared_output.artifact_blob
            source_coverage = prepared_output.source_coverage
            display_kind = prepared_output.display_kind
            source_coverage_reason = prepared_output.source_coverage_reason
            artifact_unavailability_reason = (
                prepared_output.artifact_unavailability_reason
            )
            result_text = canonical_preview.canonical_bytes.decode("utf-8")
            if (tool_name in {"exec_process", "manage_process"}
                and result.public_format is ToolOutputSourceFormatHint.JSON
                and result.output_artifact_candidate is not None
                and result.output_artifact_candidate.source_format_hint is ToolOutputSourceFormatHint.JSON):
                value = result.public_value()
                if isinstance(value, dict) and artifact_disposition is not ToolOutputArtifactDisposition.NOT_REQUIRED:
                    artifact = {"disposition": artifact_disposition.value,
                        "source_coverage": source_coverage.value}
                    if artifact_id is not None:
                        artifact["target"] = {"kind": "ARTIFACT", "artifact_id": artifact_id}
                    if source_coverage_reason is not None:
                        artifact["source_coverage_reason"] = source_coverage_reason.value
                    if artifact_unavailability_reason is not None:
                        artifact["reason"] = artifact_unavailability_reason.value
                    value["output_artifact"] = artifact
                    result = replace(result, content=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
                    # Native and JS expose this same public JSON value.
                    preview_value = json.loads(result_text)
                    preview_value["output_artifact"] = artifact
                    result_text = json.dumps(preview_value, ensure_ascii=False, separators=(",", ":"))
                    canonical_preview = InlineContent.from_bytes(result_text.encode("utf-8"))
        if attempt_id is not None and not direct_memory:
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
            canonical_preview_content=canonical_preview,
            canonical_prompt=canonical_prompt,
            artifact_disposition=artifact_disposition,
            artifact_id=artifact_id,
            artifact_blob_descriptor=artifact_blob,
            source_coverage=source_coverage,
            display_kind=display_kind,
            source_coverage_reason=source_coverage_reason,
            artifact_unavailability_reason=artifact_unavailability_reason,
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
            memory_mutation=result.memory_mutation,
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
        if direct_memory:
            if not isinstance(accepted, AcceptedMemoryToolResult):
                raise RuntimeError("direct memory writer returned no canonical result")
            result_text = accepted.canonical_body
            if (
                tool_name == "remember"
                and accepted.result_state == "SUCCESS"
                and json.loads(result_text).get("status") == "SAVED"
            ):
                self._tools.offer_memory_embedding_wake()
            if attempt_id is not None:
                assert tool_result_block_id is not None and live_attribution is not None
                if result_text:
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
                        accepted.result_state,
                        result_text,
                        len(result_text.encode("utf-8")),
                        live_digest(result_text),
                    ),
                    block_id=tool_result_block_id,
                    block_ordinal=0,
                    block_kind=LiveBlockKind.TOOL_RESULT,
                    **live_attribution,
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
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            call_ordinal=call_ordinal,
            tool_name=selected_tool_name,
            tool_call_id=tool_call_id,
            public_arguments=selected_arguments,
            result_id=result_id,
            result_entry_id=result_entry_id,
            accepted_entry_sequence=accepted.entry_sequence,
            result_state=(
                accepted.result_state
                if isinstance(accepted, AcceptedMemoryToolResult)
                else prepared_acceptance.result_state
            ),
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
                accepted.model_visible_memory_fact_ids
                if isinstance(accepted, AcceptedMemoryToolResult)
                else prepared_acceptance.model_visible_memory_fact_ids
            ),
            canonical_content=(
                result.content
                if isinstance(result.content, FrozenPromptContent)
                else None
            ),
        )
        capture = _code_result.get()
        if capture is not None:
            capture.append((result, settlement))
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






def _tool_invocation_failure(error: Exception, effect_class: str) -> KernelToolResult:
    """Observe an execution failure without claiming effects were rolled back."""

    read_only = effect_class in {"read_only", "TERMINAL_OBSERVATION"}
    return KernelToolResult(
        state="SYSTEM_ERROR",
        public_format=ToolOutputSourceFormatHint.JSON,
        content=json.dumps(
            {
                "error": type(error).__name__,
                "message": str(error),
                "physical_outcome": (
                    "NO_RELIABLE_OBSERVATION" if read_only else "UNKNOWN"
                ),
                "retry_performed": False,
                "guidance": (
                    "The tool could not provide a reliable result. This does not "
                    "prove the resource is absent. Use the error to choose the next step."
                    if read_only else
                    "The operation may have partially or fully executed. Do not assume "
                    "success, no changes, or rollback. Check the actual state or "
                    "idempotency before repeating it; otherwise ask the user. "
                    "You may continue with independent work."
                ),
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        effect_class=effect_class,
    )


def _tool_effect_class(
    tool_name: str,
    arguments: Mapping[str, object],
    result_effect_class: str | None = None,
) -> str:
    if result_effect_class is not None:
        return result_effect_class
    if tool_name == "manage_process":
        action = arguments.get("action")
        if action in {"poll", "wait"}:
            return "TERMINAL_OBSERVATION"
        if action in {"write", "submit", "close_stdin", "kill"}:
            return "TERMINAL_EFFECT"
        raise RuntimeError("manage_process action escaped its closed catalog")
    if tool_name == "exec_process":
        return "TERMINAL_EFFECT"
    if tool_name == "run_agent.batch":
        tool_name = "run_agent"
    elif tool_name == "wait_agent.works":
        tool_name = "wait_agent"
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
]
