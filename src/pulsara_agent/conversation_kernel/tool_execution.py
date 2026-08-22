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
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementToken,
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


from pulsara_agent.primitives.tool_observation import (
    ToolObservationOrigin,
)


from pulsara_agent.conversation_kernel.vocabulary import LiveEventType


from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCanonicalCompileSnapshot,
)

from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
)


from pulsara_agent.primitives.context import (
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
    SubagentRuntimePort,
)


_RETRYABLE_EXPLICIT_RESULT_SETTLEMENT_ERRORS = (
    TimeoutError,
    ConnectionError,
    InterfaceError,
    OperationalError,
)


@dataclass(frozen=True, slots=True)
class _KnownToolResultSettlementOutcome:
    accepted: AcceptedEntry
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
    remember_requested: bool
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
    ) -> ToolBatchExecutionResult:
        tool_call_count = 0
        remember_requested = False
        unsettled_process_local_effect: ProcessLocalEffectSettlementToken | None = None
        try:
            report_call_count = sum(
                call.tool_name == "report_agent_result" for call in calls
            )
            if report_call_count and (len(calls) != 1 or report_call_count != 1):
                # A report is a terminal protocol choice, not an ordinary
                # sibling.  Reject the complete batch before authorization
                # or attempt admission so no physical effect can escape.
                workspace_id = await self._resolved_workspace_id()
                for call in calls:
                    tool_call_count += 1
                    binding = surface_borrow.execution_binding(call.tool_name)
                    await self._settle_known_tool_result(
                        session_id=request.session_id,
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        invocation_arguments={},
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
                                "WORKSPACE_BOUND",
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
                return ToolBatchExecutionResult(
                    tool_call_count=tool_call_count,
                    remember_requested=remember_requested,
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
                if surface_borrow is None:
                    raise RuntimeError("model response lost its tool surface borrow")
                binding = surface_borrow.execution_binding(call.tool_name)
                authorization = await self._tools.authorize(
                    tool_name=call.tool_name,
                    arguments=invocation_arguments,
                    tool_call_id=call.tool_call_id,
                    turn_id=turn_id,
                    assistant_entry_id=assistant_entry_id,
                    permission_snapshot=(canonical_facts.run_permission_snapshot),
                    surface_borrow=surface_borrow,
                    memory_context=request.memory_context,
                )
                machine_policy_kind = authorization.kind
                capability_decision_id = _stable_id(
                    "capability-decision", assistant_entry_id, call.tool_call_id
                )
                if (
                    authorization.kind
                    is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
                ):
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
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        turn_id=turn_id,
                        assistant_entry_id=assistant_entry_id,
                        permission_snapshot=(canonical_facts.run_permission_snapshot),
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
                explicit_completion: (
                    tuple[object, FrozenSubagentResultPublicFact, KernelToolResult]
                    | None
                ) = None
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
                            assistant_entry_id=assistant_entry_id,
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
                                result = explicit_completion[2]
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
                if explicit_completion is not None:
                    permit_token, result_fact, acknowledgement = explicit_completion
                    explicit_task = asyncio.create_task(
                        self._settle_explicit_subagent_result(
                            task_id=result_fact.task_id,
                            turn_id=turn_id,
                            assistant_entry_id=assistant_entry_id,
                            tool_call_id=call.tool_call_id,
                            attempt_id=attempt_id,
                            result_id=result_id,
                            result_entry_id=result_entry_id,
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
                        explicit_accepted = explicit_task.result()
                    except BaseException:
                        await self._subagent_runtime.finish_completion(
                            permit_token, committed=False
                        )
                        raise
                    await self._subagent_runtime.finish_completion(
                        permit_token, committed=True
                    )
                    if cancellation is not None:
                        raise cancellation
                    return ToolBatchExecutionResult(
                        tool_call_count=tool_call_count,
                        remember_requested=remember_requested,
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
                                    "WORKSPACE_BOUND",
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
                if result.memory_candidate is not None:
                    remember_requested = True
                if cancellation is not None:
                    raise cancellation
                if result.caller_cancelled_while_running:
                    # Preserve the unique known result first, then honor
                    # the user/Host cancellation by interrupting the turn.
                    raise asyncio.CancelledError
            return ToolBatchExecutionResult(
                tool_call_count=tool_call_count,
                remember_requested=remember_requested,
            )

        finally:
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
        task_id: str,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        attempt_id: str,
        result_id: str,
        result_entry_id: str,
        workspace_id: str,
        acknowledgement: KernelToolResult,
        result_fact: FrozenSubagentResultPublicFact,
        observed_at: datetime,
        observation_origin: ToolObservationOrigin,
    ) -> AcceptedEntry:
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
        while True:
            try:
                return await self._io.run(
                    self._repository.accept_explicit_subagent_result,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
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
                return AcceptedEntry(
                    entry_id=confirmation.accepted_entry_id,
                    turn_id=turn_id,
                    entry_sequence=confirmation.entry_sequence,
                    event_sequence=confirmation.event_sequence,
                    turn_completed=True,
                )
            if confirmation.kind is ExplicitSubagentResultConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "explicit subagent result has a conflicting canonical winner"
                )

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
