"""Fresh Stage 2 foreground conversation runner.

The runner owns only one live Host activation.  It never resumes a provider,
coroutine, interaction, terminal process, or subagent execution after a crash.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from time import monotonic
from threading import Lock
from typing import AsyncIterator, Awaitable, Callable, Mapping, Protocol
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
)
from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
    PreparedKernelModelCall,
)
from pulsara_agent.conversation_kernel.contracts import CanonicalContent, WriterLease
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
    LiveSettlementKind,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
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
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PreparedToolResultAcceptance,
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
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.ports.terminal_observation import PreparedInstallationTarget
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    thaw_tool_json_object,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.diagnostics import (
    project_model_input_compile_observation,
)
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    CanonicalInputOriginKind,
    FrozenCanonicalCompileSnapshot,
    CanonicalModelInputSnapshot,
    FrozenCompiledModelInput,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE, PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    thaw_json,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)


class KernelModelPort(Protocol):
    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall: ...

    def stream(
        self, request: KernelModelExecutionRequest
    ) -> AsyncIterator[ProviderStreamPayload]: ...


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
    memory_proposal: KernelMemoryProposal | None = None
    remote_identity: str | None = None
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    artifact_source_read: bool = False
    process_local_settlement: "ProcessLocalEffectSettlementToken | None" = None

    def __post_init__(self) -> None:
        # The model-facing tool result and artifact candidate are both strict
        # UTF-8 product contracts.  No errors="replace" lowering is allowed.
        self.content.decode("utf-8")
        if self.artifact_source_read and self.output_artifact_candidate is not None:
            raise ValueError("artifact_read cannot recursively own an artifact")


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


@dataclass(frozen=True, slots=True)
class KernelMemoryProposal:
    candidate_id: str
    proposal_kind: str
    proposal_payload: Mapping[str, object]
    governance_job_id: str


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
            raise ValueError(
                "accepted attempt permission attribution is incomplete"
            )


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
        extensions: KernelExtensionHost | None = None,
        steer_consumer: Callable[[str, float], Awaitable[int]] | None = None,
        workspace_id: str | None = None,
        tool_output_processor: ToolOutputArtifactProcessor | None = None,
        plan_interactions: KernelPlanInteractionCoordinator | None = None,
        automatic_plan_continuation: AutomaticPlanContinuationPort | None = None,
        launch_permission_mode: PermissionMode = DEFAULT_PERMISSION_MODE,
        maximum_model_calls_per_turn: int = STAGE2_LIMITS.model_calls_per_turn_hard,
        maximum_input_tokens_per_call: int = STAGE2_LIMITS.provider_input_tokens_per_call_hard,
        maximum_output_tokens_per_call: int = STAGE2_LIMITS.provider_output_tokens_per_call_hard,
        operation_timeout_seconds: float = 120.0,
    ) -> None:
        if (
            min(
                maximum_model_calls_per_turn,
                maximum_input_tokens_per_call,
                maximum_output_tokens_per_call,
            )
            < 1
            or operation_timeout_seconds <= 0
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
        self._extensions = extensions
        self._steer_consumer = steer_consumer
        self._maximum_model_calls_per_turn = maximum_model_calls_per_turn
        self._maximum_input_tokens_per_call = maximum_input_tokens_per_call
        self._maximum_output_tokens_per_call = maximum_output_tokens_per_call
        self._operation_timeout_seconds = operation_timeout_seconds

    async def run_turn(
        self,
        text: str,
        *,
        command_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
    ) -> KernelRunResult:
        return await self._run_turn(
            text,
            command_id=command_id,
            subagent_task_id=None,
            requested_permission_mode=(
                requested_permission_mode or self._launch_permission_mode
            ),
        )

    async def run_subagent_turn(
        self,
        *,
        task_id: str,
        objective: str,
    ) -> KernelRunResult:
        if not task_id:
            raise ValueError("subagent task identity is required")
        return await self._run_turn(
            objective,
            command_id=None,
            subagent_task_id=task_id,
            requested_permission_mode=None,
        )

    async def _run_turn(
        self,
        text: str,
        *,
        command_id: str | None,
        subagent_task_id: str | None,
        requested_permission_mode: PermissionMode | None,
    ) -> KernelRunResult:
        if not text:
            raise ValueError("user message must be non-empty")
        deadline = monotonic() + self._operation_timeout_seconds
        if subagent_task_id is None:
            stable_command_id = command_id or _id("command")
            turn_id = _stable_id(
                "turn", self._writer_lease.guard.session_id, stable_command_id
            )
            content = await self._content(text.encode("utf-8"), deadline=deadline)
            await self._io.run(
                self._repository.start_root_turn,
                self._writer_lease.guard,
                command_id=stable_command_id,
                turn_id=turn_id,
                entry_id=_stable_id("entry", turn_id, "user"),
                context_binding_revision_id=_stable_id(
                    "context-revision", turn_id, "0"
                ),
                permission_snapshot_id=_stable_id(
                    "permission-snapshot", turn_id
                ),
                requested_permission_mode=(
                    requested_permission_mode or self._launch_permission_mode
                ),
                content=content,
                occurred_at=datetime.now(timezone.utc),
                deadline_monotonic=deadline,
            )
        else:
            turn_id = _stable_id(
                "subagent-turn",
                self._writer_lease.guard.session_id,
                subagent_task_id,
            )
            content = await self._content(text.encode("utf-8"), deadline=deadline)
            await self._io.run(
                self._repository.start_subagent_turn,
                self._writer_lease.guard,
                task_id=subagent_task_id,
                turn_id=turn_id,
                entry_id=_stable_id("entry", turn_id, "objective"),
                context_binding_revision_id=_stable_id(
                    "context-revision", turn_id, "0"
                ),
                content=content,
                occurred_at=datetime.now(timezone.utc),
                actor_id="subagent-manager",
                deadline_monotonic=deadline,
            )
        return await self.run_accepted_turn(
            turn_id,
            deadline_monotonic=deadline,
        )

    async def run_accepted_turn(
        self,
        turn_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> KernelRunResult:
        """Execute a ROOT/task turn whose user entry is already canonical."""

        deadline = deadline_monotonic or (monotonic() + self._operation_timeout_seconds)
        model_call_count = 0
        tool_call_count = 0
        active_surface_borrow: ProcessLocalToolSurfaceBorrow | None = None
        try:
            unsettled_process_local_effect: ProcessLocalEffectSettlementToken | None = (
                None
            )
            while model_call_count < self._maximum_model_calls_per_turn:
                if self._steer_consumer is not None:
                    await self._steer_consumer(turn_id, deadline)
                model_call_count += 1
                prepared = await self._io.run(
                    self._safe_point.freeze_provider_input,
                    turn_id=turn_id,
                    deadline_monotonic=deadline,
                )
                try:
                    try:
                        canonical_facts = await self._io.run(
                            self._input_reader.read_frozen_compile_snapshot,
                            prepared.cut,
                            deadline_monotonic=deadline,
                        )
                        canonical_input = canonical_facts.canonical_input
                    except TimeoutError as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.DEADLINE_EXPIRED
                        ) from exc
                    identity = canonical_input.identity
                    try:
                        tool_surface = self._tools.snapshot_tool_surface(
                            conversation_scope_kind=identity.conversation_scope_kind,
                            scope_subagent_task_id=identity.scope_subagent_task_id,
                        )
                    except Exception as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                        ) from exc
                    try:
                        prepared_call = self._model.prepare_call(
                            KernelModelPreparationRequest(
                                session_id=self._writer_lease.guard.session_id,
                                turn_id=turn_id,
                                model_call_index=model_call_count,
                                purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                                maximum_input_tokens=(
                                    self._maximum_input_tokens_per_call
                                ),
                                maximum_output_tokens=(
                                    self._maximum_output_tokens_per_call
                                ),
                                tool_surface=tool_surface,
                            )
                        )
                    except Exception as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.MODEL_TARGET_PREPARATION_FAILED
                        ) from exc
                    activation_subject, activation_text = _activation_subject(
                        canonical_input
                    )
                    try:
                        sources = await self._io.run(
                            self._context_source_collector.collect,
                            activation_subject=activation_subject,
                            activation_text=activation_text,
                            tool_surface=tool_surface.model_surface,
                            canonical_facts=canonical_facts,
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
                    if (
                        sources.registry_fingerprint
                        != self._context_source_collector.registry_fingerprint
                    ):
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                        )
                    compile_request = StructuredModelInputCompileRequest(
                        context_id=f"model-context:{uuid4().hex}",
                        model_call_index=model_call_count,
                        canonical_input=canonical_input,
                        canonical_facts=canonical_facts,
                        compile_binding=prepared_call.compile_binding,
                        sources=sources,
                    )
                    try:
                        compiled_input = await self._io.run(
                            _compile_structured_input,
                            self._compiler,
                            compile_request,
                            deadline_monotonic=deadline,
                        )
                    except StructuredModelInputCompileError:
                        raise
                    except TimeoutError as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.DEADLINE_EXPIRED
                        ) from exc
                    self._offer_compile_observation(
                        turn_id=turn_id,
                        model_call_index=model_call_count,
                        compiled=compiled_input,
                    )
                    try:
                        active_surface_borrow = self._tools.borrow_tool_surface(
                            tool_surface
                        )
                    except Exception as exc:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
                        ) from exc
                    request = KernelModelExecutionRequest(
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        model_call_index=model_call_count,
                        prepared_call=prepared_call,
                        compiled_input=compiled_input,
                        cut=prepared.cut,
                        surface_borrow=active_surface_borrow,
                    )
                    prepared.begin_model_operation()
                    entry_id = _id("entry")
                    completed = await self._collect_model(
                        request, proposed_entry_id=entry_id
                    )
                    canonical_blocks = await self._canonical_blocks(
                        completed, deadline=deadline
                    )
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
                        parent_bytes, deadline=deadline
                    )
                    occurred_at = datetime.now(timezone.utc)
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
                            deadline_monotonic=deadline,
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
                            deadline_monotonic=max(deadline, monotonic() + 5.0),
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
                    return KernelRunResult(
                        turn_id=turn_id,
                        final_entry_id=accepted.entry_id,
                        final_text=completed.public_text,
                        model_call_count=model_call_count,
                        tool_call_count=tool_call_count,
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
                        deadline=deadline,
                    )
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    if outcome.interaction_kind is PlanInteractionKind.QUESTION:
                        # The canonical answer/tool result is installed by the
                        # Host resolution command.  Human think time is not a
                        # provider/tool operation deadline.
                        deadline = monotonic() + self._operation_timeout_seconds
                        continue
                    if not outcome.origin_turn_completed:
                        # Idempotent enter_plan against the already-active
                        # workflow settles the batch but keeps this exact run.
                        deadline = monotonic() + self._operation_timeout_seconds
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
                        permission_snapshot=(
                            canonical_facts.run_permission_snapshot
                        ),
                        surface_borrow=active_surface_borrow,
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
                            deadline_monotonic=deadline,
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
                    binding_fingerprint: str | None = None
                    if authorization.kind is KernelToolAuthorizationKind.ALLOW:
                        try:
                            binding_fingerprint = (
                                active_surface_borrow.binding_fingerprint(
                                    call.tool_name
                                )
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
                                result.content, deadline=deadline
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
                                deadline_monotonic=deadline,
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
                                deadline_monotonic=deadline,
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
                        workspace_id = await self._resolved_workspace_id(deadline)
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
                                tool_surface.model_surface.surface_fingerprint
                            ),
                            executor_binding_fingerprint=binding_fingerprint,
                            surface_borrow=active_surface_borrow,
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
                            unsettled_process_local_effect = (
                                result.process_local_settlement
                            )
                        except asyncio.CancelledError:
                            self._live_bus.offer_settlement_nowait(
                                kind=LiveSettlementKind.ABORTED,
                                session_id=request.session_id,
                                turn_id=turn_id,
                                draft_identity=result_entry_id,
                                reason_code="TOOL_RESULT_CANCELLED",
                                **live_attribution,
                            )
                            raise
                        except Exception as exc:
                            result = KernelToolResult(
                                state="SYSTEM_ERROR",
                                content=(
                                    f"tool execution failed: {type(exc).__name__}"
                                ).encode("utf-8"),
                            )
                        finally:
                            if live_sink is not None:
                                await asyncio.shield(live_sink.close())
                        if (
                            attempt_id is not None
                            and result.remote_identity is not None
                        ):
                            await self._io.run(
                                self._repository.publish_tool_remote_identity,
                                self._writer_lease.guard,
                                attempt_id=attempt_id,
                                remote_identity=result.remote_identity,
                                occurred_at=datetime.now(timezone.utc),
                                actor_id=call.tool_name,
                                deadline_monotonic=deadline,
                            )
                        result_text = result.content.decode("utf-8")
                    workspace_id = await self._resolved_workspace_id(deadline)
                    prepared_output = await self._io.run(
                        self._tool_output_processor.prepare,
                        workspace_id=workspace_id,
                        result_entry_id=result_entry_id,
                        public_output=result.content.decode("utf-8"),
                        candidate=result.output_artifact_candidate,
                        artifact_source_read=result.artifact_source_read,
                        deadline_monotonic=deadline,
                    )
                    result_text = (
                        prepared_output.canonical_preview.canonical_bytes.decode(
                            "utf-8"
                        )
                    )
                    if attempt_id is not None:
                        if result_text and (live_sink is None or not live_sink.emitted):
                            self._live_bus.offer_nowait(
                                event_type=LiveEventType.TOOL_RESULT_DELTA,
                                session_id=request.session_id,
                                turn_id=turn_id,
                                draft_identity=result_entry_id,
                                payload=ToolResultDeltaPayload(
                                    tool_result_block_id, result_text
                                ),
                                block_id=tool_result_block_id,
                                block_ordinal=0,
                                block_kind=LiveBlockKind.TOOL_RESULT,
                                **live_attribution,
                            )
                        self._live_bus.offer_nowait(
                            event_type=LiveEventType.TOOL_RESULT_END,
                            session_id=request.session_id,
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
                        assistant_entry_id=accepted.entry_id,
                        tool_call_id=call.tool_call_id,
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
                        actor_id=call.tool_name,
                        occurred_at=datetime.now(timezone.utc),
                        memory_candidate_id=(
                            None
                            if result.memory_proposal is None
                            else result.memory_proposal.candidate_id
                        ),
                        memory_proposal_kind=(
                            None
                            if result.memory_proposal is None
                            else result.memory_proposal.proposal_kind
                        ),
                        memory_proposal_payload=(
                            None
                            if result.memory_proposal is None
                            else result.memory_proposal.proposal_payload
                        ),
                        memory_governance_job_id=(
                            None
                            if result.memory_proposal is None
                            else result.memory_proposal.governance_job_id
                        ),
                    )
                    result_acceptance = await self._accept_tool_result_exact(
                        prepared_acceptance,
                        deadline=deadline,
                    )
                    if result.process_local_settlement is not None:
                        await self._tools.settle_process_local_effect(
                            result.process_local_settlement,
                            ProcessLocalEffectSettlementDisposition.COMMITTED,
                        )
                        unsettled_process_local_effect = None
                    if attempt_id is not None:
                        self._live_bus.offer_settlement_nowait(
                            kind=LiveSettlementKind.COMMITTED,
                            session_id=request.session_id,
                            turn_id=turn_id,
                            draft_identity=result_entry_id,
                            committed_entry_id=result_acceptance.entry_id,
                            **live_attribution,
                        )
                active_surface_borrow.close()
                active_surface_borrow = None
            raise RuntimeError("model-call limit exhausted")
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
            try:
                await self._io.run(
                    self._repository.interrupt_turn,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    reason="FOREGROUND_EXECUTION_INTERRUPTED",
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="foreground-runner",
                    # Terminalization owns a fresh bounded physical deadline;
                    # human question wait or an expired model cycle cannot
                    # suppress canonical interruption.
                    deadline_monotonic=(
                        monotonic() + self._operation_timeout_seconds
                    ),
                )
            except BaseException:
                pass
            raise

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
                "plan-interaction", workflow_id, assistant_entry_id, selected.tool_call_id
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
            advertised_executor_binding = surface_borrow.binding_fingerprint(
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
                or advertised_spec.executor_binding_fingerprint
                != advertised_executor_binding
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
                kind is PlanToolControlKind.QUESTION
                and self._plan_interactions is None
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
            _stable_id(
                "context-revision", continuation_turn_id or "", "0"
            )
            if continuation_turn_id is not None
            else None
        )
        prepared_calls: list[PreparedPlanBatchCall] = []
        for index, call in enumerate(calls):
            selected_question = (
                apply_control
                and
                index == selected_call_index
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
                        else _stable_id("tool-result", assistant_entry_id, call.tool_call_id)
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
            workspace_id=await self._resolved_workspace_id(deadline),
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
                outcome = await self._automatic_plan_continuation(
                    candidate, deadline
                )
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
                        deadline_monotonic=max(deadline, monotonic() + 5.0),
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
            async for item in self._model.stream(request):
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
        *,
        deadline: float,
    ) -> tuple[AssistantBlock, ...]:
        result: list[AssistantBlock] = []
        for block in completed.blocks:
            if isinstance(block, CompletedTextBlock):
                result.append(
                    AssistantTextBlock(
                        block_id=block.block_id,
                        text=await self._content(
                            block.text.encode("utf-8"), deadline=deadline
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
                            deadline=deadline,
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
                    text=await self._content(b"", deadline=deadline),
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

    async def _resolved_workspace_id(self, deadline: float) -> str:
        if self._workspace_id is None:
            self._workspace_id = await self._io.run(
                self._repository.read_session_workspace_id,
                self._writer_lease.guard,
                deadline_monotonic=deadline,
            )
        return self._workspace_id

    async def _accept_tool_result_exact(
        self,
        candidate: PreparedToolResultAcceptance,
        *,
        deadline: float,
    ) -> AcceptedEntry:
        try:
            return await self._io.run(
                self._repository.accept_tool_result,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=deadline,
            )
        except Exception:
            winner = await self._io.run(
                self._repository.confirm_tool_result_winner,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=max(deadline, monotonic() + 5.0),
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
                deadline_monotonic=max(deadline, monotonic() + 5.0),
            )
        except Exception:
            winner = await self._io.run(
                self._repository.confirm_tool_result_winner,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=max(deadline, monotonic() + 5.0),
            )
            if winner is None:
                raise
            return winner


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _json_digest(value: FrozenJsonObjectFact) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


def _activation_subject(
    canonical_input: CanonicalModelInputSnapshot,
) -> tuple[CapabilityActivationSubjectKind, str]:
    if (
        canonical_input.identity.conversation_scope_kind
        is ModelInputScopeKind.SUBAGENT_TASK
    ):
        return CapabilityActivationSubjectKind.SUBAGENT_OBJECTIVE, ""
    for item in reversed(canonical_input.items):
        if (
            item.item_kind.value == "USER"
            and item.source_turn_id == canonical_input.identity.turn_id
            and item.input_origin
            in {
                CanonicalInputOriginKind.HUMAN_MESSAGE,
                CanonicalInputOriginKind.HUMAN_STEER,
            }
        ):
            return CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT, item.text
    return CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT, ""


def _compile_structured_input(
    compiler: StructuredModelInputCompiler,
    request: StructuredModelInputCompileRequest,
    *,
    deadline_monotonic: float,
) -> FrozenCompiledModelInput:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("structured model input deadline expired")
    return compiler.compile(request)


__all__ = [
    "ConversationKernelRunner",
    "KernelModelPort",
    "KernelRunResult",
    "KernelToolPort",
    "KernelToolAuthorization",
    "KernelToolAuthorizationKind",
    "KernelToolResult",
]
