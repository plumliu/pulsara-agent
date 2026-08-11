"""Fresh Stage 2 foreground conversation runner.

The runner owns only one live Host activation.  It never resumes a provider,
coroutine, interaction, terminal process, or subagent execution after a crash.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from time import monotonic
from typing import AsyncIterator, Awaitable, Callable, Mapping, Protocol
from uuid import uuid4

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
from pulsara_agent.conversation_kernel.capability import (
    KernelCapabilityComposer,
    KernelCapabilityProjection,
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
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    PreparedProviderInputCut,
    PreparedToolResultAcceptance,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderInputReader,
    ProviderInputItemKind,
    RematerializedProviderInput,
)
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.tool_execution import ToolOutputArtifactCandidate


@dataclass(frozen=True, slots=True)
class KernelModelRequest:
    session_id: str
    turn_id: str
    cut: PreparedProviderInputCut
    provider_input: RematerializedProviderInput
    model_call_index: int
    system_prompt: str | None
    maximum_input_tokens: int
    maximum_output_tokens: int


class KernelModelPort(Protocol):
    def stream(
        self, request: KernelModelRequest
    ) -> AsyncIterator[ProviderStreamPayload]: ...


@dataclass(frozen=True, slots=True)
class KernelToolResult:
    state: str
    content: bytes
    memory_proposal: KernelMemoryProposal | None = None
    remote_identity: str | None = None
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    artifact_source_read: bool = False

    def __post_init__(self) -> None:
        # The model-facing tool result and artifact candidate are both strict
        # UTF-8 product contracts.  No errors="replace" lowering is allowed.
        self.content.decode("utf-8")
        if self.artifact_source_read and self.output_artifact_candidate is not None:
            raise ValueError("artifact_read cannot recursively own an artifact")


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


class KernelToolPort(Protocol):
    async def authorize(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
    ) -> KernelToolAuthorization: ...

    async def request_confirmation(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
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
    ) -> KernelToolResult: ...


@dataclass(frozen=True, slots=True)
class KernelRunResult:
    turn_id: str
    final_entry_id: str
    final_text: str
    model_call_count: int
    tool_call_count: int


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
        capability_composer: KernelCapabilityComposer | None = None,
        extensions: KernelExtensionHost | None = None,
        steer_consumer: Callable[[str, float], Awaitable[int]] | None = None,
        workspace_id: str | None = None,
        tool_output_processor: ToolOutputArtifactProcessor | None = None,
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
        self._workspace_id = workspace_id
        self._io = io_owner or KernelSessionIO()
        self._capability_composer = capability_composer
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
    ) -> KernelRunResult:
        return await self._run_turn(
            text,
            command_id=command_id,
            subagent_task_id=None,
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
        )

    async def _run_turn(
        self,
        text: str,
        *,
        command_id: str | None,
        subagent_task_id: str | None,
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
        try:
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
                    provider_input = await self._io.run(
                        self._input_reader.rematerialize,
                        prepared.cut,
                        deadline_monotonic=deadline,
                    )
                    capability_projection = (
                        await self._io.run(
                            _compose_capability_projection,
                            self._capability_composer,
                            _latest_user_input(provider_input),
                            deadline_monotonic=deadline,
                        )
                        if self._capability_composer is not None
                        else None
                    )
                    request = KernelModelRequest(
                        session_id=self._writer_lease.guard.session_id,
                        turn_id=turn_id,
                        cut=prepared.cut,
                        provider_input=provider_input,
                        model_call_index=model_call_count,
                        system_prompt=(
                            None
                            if capability_projection is None
                            else capability_projection.system_prompt
                        ),
                        maximum_input_tokens=self._maximum_input_tokens_per_call,
                        maximum_output_tokens=self._maximum_output_tokens_per_call,
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
                            cut=prepared.cut,
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
                            cut=prepared.cut,
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
                        scope_kind=provider_input.conversation_scope_kind,
                        scope_subagent_task_id=provider_input.scope_subagent_task_id,
                        channel_kind=LiveChannelKind.MODEL_OUTPUT,
                        generation_id=f"model-output:{entry_id}",
                        proposed_entry_id=entry_id,
                    )
                finally:
                    prepared.close()
                if not calls and accepted.turn_completed:
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
                    continue
                for call in calls:
                    tool_call_count += 1
                    result_entry_id = _id("entry")
                    result_id = _id("tool-result")
                    authorization = await self._tools.authorize(
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        tool_call_id=call.tool_call_id,
                        turn_id=turn_id,
                        assistant_entry_id=accepted.entry_id,
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
                            deadline_monotonic=deadline,
                        )
                        authorization = await self._tools.request_confirmation(
                            tool_name=call.tool_name,
                            tool_call_id=call.tool_call_id,
                            turn_id=turn_id,
                            assistant_entry_id=accepted.entry_id,
                        )
                    attempt_id: str | None = None
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
                                deadline_monotonic=deadline,
                            )
                            continue
                    else:
                        attempt_id = authorization.accepted_attempt_id or _id(
                            "tool-attempt"
                        )
                        # The adapter is not reachable until both the complete
                        # tool-request message and this attempt transaction return.
                        if authorization.accepted_attempt_id is None:
                            await self._io.run(
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
                                deadline_monotonic=deadline,
                            )
                        tool_result_generation = f"tool-result:{result_entry_id}"
                        tool_result_block_id = _stable_id(
                            "tool-result-block", result_entry_id, call.tool_call_id
                        )
                        live_attribution = {
                            "scope_kind": provider_input.conversation_scope_kind,
                            "scope_subagent_task_id": provider_input.scope_subagent_task_id,
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
                        try:
                            result = await self._tools.invoke(
                                tool_name=call.tool_name,
                                arguments=call.arguments,
                                tool_call_id=call.tool_call_id,
                                attempt_id=attempt_id,
                                turn_id=turn_id,
                                assistant_entry_id=accepted.entry_id,
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
                        if result_text:
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
                    if attempt_id is not None:
                        self._live_bus.offer_settlement_nowait(
                            kind=LiveSettlementKind.COMMITTED,
                            session_id=request.session_id,
                            turn_id=turn_id,
                            draft_identity=result_entry_id,
                            committed_entry_id=result_acceptance.entry_id,
                            **live_attribution,
                        )
            raise RuntimeError("model-call limit exhausted")
        except BaseException as error:
            if self._extensions is not None:
                if isinstance(error, CanonicalProviderContinuityError):
                    self._extensions.offer_operational_nowait(
                        OperationalHookOffer(
                            event_type=OperationalHookType.PROVIDER_CONTINUITY_FAILED,
                            session_id=self._writer_lease.guard.session_id,
                            turn_id=turn_id,
                            public_payload={"failure_kind": error.kind.value},
                        )
                    )
                self._extensions.offer_operational_nowait(
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
                    deadline_monotonic=deadline,
                )
            except BaseException:
                pass
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

    async def _collect_model(
        self,
        request: KernelModelRequest,
        *,
        proposed_entry_id: str,
    ) -> CompletedAssistantMessage:
        assembler = ProviderStreamAssembler(
            session_id=request.session_id,
            turn_id=request.turn_id,
            live_bus=self._live_bus,
            proposed_entry_id=proposed_entry_id,
            conversation_scope_kind=request.provider_input.conversation_scope_kind,
            scope_subagent_task_id=request.provider_input.scope_subagent_task_id,
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
                scope_kind=request.provider_input.conversation_scope_kind,
                scope_subagent_task_id=request.provider_input.scope_subagent_task_id,
                channel_kind=LiveChannelKind.MODEL_OUTPUT,
                generation_id=f"model-output:{proposed_entry_id}",
                proposed_entry_id=proposed_entry_id,
            )
            raise

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
            else:
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


def _json_digest(value: Mapping[str, object]) -> str:
    from hashlib import sha256

    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _latest_user_input(provider_input: RematerializedProviderInput) -> str:
    for item in reversed(provider_input.items):
        if item.item_kind is ProviderInputItemKind.USER:
            return item.text
    return ""


def _compose_capability_projection(
    composer: KernelCapabilityComposer,
    user_input: str,
    *,
    deadline_monotonic: float,
) -> KernelCapabilityProjection:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("capability composition deadline expired")
    return composer.compose(user_input=user_input)


__all__ = [
    "ConversationKernelRunner",
    "KernelModelPort",
    "KernelModelRequest",
    "KernelRunResult",
    "KernelToolPort",
    "KernelToolAuthorization",
    "KernelToolAuthorizationKind",
    "KernelToolResult",
]
