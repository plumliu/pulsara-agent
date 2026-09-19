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
from typing import Awaitable, Callable, Mapping, Protocol
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
from pulsara_agent.conversation_kernel.visualization import (
    PostgresCanonicalVisualizationReadPort,
    VisualizationSource,
    VisualizationSourceKind,
    VisualizationSubscription,
    materialize_visualization_subscription,
    parse_visualization_source,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceCollectorPort,
)
from pulsara_agent.capability.render import MAX_SKILL_CATALOG_UTF8_BYTES
from pulsara_agent.conversation_kernel.prompt_content import (
    FrozenCanonicalPrompt,
    freeze_canonical_prompt,
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
    CompactionAttemptToken,
    CompactionCoordinator,
    CompactionExecutionResult,
    ModelSwitchCompactionTriggerCandidate,
    ModelSwitchDirectPrecompileDecision,
    OrdinaryPrecompileDecision,
    PreparedCompactSessionStart,
    PreparedCompactSessionStartFacts,
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
    CompletedProviderModelExecution,
    KernelModelExecutionRequest,
    PreparedKernelModelCall,
    PreparedKernelModelExecution,
    ProviderFollowupWireResourceQuote,
    quote_provider_followup_wire_resources,
)
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    LLMToolCall,
    MessageRole,
)
from pulsara_agent.model_input.lowering import image_reference_digest_part
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    provider_assistant_public_projection_fingerprint,
)
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderOutputIncompleteReason,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    _issue_new_subagent_lease_source,
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
from pulsara_agent.conversation_kernel.limits import (
    PLAN_CONTROL_RESULT_INLINE_HARD_BYTES,
    ROOT_COMPLETION_SUFFIX_BATCH_ITEMS,
    STAGE2_LIMITS,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.mcp.contracts import (
    MAXIMUM_MCP_CATALOG_FULL_BYTES,
)
from pulsara_agent.conversation_kernel.memory.citations import (
    ProcessLocalMemoryCallContextOwner,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    FrozenImageToolResourceAllowance,
    FrozenImageToolResourceIncrement,
    ToolInvocationPort,
    ToolSurfacePlanningPort,
)
from pulsara_agent.conversation_kernel.tool_surface import BuiltinExecutionPolicyRef
from pulsara_agent.conversation_kernel.subagents.runtime_port import (
    PreparedInferredSubagentCompletion,
    SubagentRuntimePort,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    MAXIMUM_RESULT_SUMMARY_UTF8_BYTES,
    MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES,
    PreparedSubagentLaunch,
    SubagentProfileKind,
    SubagentResultSource,
    SubagentTaskStatus,
    SubagentTerminalReason,
    build_subagent_completion_storage_body,
    project_subagent_completion_for_provider,
)
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.tools.builtins.filesystem import (
    ViewImageSource,
    ViewImageSourceKind,
    parse_view_image_source,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AcceptedSubagentCompletion,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    ConversationKernelConflict,
    SubagentCompletionDisposition,
    build_prepared_root_turn_intent,
    build_prepared_subagent_turn_admission,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
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
    PreparedProspectiveActiveRootInput,
    PreparedProspectiveRootDispatch,
    PreparedWireMeasurementDecision,
    ProviderDispatchCoordinator,
)
from pulsara_agent.conversation_kernel.steer_consumption import (
    PreparedSteerPlanStale,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedActiveRootInputCandidate,
    PreparedRootProviderInputCandidate,
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
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
    PreparedInstallationTarget,
    TerminalObservationInstallationAttempt,
)
from pulsara_agent.ports.user_control_feedback import (
    UserControlFeedbackInstallationAttempt,
)
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator
from pulsara_agent.model_input.compiler import (
    StructuredModelInputCompiler,
    tool_image_attachment_message,
)
from pulsara_agent.model_input.diagnostics import (
    project_model_input_compile_observation,
)
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    ContextSourceKind,
    ContextTrustClass,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
    ProviderToolResultClosureKind,
    STRUCTURED_MODEL_INPUT_LIMITS,
)
from pulsara_agent.model_input.continuity import (
    MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES,
    ProcessLocalProviderInputInstallPermit,
    ProviderInputContinuityScope,
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
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
    maximum_hook_context_provider_body_bytes,
)
from pulsara_agent.hooks.contracts import (
    ContinuationDecision,
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookEventType,
    SessionStartInput,
    SessionStartRef,
    StopInput,
    StopRef,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultLogicalMessageKind,
    conservative_tool_result_logical_message,
    provider_neutral_message_logical_bytes,
)


_MODEL_SWITCH_CONTEXT_REDUCTION_NOTICE = (
    "模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。"
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


@dataclass(frozen=True, slots=True)
class FrozenPostResponseResourceQuote:
    current_canonical_expanded_bytes: int
    actual_assistant_canonical_bytes: int
    bounded_followup_canonical_bytes: int
    canonical_upper_after: int
    current_epoch_logical_bytes: int
    actual_assistant_logical_bytes: int
    bounded_followup_logical_bytes: int
    logical_upper_after: int
    current_canonical_items: int
    bounded_followup_items: int
    item_upper_after: int
    followup_wire: ProviderFollowupWireResourceQuote | None = dataclass_field(
        default=None,
        repr=False,
    )
    image_call_allowances: tuple[FrozenImageToolResourceAllowance, ...] = (
        dataclass_field(default=(), repr=False)
    )

    def __post_init__(self) -> None:
        values = (
            self.current_canonical_expanded_bytes,
            self.actual_assistant_canonical_bytes,
            self.bounded_followup_canonical_bytes,
            self.canonical_upper_after,
            self.current_epoch_logical_bytes,
            self.actual_assistant_logical_bytes,
            self.bounded_followup_logical_bytes,
            self.logical_upper_after,
            self.current_canonical_items,
            self.bounded_followup_items,
            self.item_upper_after,
        )
        if min(values) < 0:
            raise ValueError("post-response resource quote is invalid")
        if self.canonical_upper_after != sum(values[:3]):
            raise ValueError("post-response canonical quote is inconsistent")
        if self.logical_upper_after != sum(values[4:7]):
            raise ValueError("post-response logical quote is inconsistent")
        # The accepted assistant is one item; each bounded follow-up item is
        # the exact result/closure/late occurrence counted by the reader.
        if self.item_upper_after != (
            self.current_canonical_items + 1 + self.bounded_followup_items
        ):
            raise ValueError("post-response item quote is inconsistent")
        if tuple(item.call_ordinal for item in self.image_call_allowances) != tuple(
            sorted(item.call_ordinal for item in self.image_call_allowances)
        ) or len({item.tool_call_id for item in self.image_call_allowances}) != len(
            self.image_call_allowances
        ):
            raise ValueError("post-response image allowances are not ordered")


class OutputResourceInterruption(RuntimeError):
    """A complete provider response cannot be settled before its effects."""

    def __init__(self, reason: str, quote: FrozenPostResponseResourceQuote) -> None:
        self.reason = reason
        self.quote = quote
        super().__init__(
            "provider output resource interruption: "
            f"{reason}; canonical={quote.canonical_upper_after}/"
            f"{MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES}, "
            f"logical={quote.logical_upper_after}/"
            f"{MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES}, "
            f"items={quote.item_upper_after}/"
            f"{MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS}"
        )


@dataclass(frozen=True, slots=True)
class _CollectedModelResponse:
    completed: CompletedAssistantMessage
    provider_completion: CompletedProviderModelExecution
    provider_replay: PreparedDurableProviderAssistantReplay | None = dataclass_field(
        default=None, repr=False
    )


@dataclass(frozen=True, slots=True)
class _ImageToolResourceQuoteOwner:
    request: KernelModelExecutionRequest = dataclass_field(repr=False)
    assistant: LLMMessage = dataclass_field(repr=False)
    provider_replay: PreparedDurableProviderAssistantReplay | None = dataclass_field(
        repr=False
    )
    base_suffix_messages: tuple[LLMMessage, ...] = dataclass_field(repr=False)
    base_wire: ProviderFollowupWireResourceQuote = dataclass_field(repr=False)

    def quote(
        self,
        *,
        tool_call_id: str,
        source: ViewImageSource | VisualizationSource,
        content: FrozenPromptContent,
    ) -> FrozenImageToolResourceIncrement:
        images = tuple(part for part in content.parts if isinstance(part, LLMImagePart))
        if len(images) != 1:
            raise ValueError("image Tool quote requires one validated image")
        carrier = tool_image_attachment_message(((tool_call_id, source, images[0]),))
        quoted = quote_provider_followup_wire_resources(
            request=self.request,
            actual_assistant_message=self.assistant,
            provider_replay=self.provider_replay,
            bounded_suffix_messages=(*self.base_suffix_messages, carrier),
        )
        return FrozenImageToolResourceIncrement(
            canonical_bytes=freeze_canonical_prompt(
                content
            ).resource_quote.canonical_expanded_bytes,
            logical_bytes=provider_neutral_message_logical_bytes(carrier),
            wire_bytes=(
                quoted.final_wire_utf8_bytes - self.base_wire.final_wire_utf8_bytes
            ),
            input_tokens=(
                quoted.final_wire_estimated_input_tokens
                - self.base_wire.final_wire_estimated_input_tokens
            ),
        )


_PLAN_CONTROL_TOOL_NAMES = frozenset({"enter_plan", "ask_plan_question", "exit_plan"})
_MCP_ONLY_CAPABILITY_ACTIONS = frozenset(
    {
        "ADD_LOCAL_MCP",
        "UPDATE_LOCAL_MCP",
        "REMOVE_LOCAL_MCP",
        "CONFIGURE_PLUGIN_MCP_CONNECTION",
        "AUTHORIZE_MCP",
        "CLEAR_MCP_AUTHORIZATION",
    }
)
_LONGEST_TOOL_RESULT_STATE = "CANCELLED_BEFORE_DISPATCH"
_LONGEST_TOOL_CLOSURE = (
    ProviderToolResultClosureKind.INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED
)


def _completed_assistant_semantic_message(
    completed: CompletedAssistantMessage,
) -> LLMMessage:
    text = "".join(
        block.text
        if isinstance(block, CompletedTextBlock)
        else block.data
        if isinstance(block, CompletedDataBlock)
        else ""
        for block in completed.blocks
    )
    calls = tuple(
        LLMToolCall(
            id=block.tool_call_id,
            name=block.tool_name,
            arguments=canonical_json_bytes(thaw_json(block.arguments)).decode("utf-8"),
        )
        for block in completed.blocks
        if isinstance(block, CompletedToolCallBlock)
    )
    return LLMMessage.assistant_turn(text=text or None, tool_calls=calls)


def _completed_assistant_canonical_bytes(
    completed: CompletedAssistantMessage,
) -> int:
    total = 0
    for block in completed.blocks:
        if isinstance(block, CompletedTextBlock):
            total += len(block.text.encode("utf-8"))
        elif isinstance(block, CompletedDataBlock):
            total += len(block.data.encode("utf-8"))
        elif isinstance(block, CompletedToolCallBlock):
            total += len(canonical_json_bytes(thaw_json(block.arguments)))
    return total


def _tool_result_closure_storage_text(tool_call_id: str) -> str:
    return canonical_json_bytes(
        {
            "schema_version": "provider_tool_result_closure.v1",
            "tool_call_id": tool_call_id,
            "disposition": _LONGEST_TOOL_CLOSURE.value,
        }
    ).decode("utf-8")


def _tool_result_closure_message(tool_call_id: str) -> LLMMessage:
    return LLMMessage.tool_result(
        canonical_json_bytes({"disposition": _LONGEST_TOOL_CLOSURE.value}).decode(
            "utf-8"
        ),
        tool_call_id=tool_call_id,
    )


def _ordinary_result_followup_upper(
    call: CompletedToolCallBlock,
) -> tuple[int, int, int, tuple[LLMMessage, ...]]:
    """Quote the larger result versus closure+late branch for one call."""

    late = conservative_tool_result_logical_message(
        message_kind=ToolResultLogicalMessageKind.LATE_TOOL_OUTCOME,
        tool_call_id=call.tool_call_id,
    ).message
    closure = _tool_result_closure_message(call.tool_call_id)
    logical = provider_neutral_message_logical_bytes(closure) + (
        provider_neutral_message_logical_bytes(late)
    )
    result_body = "\\" * CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
    late_storage = canonical_json_bytes(
        {
            "schema_version": "late_tool_outcome_observation.v1",
            "tool_call_id": call.tool_call_id,
            "result_state": _LONGEST_TOOL_RESULT_STATE,
            "result": result_body,
        }
    )
    closure_storage = _tool_result_closure_storage_text(call.tool_call_id).encode(
        "utf-8"
    )
    canonical = max(
        CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
        len(closure_storage) + len(late_storage),
    )
    return canonical, logical, 2, (closure, late)


def _plan_result_followup_upper(
    call: CompletedToolCallBlock,
) -> tuple[int, int, int, tuple[LLMMessage, ...]]:
    result = conservative_tool_result_logical_message(
        message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
        tool_call_id=call.tool_call_id,
    ).message
    return (
        PLAN_CONTROL_RESULT_INLINE_HARD_BYTES,
        provider_neutral_message_logical_bytes(result),
        1,
        (result,),
    )


def _entered_plan_continuation_followup_upper() -> tuple[
    int, int, int, tuple[LLMMessage, ...]
]:
    """Quote the continuation created with a successful fresh ``enter_plan``.

    The workflow id is a ``plan-workflow:`` prefix plus one SHA-256 hex digest,
    so every real id has the same encoded length.  The provider projection is
    the closed value produced by the canonical reader for ENTERED_PLAN.
    """

    workflow_id_shape = "plan-workflow:" + ("0" * 64)
    canonical = canonical_json_bytes(
        {
            "transition": "ENTERED_PLAN",
            "workflow_id": workflow_id_shape,
        }
    )
    message = LLMMessage.user(
        canonical_json_bytes(
            {
                "pulsara_plan_continuation": {
                    "status": "ACTIVE",
                    "transition": "ENTERED_PLAN",
                }
            }
        ).decode("utf-8")
    )
    return (
        len(canonical),
        provider_neutral_message_logical_bytes(message),
        1,
        (message,),
    )


def _root_completion_followup_upper(
    item_count: int = ROOT_COMPLETION_SUFFIX_BATCH_ITEMS,
) -> tuple[int, int, int, tuple[LLMMessage, ...]]:
    """Quote every completion in the existing per-safe-point FIFO batch.

    Source rows are created only through the bounded subagent task/result
    contracts.  Control characters produce the largest JSON escaping expansion
    admitted by those text contracts, so the selected failure projection also
    bounds both OpenAI wire encoders after their second JSON escape.
    """

    control_fill = "\x01"
    task_id = control_fill * 512
    dependencies = tuple(control_fill * 509 + f"{index:03d}" for index in range(16))
    profile = max(SubagentProfileKind, key=lambda item: len(item.value)).value
    projections: list[str] = []
    for reason in SubagentTerminalReason:
        body = build_subagent_completion_storage_body(
            task_id=task_id,
            task_key="a" * 64,
            label=control_fill * 256,
            display_role=control_fill * 256,
            profile=profile,
            status=SubagentTaskStatus.FAILED,
            terminal_reason=reason.value,
            terminal_public_detail=(
                control_fill * MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES
            ),
            failed_dependency_task_ids=dependencies,
            result_id=None,
            result_source=None,
            result_summary=None,
        )
        projections.append(
            project_subagent_completion_for_provider(
                body,
                source_task_id=task_id,
            )
        )
    completed_body = build_subagent_completion_storage_body(
        task_id=task_id,
        task_key="a" * 64,
        label=control_fill * 256,
        display_role=control_fill * 256,
        profile=profile,
        status=SubagentTaskStatus.COMPLETED,
        terminal_reason=None,
        terminal_public_detail=None,
        failed_dependency_task_ids=(),
        result_id=control_fill * 512,
        result_source=SubagentResultSource.EXPLICIT.value,
        result_summary=control_fill * MAXIMUM_RESULT_SUMMARY_UTF8_BYTES,
    )
    projections.append(
        project_subagent_completion_for_provider(
            completed_body,
            source_task_id=task_id,
        )
    )
    projection = max(projections, key=lambda value: len(value.encode("utf-8")))
    message = LLMMessage.user(projection)
    canonical = len(projection.encode("utf-8"))
    logical = provider_neutral_message_logical_bytes(message)
    if not 0 <= item_count <= ROOT_COMPLETION_SUFFIX_BATCH_ITEMS:
        raise ValueError("ROOT completion follow-up count is invalid")
    return (
        canonical * item_count,
        logical * item_count,
        item_count,
        (message,) * item_count,
    )


def _runtime_source_message_upper(
    *,
    source_kind: ContextSourceKind,
    trust_class: ContextTrustClass,
    lifecycle: SourceObservationLifecycle,
    maximum_body_utf8_bytes: int,
) -> LLMMessage:
    """Build the escaping maximum for one existing bounded source body."""

    if (
        not 0
        <= maximum_body_utf8_bytes
        <= (STRUCTURED_MODEL_INPUT_LIMITS.maximum_single_source_variant_bytes)
    ):
        raise ValueError("runtime source upper exceeds the compiler source bound")
    return encode_runtime_observation(
        source_kind=source_kind,
        trust_class=trust_class,
        lifecycle=lifecycle,
        presence=SourceObservationPresence.VALUE,
        contract_version="post-response-resource-upper",
        body="\x01" * maximum_body_utf8_bytes,
    )


def _capability_catalog_source_kinds(
    call: CompletedToolCallBlock,
) -> tuple[ContextSourceKind, ...]:
    """Return catalogs that a valid capability call can change before follow-up."""

    if call.tool_name == "reload_capabilities":
        return (ContextSourceKind.SKILL_CATALOG, ContextSourceKind.MCP_CATALOG)
    if call.tool_name != "manage_capability":
        return ()
    action = thaw_json(call.arguments).get("action")
    if action in _MCP_ONLY_CAPABILITY_ACTIONS:
        return (ContextSourceKind.MCP_CATALOG,)
    if action in {"INSTALL_PLUGIN", "SET_PLUGIN_ENABLED", "REMOVE_PLUGIN"}:
        return (ContextSourceKind.SKILL_CATALOG, ContextSourceKind.MCP_CATALOG)
    # The closed capability intent parser rejects unknown actions before any
    # mutation or adoption, so they cannot change a subsequent source.
    return ()


@dataclass(frozen=True, slots=True)
class _SessionStartAttemptToken:
    session_id: str
    source: str
    boundary_token: object | None = dataclass_field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class _PendingSessionStartBoundary:
    source: str
    boundary_token: object = dataclass_field(repr=False, compare=False)
    compact_attempt_token: object | None = dataclass_field(
        default=None, repr=False, compare=False
    )
    adopted_snapshot_id: str | None = None
    adopted_binding_revision_id: str | None = None


class _SessionStartColdBoundaryOwner(SessionStartCompactBoundaryPort):
    """One process-local pending SessionStart for the next ROOT cold open."""

    def __init__(self, initial_source: str) -> None:
        if initial_source not in {"startup", "resume"}:
            raise ValueError("initial SessionStart source is invalid")
        self._pending: _PendingSessionStartBoundary | None = (
            _PendingSessionStartBoundary(initial_source, object())
        )
        self._lock = asyncio.Lock()

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    async def arm_compact_boundary(
        self,
        *,
        attempt_token: CompactionAttemptToken,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> None:
        if not adopted_snapshot_id or not adopted_binding_revision_id:
            raise ValueError("compact SessionStart boundary identity is incomplete")
        async with self._lock:
            self._pending = _PendingSessionStartBoundary(
                "compact",
                (
                    attempt_token,
                    adopted_snapshot_id,
                    adopted_binding_revision_id,
                ),
                attempt_token,
                adopted_snapshot_id,
                adopted_binding_revision_id,
            )

    async def consume_any(self) -> _PendingSessionStartBoundary | None:
        async with self._lock:
            pending = self._pending
            self._pending = None
            return pending

    async def consume_compact(
        self, facts: PreparedCompactSessionStartFacts
    ) -> _PendingSessionStartBoundary | None:
        async with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if (
                pending.source != "compact"
                or pending.compact_attempt_token is not facts.attempt_token
                or pending.adopted_snapshot_id != facts.adopted_snapshot_id
                or pending.adopted_binding_revision_id
                != facts.adopted_binding_revision_id
            ):
                raise RuntimeError("compact SessionStart boundary token drifted")
            self._pending = None
            return pending


class SessionStartBlocked(RuntimeError):
    pass


class CompactionContinuationBlocked(RuntimeError):
    pass


class ChildCompactionContinuationBlocked(RuntimeError):
    pass


class RootControlCompletionSettlementPort(Protocol):
    async def __call__(self, turn_id: str, *, turn_completed: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class _RootSessionStartCompactPort(SessionStartCompactPort):
    dispatcher: KernelHookDispatcher | None = dataclass_field(repr=False, compare=False)
    context_owner: HookContextOwner | None = dataclass_field(repr=False, compare=False)
    scope: HookDispatchScopeRef | None = dataclass_field(repr=False, compare=False)
    boundary_owner: _SessionStartColdBoundaryOwner = dataclass_field(
        repr=False, compare=False
    )
    intent: ActiveTurnCancellationIntent = dataclass_field(repr=False, compare=False)
    session_id: str
    cwd: str

    async def __call__(
        self, facts: PreparedCompactSessionStartFacts
    ) -> PreparedCompactSessionStart:
        if (
            facts.scope.session_id != self.session_id
            or facts.turn_id != self.intent.turn_id
        ):
            raise RuntimeError("compact SessionStart port received foreign facts")
        boundary = await self.boundary_owner.consume_compact(facts)
        if boundary is None:
            return PreparedCompactSessionStart(True)
        if self.dispatcher is None or self.context_owner is None or self.scope is None:
            return PreparedCompactSessionStart(True)
        public_input = SessionStartInput(
            session_id=self.session_id,
            cwd=self.cwd,
            model=facts.model_id,
            source="compact",
            permission_mode=external_permission_mode(
                facts.permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    facts.permission_snapshot.plan_workflow_id is not None
                ),
            ),
        )
        causal_ref = SessionStartRef(
            _SessionStartAttemptToken(
                self.session_id,
                "compact",
                boundary.boundary_token,
            ),
            "compact",
        )
        outcome = await self.dispatcher.dispatch(
            HookDispatchEnvelope(
                self.dispatcher.capture_view(),
                self.scope,
                public_input,
                causal_ref,
                facts.deadline_monotonic,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, source="compact"
            ),
        )
        if self.intent.cause is not None:
            raise asyncio.CancelledError
        if outcome.decision is GateDecision.BLOCK:
            return PreparedCompactSessionStart(
                False,
                outcome.reason or "compact SessionStart Hook blocked",
            )
        reservation = self.context_owner.prepare_sync(
            scope=self.scope,
            causal_ref=causal_ref,
            entries=outcome.context_entries,
        )
        if reservation is not None:
            reservation.commit()
        return PreparedCompactSessionStart(True, reservation=reservation)


class _RunnerToolCompositionPort(ToolSurfacePlanningPort, ToolInvocationPort, Protocol):
    """Require one Tool owner to satisfy the runner's two narrow consumers."""

    def visualization_subscriptions(
        self, turn_id: str
    ) -> tuple[VisualizationSubscription, ...]: ...

    def consume_visualization_subscriptions(
        self, turn_id: str, expected: tuple[VisualizationSubscription, ...]
    ) -> None: ...

    def discard_visualization_subscriptions(self, turn_id: str) -> None: ...


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
        root_control_preparation_barrier: (
            Callable[[str], Awaitable[None]] | None
        ) = None,
        root_control_completion_fence: (Callable[[str], Awaitable[bool]] | None) = None,
        root_control_completion_settlement: (
            RootControlCompletionSettlementPort | None
        ) = None,
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
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_source: str = "startup",
        presentation_notice_sink: Callable[[str], None] | None = None,
        provider_input_installed_observer: (
            Callable[[KernelModelExecutionRequest], None] | None
        ) = None,
        provider_input_failure_observer: (
            Callable[[str, str], Awaitable[None]] | None
        ) = None,
    ) -> None:
        if maximum_output_tokens_per_call < 1 or (
            maximum_input_tokens_per_call is not None
            and maximum_input_tokens_per_call < 1
        ):
            raise ValueError("runner limits must be finite and positive")
        self._repository = repository
        self._writer_lease = writer_lease
        self._live_bus = live_bus
        self._tools = tools
        self._model_resolution_snapshot_provider = model_resolution_snapshot_provider
        resolved_input_reader = input_reader or CanonicalProviderInputReader(
            repository.connection_provider,
            blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
        )
        blob_store = PostgresCanonicalBlobStore(repository.connection_provider)
        self._before_provider_preparation = before_provider_preparation
        self._root_control_preparation_barrier = root_control_preparation_barrier
        self._root_control_completion_fence = root_control_completion_fence
        self._root_control_completion_settlement = root_control_completion_settlement
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
        self._context_source_collector = context_source_collector
        self._hook_dispatcher = hook_dispatcher
        self._hook_context_owner = hook_context_owner
        self._hook_scope = hook_scope
        self._presentation_notice_sink = presentation_notice_sink
        self._provider_input_installed_observer = provider_input_installed_observer
        self._provider_input_failure_observer = provider_input_failure_observer
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
            hook_dispatcher=hook_dispatcher,
            hook_root_scope=hook_scope,
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
        if intent.scope_kind is not ModelInputScopeKind.ROOT:
            return None
        return _RootSessionStartCompactPort(
            self._hook_dispatcher,
            self._hook_context_owner,
            self._hook_scope,
            self._session_start_boundary,
            intent,
            self._writer_lease.guard.session_id,
            (
                str(self._tools.snapshot_terminal_cwd())
                if self._hook_dispatcher is not None
                else ""
            ),
        )

    def _compact_session_start_boundary_port(
        self, intent: ActiveTurnCancellationIntent
    ) -> SessionStartCompactBoundaryPort | None:
        if intent.scope_kind is not ModelInputScopeKind.ROOT:
            return None
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
        force: bool,
    ) -> CompactionOutcome:
        return await self.compaction.compact_idle_turn(
            turn_id=turn_id,
            command_id=command_id,
            force=force,
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

    async def admit_subagent_turn(
        self,
        *,
        launch: PreparedSubagentLaunch,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> ActiveTurnCancellationIntent:
        task_start = launch.task_start
        if task_start.session_id != self._writer_lease.guard.session_id:
            raise ValueError("subagent launch belongs to another session")
        turn_id = launch.child_turn_id
        task_id = task_start.task_id
        content = await self._content(
            task_start.objective.encode("utf-8"), deadline=self._canonical_deadline()
        )
        occurred_at = datetime.now(timezone.utc)
        candidate = build_prepared_subagent_turn_admission(
            session_id=self._writer_lease.guard.session_id,
            task_id=task_id,
            turn_id=turn_id,
            entry_id=_stable_id("entry", turn_id, "objective"),
            context_binding_revision_id=_stable_id("context-revision", turn_id, "0"),
            permission_snapshot_id=_stable_id("permission-snapshot", turn_id),
            task_start_event_id=task_start.event_id,
            expected_parent_permission_snapshot=(launch.parent_permission_snapshot),
            content=content,
            occurred_at=occurred_at,
            actor_id="subagent-manager",
        )
        intent = cancellation_intent or ActiveTurnCancellationIntent(
            turn_id, ModelInputScopeKind.SUBAGENT_TASK, task_id
        )
        intent.require_exact(
            turn_id=turn_id,
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id=task_id,
        )
        await self._turn_admission.accept_subagent(
            candidate, cancellation_intent=intent
        )
        return intent

    async def run_admitted_subagent_turn(
        self,
        *,
        launch: PreparedSubagentLaunch,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        """Own one admitted child run and its exact process-local cleanup."""

        task_id = launch.task_start.task_id
        scope = ProviderInputContinuityScope(
            session_id=self._writer_lease.guard.session_id,
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id=task_id,
        )
        self._continuity.authorize_new_subagent_scope(
            scope,
            source=_issue_new_subagent_lease_source(
                writer_guard=self._writer_lease.guard,
                durable_runnable_task_fact=launch.task_start,
            ),
        )
        try:
            return await self.run_accepted_turn(
                launch.child_turn_id,
                cancellation_intent=cancellation_intent,
                expected_first_model_identity=launch.configured_model_identity,
            )
        finally:
            self._continuity.retire_terminal_subagent_scope(scope)
            self._memory_contexts.discard_scope(scope)

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
        stable_command_id = command_id or _id("command")
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
            turn_id, ModelInputScopeKind.ROOT, None
        )
        intent.require_exact(
            turn_id=turn_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        prospective_dispatch: PreparedProspectiveRootDispatch | None = None
        try:
            if hook_context_reservation is not None:
                hook_context_reservation.commit()
            prospective_candidate = await self._io.run(
                self._repository.prepare_root_provider_input_candidate,
                self._writer_lease.guard,
                intent=candidate,
                model_resolution_snapshot=frozen_model_resolution,
                deadline_monotonic=self._canonical_deadline(),
            )
            prospective_dispatch = await self.prepare_prospective_root_input(
                prospective_candidate,
                cancellation_intent=intent,
            )
            await self._turn_admission.accept_root_intent(
                candidate,
                provider_input_admission=prospective_dispatch.admission,
                model_resolution_snapshot=frozen_model_resolution,
                cancellation_intent=intent,
            )
        except BaseException:
            if prospective_dispatch is not None:
                prospective_dispatch.close()
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            raise
        return await self.run_accepted_turn(
            turn_id,
            cancellation_intent=intent,
            prospective_root_dispatch=prospective_dispatch,
        )

    async def prepare_prospective_root_input(
        self,
        candidate: PreparedRootProviderInputCandidate,
        *,
        admitted_writer: CompactionWriteReservation | None = None,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> PreparedProspectiveRootDispatch:
        """Prepare one exact first ROOT input before its canonical writer runs."""

        if (
            admitted_writer is None
            and self._root_control_preparation_barrier is not None
        ):
            await self._root_control_preparation_barrier(candidate.exact_turn_id)
        if self._before_provider_preparation is not None:
            await self._before_provider_preparation()
        intent = cancellation_intent or ActiveTurnCancellationIntent(
            candidate.exact_turn_id, ModelInputScopeKind.ROOT, None
        )
        intent.require_exact(
            turn_id=candidate.exact_turn_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        try:
            prepared = await self._provider_dispatch.prepare_prospective_root_input(
                candidate=candidate,
                inherited_memory_use_policy=self._root_memory_use_policy,
                deadline=self._planning_deadline(),
            )
        except (
            StructuredModelInputCompileError,
            ModelTargetCapabilityMismatch,
        ) as failure:
            recovered = await self.compaction.recover_pending_root_input(
                candidate=candidate,
                failure=failure,
                inherited_memory_use_policy=self._root_memory_use_policy,
                admitted_writer=admitted_writer,
                session_start_boundary_port=self._session_start_boundary,
            )
            self._emit_pending_root_handover_notice(recovered)
            return await self._attach_pending_root_hook_context(recovered, intent)
        handover = self.compaction.prospective_root_model_switch_candidate(prepared)
        if handover is None:
            if not self.compaction.prospective_root_crosses_automatic_threshold(
                prepared
            ):
                return await self._attach_pending_root_hook_context(prepared, intent)
            soft_trigger = StructuredModelInputCompileError(
                ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
            )
            # The prospective family may own the only Empty-scope planning
            # reservation.  Compaction must acquire its own exact source
            # preparation, so retire the dry prospective authority first.
            prepared.close()
            try:
                compacted = await self.compaction.recover_pending_root_input(
                    candidate=candidate,
                    failure=soft_trigger,
                    inherited_memory_use_policy=self._root_memory_use_policy,
                    admitted_writer=admitted_writer,
                    session_start_boundary_port=self._session_start_boundary,
                )
            except BaseException as error:
                if error is soft_trigger:
                    retry = (
                        await self._provider_dispatch.prepare_prospective_root_input(
                            candidate=candidate,
                            inherited_memory_use_policy=self._root_memory_use_policy,
                            deadline=self._planning_deadline(),
                        )
                    )
                    return await self._attach_pending_root_hook_context(retry, intent)
                raise
            self._emit_pending_root_handover_notice(compacted)
            return await self._attach_pending_root_hook_context(compacted, intent)
        prepared.close()
        switched = await self.compaction.execute_pending_root_model_handover(
            candidate=candidate,
            inherited_memory_use_policy=self._root_memory_use_policy,
            model_switch_candidate=handover,
            admitted_writer=admitted_writer,
            session_start_boundary_port=self._session_start_boundary,
        )
        self._emit_pending_root_handover_notice(switched)
        return await self._attach_pending_root_hook_context(switched, intent)

    async def _attach_pending_root_hook_context(
        self,
        prepared: PreparedProspectiveRootDispatch,
        intent: ActiveTurnCancellationIntent,
    ) -> PreparedProspectiveRootDispatch:
        """Add pending Hook context to one already-frozen first-call basis."""

        candidate = prepared.admission.candidate
        intent.require_exact(
            turn_id=candidate.exact_turn_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        deadline = self._planning_deadline()
        session_start_context: PendingHookContextReservation | None = None
        hook_sibling = None
        try:
            if self._session_start_boundary.has_pending:
                session_start_context = await self._dispatch_initial_session_start(
                    intent,
                    permission_snapshot=candidate.permission_snapshot,
                    model_id=prepared.prepared_target.target.fact.model_id,
                    deadline_monotonic=deadline,
                )
            hook_sibling = await self._provider_dispatch.prepare_hook_context_sibling(
                prepared,
                model_call_index=1,
                deadline=deadline,
            )
            if hook_sibling is None:
                session_start_context = None
                return prepared
            decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                hook_sibling.candidate,
                deadline=deadline,
            )
            if decision.wire_input_plan is None:
                hook_sibling.retire()
                hook_sibling = None
                session_start_context = None
                return prepared
            selected = self._provider_dispatch.bind_selected_prospective_root_dispatch(
                base=prepared,
                sibling=hook_sibling,
                decision=decision,
            )
            hook_sibling = None
            session_start_context = None
            return selected
        except BaseException:
            if hook_sibling is not None and hook_sibling.owns_reservation:
                hook_sibling.retire()
            if session_start_context is not None:
                session_start_context.retire()
            if prepared.owns_resources:
                prepared.close()
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

    async def prepare_plan_review_continuation_input(
        self,
        candidate: PreparedRootProviderInputCandidate,
        *,
        admitted_writer: CompactionWriteReservation,
    ) -> PreparedProspectiveRootDispatch:
        """Prepare a user-resolved Plan successor under its admitted writer."""

        return await self.prepare_prospective_root_input(
            candidate,
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

    async def prepare_plan_continuation_input(
        self, candidate: PreparedRootProviderInputCandidate
    ) -> PreparedProspectiveRootDispatch:
        """Prepare a continuation already covered by the response output gate."""

        if self._root_control_preparation_barrier is not None:
            await self._root_control_preparation_barrier(candidate.exact_turn_id)
        if self._before_provider_preparation is not None:
            await self._before_provider_preparation()
        prepared = await self._provider_dispatch.prepare_prospective_root_input(
            candidate=candidate,
            inherited_memory_use_policy=self._root_memory_use_policy,
            deadline=self._planning_deadline(),
        )
        if (
            self.compaction.prospective_root_model_switch_candidate(prepared)
            is not None
        ):
            prepared.close()
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            )
        return await self._attach_pending_root_hook_context(
            prepared,
            ActiveTurnCancellationIntent(
                candidate.exact_turn_id, ModelInputScopeKind.ROOT, None
            ),
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
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
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

    def _emit_pending_root_handover_notice(
        self, prepared: PreparedProspectiveRootDispatch
    ) -> None:
        if (
            prepared.model_switch_tier == 3
            and self._presentation_notice_sink is not None
        ):
            self._presentation_notice_sink(_MODEL_SWITCH_CONTEXT_REDUCTION_NOTICE)

    async def run_accepted_turn(
        self,
        turn_id: str,
        *,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
        expected_first_model_identity: str | None = None,
        prospective_root_dispatch: PreparedProspectiveRootDispatch | None = None,
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
        if expected_first_model_identity is not None and (
            intent.scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
            or not intent.scope_subagent_task_id
        ):
            raise ValueError("first-model precondition is child-only")

        model_call_count = 0
        tool_call_count = 0
        current_memory_use_policy = (
            self._root_memory_use_policy
            if intent.scope_kind is ModelInputScopeKind.ROOT
            else MemoryUsePolicy.ENABLED
        )
        active_surface_borrow: ProcessLocalToolSurfaceBorrow | None = None
        successor_dispatch: PreparedProviderDispatch | None = None
        successor_wire_decision: PreparedWireMeasurementDecision | None = None
        pending_prospective_root_dispatch = prospective_root_dispatch
        completed_tool_batch = False
        stop_continuation_used = False
        input_selection_in_progress = False
        root_completion_phase_opened = False
        if (
            intent.scope_kind is ModelInputScopeKind.ROOT
            and self._subagent_runtime is not None
        ):
            await self._subagent_runtime.open_root_completion_delivery(turn_id)
            root_completion_phase_opened = True
        try:
            if pending_prospective_root_dispatch is not None:
                if intent.scope_kind is not ModelInputScopeKind.ROOT:
                    pending_prospective_root_dispatch.close()
                    raise ValueError("prospective ROOT input was given to a child turn")
            while True:
                if (
                    successor_dispatch is None
                    and pending_prospective_root_dispatch is None
                    and intent.scope_kind is ModelInputScopeKind.ROOT
                    and self._root_control_preparation_barrier is not None
                ):
                    await self._root_control_preparation_barrier(turn_id)
                if (
                    successor_dispatch is None
                    and pending_prospective_root_dispatch is None
                    and self._before_provider_preparation is not None
                ):
                    # No input/surface handle exists here. A transferred compaction
                    # successor must be consumed untouched; edits wait for its next
                    # ordinary preparation boundary instead of replanning it.
                    await self._before_provider_preparation()
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
                    if pending_prospective_root_dispatch is not None:
                        pending_prospective_root_dispatch.close()
                        pending_prospective_root_dispatch = None
                    compaction = await self.compaction.execute_active(
                        turn_id=turn_id,
                        model_call_index=model_call_count + 1,
                        inherited_memory_use_policy=current_memory_use_policy,
                        trigger=CompactionTrigger.MANUAL,
                        force=manual_request.force,
                        manual_request=manual_request,
                        scope_kind=intent.scope_kind,
                        scope_subagent_task_id=intent.scope_subagent_task_id,
                        hook_scope=self._hook_scope,
                        session_start_compact_port=(
                            self._compact_session_start_port(intent)
                        ),
                        session_start_boundary_port=(
                            self._compact_session_start_boundary_port(intent)
                        ),
                    )
                    self._require_active_compaction_continuation(compaction)
                    successor_dispatch = compaction.successor_dispatch
                    completed_tool_batch = False
                    continue
                if pending_prospective_root_dispatch is not None:
                    prepared_root_dispatch = pending_prospective_root_dispatch
                    pending_prospective_root_dispatch = None
                    activated_root = (
                        await self._provider_dispatch.activate_prospective_root_input(
                            prepared_root_dispatch,
                            deadline=self._planning_deadline(),
                        )
                    )
                    if activated_root is None:
                        continue
                    (
                        successor_dispatch,
                        successor_wire_decision,
                    ) = activated_root
                model_call_count += 1
                input_selection_in_progress = True
                planning_deadline = self._planning_deadline()
                dispatch = successor_dispatch
                successor_dispatch = None
                automatic_compaction_decided = dispatch is not None
                reusable_wire_observation = None
                direct_switch_admission = None
                direct_switch_wire_plan = None
                wire_decision = successor_wire_decision
                successor_wire_decision = None
                if dispatch is None:
                    headroom_admission = None
                    allow_model_switch = (
                        model_call_count == 1 and not completed_tool_batch
                    )
                    if self.compaction.precompile_needed(
                        scope_kind=intent.scope_kind,
                        scope_subagent_task_id=intent.scope_subagent_task_id,
                        allow_model_switch=allow_model_switch,
                    ):
                        headroom_admission = (
                            await self.compaction.prepare_precompile_admission(
                                turn_id=turn_id,
                                model_call_index=model_call_count,
                                deadline=planning_deadline,
                            )
                        )
                        auto_trigger = (
                            CompactionTrigger.MID_TURN_FOLLOWUP
                            if completed_tool_batch
                            else CompactionTrigger.AUTO_ACTIVE_CONTEXT
                        )
                        precompile = await self.compaction.prepare_precompile(
                            turn_id=turn_id,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            trigger=auto_trigger,
                            scope_kind=intent.scope_kind,
                            scope_subagent_task_id=intent.scope_subagent_task_id,
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
                                direct_switch_admission = precompile.direct_admission
                                direct_switch_wire_plan = precompile.wire_input_plan
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
                                model_call_index=model_call_count,
                                inherited_memory_use_policy=(current_memory_use_policy),
                                trigger=precompile.trigger,
                                force=False,
                                manual_request=None,
                                scope_kind=intent.scope_kind,
                                scope_subagent_task_id=(intent.scope_subagent_task_id),
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
                                    model_call_index=model_call_count,
                                    inherited_memory_use_policy=(
                                        current_memory_use_policy
                                    ),
                                    candidate=precompile,
                                    scope_kind=intent.scope_kind,
                                    scope_subagent_task_id=(
                                        intent.scope_subagent_task_id
                                    ),
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
                                if (
                                    compaction.model_switch_tier == 3
                                    and self._presentation_notice_sink is not None
                                ):
                                    self._presentation_notice_sink(
                                        _MODEL_SWITCH_CONTEXT_REDUCTION_NOTICE
                                    )
                                model_call_count -= 1
                                successor_dispatch = compaction.successor_dispatch
                                completed_tool_batch = False
                                continue
                    session_start_context = None
                    if (
                        intent.scope_kind is ModelInputScopeKind.ROOT
                        and self._session_start_boundary.has_pending
                    ):
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
                        except PreparedSteerPlanStale:
                            headroom_admission = None
                            if direct_switch_admission is not None:
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
                    if (
                        model_call_count == 1
                        and expected_first_model_identity is not None
                        and dispatch.prepared_call.call.target.fact.model_id
                        != expected_first_model_identity
                    ):
                        if reusable_wire_observation is not None:
                            reusable_wire_observation.discard()
                            reusable_wire_observation = None
                        dispatch.close()
                        raise ConversationKernelConflict(
                            "child first provider target drifted from launch carrier"
                        )
                    auto_trigger = (
                        CompactionTrigger.MID_TURN_FOLLOWUP
                        if completed_tool_batch
                        else CompactionTrigger.AUTO_ACTIVE_CONTEXT
                    )
                    if (
                        dispatch.installed_provider_open is None
                        and wire_decision is None
                    ):
                        if direct_switch_admission is not None:
                            if direct_switch_wire_plan is None:
                                raise RuntimeError(
                                    "direct-switch admission lost its wire plan"
                                )
                            wire_decision = self._provider_dispatch.bind_direct_switch_admission(
                                candidate=(
                                    self._provider_dispatch.wire_candidate_for_dispatch(
                                        dispatch
                                    )
                                ),
                                admission=direct_switch_admission,
                                wire_input_plan=direct_switch_wire_plan,
                            )
                        else:
                            wire_decision = await self.compaction.measure_dispatch_wire(
                                dispatch,
                                deadline=planning_deadline,
                                reusable_observation=reusable_wire_observation,
                            )
                        reusable_wire_observation = None
                    if (
                        not automatic_compaction_decided
                        and self.compaction.automatic_allowed(
                            scope_kind=intent.scope_kind,
                            scope_subagent_task_id=intent.scope_subagent_task_id,
                        )
                        and wire_decision is not None
                        and self.compaction.wire_decision_crosses_automatic_threshold(
                            dispatch, wire_decision
                        )
                    ):
                        dispatch.close_for_canonical_replan()
                        compaction = await self.compaction.execute_active(
                            turn_id=turn_id,
                            model_call_index=model_call_count,
                            inherited_memory_use_policy=current_memory_use_policy,
                            trigger=auto_trigger,
                            force=False,
                            manual_request=None,
                            scope_kind=intent.scope_kind,
                            scope_subagent_task_id=intent.scope_subagent_task_id,
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
                root_answer_fenced = False
                control_answer_fenced = False
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
                    if self._provider_input_installed_observer is not None:
                        self._provider_input_installed_observer(provider_open.request)
                    request = provider_open.request
                    input_selection_in_progress = False
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
                    calls = tuple(
                        item
                        for item in completed.blocks
                        if isinstance(item, CompletedToolCallBlock)
                    )
                    pending_completion_count = 0
                    root_completion_followup_items = 0
                    if (
                        identity.conversation_scope_kind is ModelInputScopeKind.ROOT
                        and self._subagent_runtime is not None
                    ):
                        if not calls:
                            pending_completion_count = await self._subagent_runtime.seal_root_completion_delivery(
                                turn_id
                            )
                            root_answer_fenced = True
                            root_completion_followup_items = pending_completion_count
                        elif (
                            any(
                                call.tool_name == "create_agent_tasks" for call in calls
                            )
                            or await self._subagent_runtime.root_completion_followup_possible(
                                turn_id
                            )
                        ):
                            root_completion_followup_items = (
                                ROOT_COMPLETION_SUFFIX_BATCH_ITEMS
                            )
                    pending_control_feedback = False
                    pending_steer_before_settlement = False
                    if (
                        not calls
                        and identity.conversation_scope_kind is ModelInputScopeKind.ROOT
                        and self._root_control_completion_fence is not None
                    ):
                        # The Host callback may wait after installing its exact
                        # process-local seal.  Transfer cleanup ownership before
                        # awaiting so cancellation cannot strand that writer
                        # reservation ahead of idle/manual compaction.
                        control_answer_fenced = True
                        pending_control_feedback = (
                            await self._root_control_completion_fence(turn_id)
                        )
                        pending_steer_before_settlement = bool(
                            await self._io.run(
                                self._repository.read_pending_prompt_steer_facts,
                                session_id=identity.session_id,
                                target_turn_id=turn_id,
                                deadline_monotonic=self._canonical_deadline(),
                            )
                        )
                    output_quote = self._quote_post_response_resources(
                        request=request,
                        permit=permit,
                        collected=collected,
                        canonical_facts=canonical_facts,
                        root_completion_followup_items=(root_completion_followup_items),
                        pending_root_dynamic_followup=(
                            pending_control_feedback or pending_steer_before_settlement
                        ),
                    )
                    try:
                        self._require_post_response_resources(
                            output_quote,
                            effective_input_budget_tokens=(
                                request.wire_input_plan.quote.effective_input_budget_tokens
                            ),
                        )
                    except OutputResourceInterruption as exc:
                        if root_answer_fenced and self._subagent_runtime is not None:
                            await (
                                self._subagent_runtime.settle_root_completion_delivery(
                                    turn_id, turn_completed=False
                                )
                            )
                            root_answer_fenced = False
                        if (
                            control_answer_fenced
                            and self._root_control_completion_settlement is not None
                        ):
                            await self._root_control_completion_settlement(
                                turn_id, turn_completed=False
                            )
                            control_answer_fenced = False
                        self._live_bus.offer_settlement_nowait(
                            kind=LiveSettlementKind.ABORTED,
                            session_id=request.session_id,
                            turn_id=turn_id,
                            draft_identity=entry_id,
                            reason_code=f"OUTPUT_RESOURCE_{exc.reason}",
                            scope_kind=(identity.conversation_scope_kind.value),
                            scope_subagent_task_id=(identity.scope_subagent_task_id),
                            channel_kind=LiveChannelKind.MODEL_OUTPUT,
                            generation_id=f"model-output:{entry_id}",
                            proposed_entry_id=entry_id,
                        )
                        raise
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
                    completion_prepared: PreparedInferredSubagentCompletion | None = (
                        None
                    )
                    complete_turn = not calls
                    stop_continuation = None
                    stop_causal_ref = None
                    if (
                        complete_turn
                        and identity.conversation_scope_kind is ModelInputScopeKind.ROOT
                        and self._hook_dispatcher is not None
                        and self._hook_context_owner is not None
                        and self._hook_scope is not None
                    ):
                        stop_public_input = StopInput(
                            session_id=self._writer_lease.guard.session_id,
                            cwd=str(self._tools.snapshot_terminal_cwd()),
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
                        stop_continuation = await self._hook_dispatcher.dispatch(
                            HookDispatchEnvelope(
                                self._hook_dispatcher.capture_view(),
                                self._hook_scope,
                                stop_public_input,
                                stop_causal_ref,
                                self._planning_deadline(),
                            ),
                            matcher_subject=event_matcher_subject(
                                stop_public_input.event_type
                            ),
                            continuation_already_used=stop_continuation_used,
                        )
                        complete_turn = (
                            stop_continuation.decision
                            is ContinuationDecision.TERMINALIZE
                        )
                    if (
                        complete_turn
                        and identity.conversation_scope_kind
                        is ModelInputScopeKind.SUBAGENT_TASK
                        and identity.scope_subagent_task_id is not None
                        and self._subagent_runtime is not None
                    ):
                        completion_prepared = await self._subagent_runtime.prepare_inferred_completion(
                            task_id=identity.scope_subagent_task_id,
                            entry_id=entry_id,
                            public_text=completed.public_text,
                            model_id=request.prepared_call.call.target.fact.model_id,
                            permission_snapshot=(
                                canonical_facts.run_permission_snapshot
                            ),
                        )
                        complete_turn = (
                            completion_prepared is not None
                            and completion_prepared.result is not None
                        )
                    subagent_result = (
                        None
                        if completion_prepared is None
                        else completion_prepared.result
                    )
                    if (
                        complete_turn
                        and identity.conversation_scope_kind is ModelInputScopeKind.ROOT
                    ):
                        pending_completion = False
                        if self._subagent_runtime is not None:
                            if not root_answer_fenced:
                                pending_completion_count = await self._subagent_runtime.seal_root_completion_delivery(
                                    turn_id
                                )
                                root_answer_fenced = True
                            pending_completion = bool(pending_completion_count)
                        complete_turn = not (
                            pending_completion or pending_control_feedback
                        )
                    visualization_batch = (
                        self._tools.visualization_subscriptions(turn_id)
                        if not calls else ()
                    )
                    frozen_visualizations = []
                    if visualization_batch:
                        visualization_reader = PostgresCanonicalVisualizationReadPort(
                            self._repository.connection_provider,
                            session_id=request.session_id,
                            workspace_id=await self._resolved_workspace_id(),
                        )
                        for subscription in visualization_batch:
                            item = await self._io.run(
                                materialize_visualization_subscription,
                                subscription,
                                visualization_reader,
                                deadline_monotonic=self._canonical_deadline(),
                            )
                            if item is not None:
                                frozen_visualizations.append(item)
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
                        visualizations=tuple(frozen_visualizations),
                        provider_replay=collected.provider_replay,
                        provider_replay_reservation=(provider_replay_reservation),
                        subagent_result=subagent_result,
                    )
                    # The settlement owner now owns promotion/release across
                    # exact-confirm and caller cancellation.
                    provider_replay_reservation = None
                    try:
                        accepted = await self._assistant_settlements.settle(settlement)
                    except BaseException:
                        if (
                            control_answer_fenced
                            and self._root_control_completion_settlement is not None
                        ):
                            await self._root_control_completion_settlement(
                                turn_id, turn_completed=False
                            )
                            control_answer_fenced = False
                        if root_answer_fenced and self._subagent_runtime is not None:
                            await (
                                self._subagent_runtime.settle_root_completion_delivery(
                                    turn_id, turn_completed=False
                                )
                            )
                            root_answer_fenced = False
                        if completion_prepared is not None:
                            await self._subagent_runtime.finish_completion(
                                completion_prepared.permit, committed=False
                            )
                        raise
                    if visualization_batch:
                        self._tools.consume_visualization_subscriptions(
                            turn_id, visualization_batch
                        )
                    if (
                        control_answer_fenced
                        and self._root_control_completion_settlement is not None
                    ):
                        await self._root_control_completion_settlement(
                            turn_id, turn_completed=accepted.turn_completed
                        )
                        control_answer_fenced = False
                    if root_answer_fenced and self._subagent_runtime is not None:
                        await self._subagent_runtime.settle_root_completion_delivery(
                            turn_id, turn_completed=accepted.turn_completed
                        )
                        root_answer_fenced = False
                    if completion_prepared is not None:
                        await self._subagent_runtime.finish_completion(
                            completion_prepared.permit, committed=True
                        )
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
                        scope_subagent_task_id=identity.scope_subagent_task_id,
                        channel_kind=LiveChannelKind.MODEL_OUTPUT,
                        generation_id=f"model-output:{entry_id}",
                        proposed_entry_id=entry_id,
                    )
                finally:
                    if (
                        control_answer_fenced
                        and self._root_control_completion_settlement is not None
                    ):
                        await self._root_control_completion_settlement(
                            turn_id, turn_completed=False
                        )
                    if root_answer_fenced and self._subagent_runtime is not None:
                        await self._subagent_runtime.settle_root_completion_delivery(
                            turn_id, turn_completed=False
                        )
                    if provider_replay_reservation is not None:
                        self._continuity.release_assistant_replay_fragment_reservation(
                            provider_replay_reservation
                        )
                    active_surface_borrow = dispatch.finish_model_operation()
                if not calls and accepted.turn_completed:
                    active_surface_borrow.close()
                    active_surface_borrow = None
                    self._memory_dispatch.offer_governance_wake()
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
                        hook_cwd=str(self._tools.snapshot_terminal_cwd()),
                        continuity_epoch_nonce=plan_epoch.epoch_nonce,
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
                    self._memory_dispatch.offer_governance_wake()
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
                    last_assistant_message=(completed.public_text or None),
                    hook_scope=self._hook_scope,
                    image_call_allowances=output_quote.image_call_allowances,
                )
                tool_call_count += batch.tool_call_count
                if batch.terminal is not None:
                    self._memory_dispatch.offer_governance_wake()
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
                successor_dispatch.close()
            if active_surface_borrow is not None:
                active_surface_borrow.close()
                active_surface_borrow = None
            if (
                input_selection_in_progress
                and intent.scope_kind is ModelInputScopeKind.ROOT
                and self._provider_input_failure_observer is not None
            ):
                await self._provider_input_failure_observer(
                    turn_id,
                    "INPUT_COMPILE_FAILED"
                    if isinstance(error, StructuredModelInputCompileError)
                    else "INPUT_ADMISSION_FAILED",
                )
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
            if intent.scope_kind is ModelInputScopeKind.SUBAGENT_TASK and isinstance(
                error, CompactionContinuationBlocked
            ):
                raise ChildCompactionContinuationBlocked(
                    "HOOK_COMPACTION_BLOCKED"
                ) from error
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
            self._memory_dispatch.offer_governance_wake()
            raise
        finally:
            self._tools.discard_visualization_subscriptions(turn_id)
            if pending_prospective_root_dispatch is not None:
                pending_prospective_root_dispatch.close()
            if root_completion_phase_opened and self._subagent_runtime is not None:
                await self._subagent_runtime.close_root_completion_delivery(turn_id)

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
        cwd = self._tools.snapshot_terminal_cwd()
        public_input = SessionStartInput(
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
        outcome = await dispatcher.dispatch(
            HookDispatchEnvelope(
                view,
                scope,
                public_input,
                causal_ref,
                deadline_monotonic,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, source=source
            ),
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

    async def accept_subagent_completion(
        self,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        task_id: str,
        command_id: str,
        actor_id: str,
        deadline_monotonic: float,
        admitted_writer: CompactionWriteReservation,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> tuple[
        AcceptedSubagentCompletion,
        PreparedProspectiveRootDispatch | None,
    ]:
        if new_context_binding_revision_id is None:
            candidate = await self._io.run(
                self._repository.prepare_manual_subagent_completion_provider_input_candidate,
                self._writer_lease.guard,
                turn_id=turn_id,
                task_id=task_id,
                command_id=command_id,
                deadline_monotonic=deadline_monotonic,
            )
            if isinstance(candidate, AcceptedSubagentCompletion):
                return candidate, None
            prepared: PreparedProspectiveActiveRootInput | None = None
            publication_handle = None
            try:
                prepared = await self._prepare_active_root_input(
                    candidate,
                    deadline=deadline_monotonic,
                    cancellation_intent=cancellation_intent,
                    admitted_writer=admitted_writer,
                )
                publication_handle = prepared.take_handle_for_publication()
                accepted = await self._io.run(
                    self._safe_point.accept_subagent_completion,
                    handle=publication_handle,
                    provider_input_admission=prepared.admission,
                    turn_id=turn_id,
                    new_context_binding_revision_id=None,
                    requested_permission_mode=None,
                    task_id=task_id,
                    command_id=command_id,
                    actor_id=actor_id,
                    deadline_monotonic=deadline_monotonic,
                )
                if accepted.disposition is not SubagentCompletionDisposition.CREATED:
                    raise ConversationKernelConflict(
                        "prepared manual completion was not published"
                    )
                await self._provider_dispatch.confirm_published_active_root_input(
                    prepared,
                    publication_handle=publication_handle,
                    deadline=deadline_monotonic,
                )
                publication_handle = None
                return accepted, None
            finally:
                if publication_handle is not None:
                    publication_handle.close()
                if prepared is not None:
                    prepared.close()

        if requested_permission_mode is None:
            raise ValueError("new ROOT completion requires a permission mode")
        candidate = await self._io.run(
            self._repository.prepare_manual_subagent_completion_root_provider_input_candidate,
            self._writer_lease.guard,
            turn_id=turn_id,
            new_context_binding_revision_id=new_context_binding_revision_id,
            requested_permission_mode=requested_permission_mode,
            task_id=task_id,
            command_id=command_id,
            deadline_monotonic=deadline_monotonic,
        )
        if isinstance(candidate, AcceptedSubagentCompletion):
            return candidate, None
        prospective_root: PreparedProspectiveRootDispatch | None = None
        try:
            prospective_root = await self.prepare_prospective_root_input(
                candidate,
                admitted_writer=admitted_writer,
                cancellation_intent=cancellation_intent,
            )
            accepted = await self._io.run(
                self._safe_point.accept_subagent_completion,
                provider_input_admission=prospective_root.admission,
                turn_id=turn_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                requested_permission_mode=requested_permission_mode,
                task_id=task_id,
                command_id=command_id,
                actor_id=actor_id,
                deadline_monotonic=deadline_monotonic,
            )
            if accepted.disposition is not SubagentCompletionDisposition.CREATED:
                prospective_root.close()
                prospective_root = None
            return accepted, prospective_root
        except BaseException:
            if prospective_root is not None:
                prospective_root.close()
            raise

    async def install_terminal_observation(
        self,
        *,
        coordinator: TerminalMonitorCoordinator,
        monitor_id: str,
        target: PreparedInstallationTarget,
        workspace_id: str,
        actor_id: str,
        deadline_monotonic: float,
        admitted_writer: CompactionWriteReservation,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> tuple[AcceptedEntry | None, PreparedProspectiveRootDispatch | None]:
        prepared_attempt = await self._io.run(
            self._safe_point.prepare_terminal_observation_installation,
            coordinator=coordinator,
            monitor_id=monitor_id,
            target=target,
            workspace_id=workspace_id,
            actor_id=actor_id,
            deadline_monotonic=deadline_monotonic,
        )
        if prepared_attempt is None or isinstance(prepared_attempt, AcceptedEntry):
            return prepared_attempt, None
        attempt: TerminalObservationInstallationAttempt = prepared_attempt
        if isinstance(attempt.target, ExistingTurnInstallation):
            candidate = await self._io.run(
                self._repository.prepare_active_terminal_observation_provider_input_candidate,
                self._writer_lease.guard,
                candidate=attempt,
                deadline_monotonic=deadline_monotonic,
            )
            prepared_active: PreparedProspectiveActiveRootInput | None = None
            publication_handle = None
            try:
                prepared_active = await self._prepare_active_root_input(
                    candidate,
                    deadline=deadline_monotonic,
                    cancellation_intent=cancellation_intent,
                    admitted_writer=admitted_writer,
                )
                publication_handle = prepared_active.take_handle_for_publication()
                accepted = await self._io.run(
                    self._safe_point.publish_terminal_observation,
                    publication_handle,
                    coordinator=coordinator,
                    attempt=attempt,
                    provider_input_admission=prepared_active.admission,
                    deadline_monotonic=deadline_monotonic,
                )
                await self._provider_dispatch.confirm_published_active_root_input(
                    prepared_active,
                    publication_handle=publication_handle,
                    deadline=deadline_monotonic,
                )
                publication_handle = None
                return accepted, None
            except (StructuredModelInputCompileError, ModelTargetCapabilityMismatch):
                coordinator.settle_installation(attempt, accepted=False)
                raise
            finally:
                if publication_handle is not None:
                    publication_handle.close()
                if prepared_active is not None:
                    prepared_active.close()
        if not isinstance(attempt.target, NewTurnInstallation):
            raise TypeError("terminal observation target is unknown")
        candidate = await self._io.run(
            self._repository.prepare_new_terminal_observation_provider_input_candidate,
            self._writer_lease.guard,
            candidate=attempt,
            deadline_monotonic=deadline_monotonic,
        )
        prospective_root: PreparedProspectiveRootDispatch | None = None
        try:
            prospective_root = await self.prepare_prospective_root_input(
                candidate,
                admitted_writer=admitted_writer,
                cancellation_intent=cancellation_intent,
            )
            accepted = await self._io.run(
                self._safe_point.publish_terminal_observation,
                None,
                coordinator=coordinator,
                attempt=attempt,
                provider_input_admission=prospective_root.admission,
                deadline_monotonic=deadline_monotonic,
            )
            return accepted, prospective_root
        except BaseException:
            if prospective_root is not None:
                prospective_root.close()
            raise

    async def install_user_control_feedback(
        self,
        *,
        attempt: UserControlFeedbackInstallationAttempt,
        deadline_monotonic: float,
        admitted_writer: CompactionWriteReservation,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> AcceptedEntry:
        confirmed = await self._io.run(
            self._safe_point.confirm_user_control_feedback,
            attempt=attempt,
            deadline_monotonic=deadline_monotonic,
        )
        if confirmed is not None:
            return confirmed
        candidate = await self._io.run(
            self._repository.prepare_user_control_feedback_provider_input_candidate,
            self._writer_lease.guard,
            candidate=attempt,
            deadline_monotonic=deadline_monotonic,
        )
        prepared: PreparedProspectiveActiveRootInput | None = None
        publication_handle = None
        try:
            prepared = await self._prepare_active_root_input(
                candidate,
                deadline=deadline_monotonic,
                cancellation_intent=cancellation_intent,
                admitted_writer=admitted_writer,
            )
            publication_handle = prepared.take_handle_for_publication()
            accepted = await self._io.run(
                self._safe_point.install_user_control_feedback,
                publication_handle,
                attempt=attempt,
                provider_input_admission=prepared.admission,
                deadline_monotonic=deadline_monotonic,
            )
            await self._provider_dispatch.confirm_published_active_root_input(
                prepared,
                publication_handle=publication_handle,
                deadline=deadline_monotonic,
            )
            publication_handle = None
            return accepted
        finally:
            if publication_handle is not None:
                publication_handle.close()
            if prepared is not None:
                prepared.close()

    async def confirm_user_control_feedback(
        self,
        *,
        attempt: UserControlFeedbackInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        return await self._io.run(
            self._safe_point.confirm_user_control_feedback,
            attempt=attempt,
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

    def _hook_output_source_upper(
        self,
        *,
        call_count: int,
        scope_kind: ModelInputScopeKind,
    ) -> LLMMessage | None:
        if (
            self._hook_dispatcher is None
            or self._hook_context_owner is None
            or self._hook_scope is None
        ):
            return None
        view = self._hook_dispatcher.capture_view()
        event_occurrences = (
            (
                (HookEventType.PRE_TOOL_USE_EVENT, call_count, False),
                (HookEventType.PERMISSION_REQUEST_EVENT, call_count, False),
                (HookEventType.POST_TOOL_USE_EVENT, call_count, False),
            )
            if call_count
            else (
                (
                    HookEventType.STOP_EVENT
                    if scope_kind is ModelInputScopeKind.ROOT
                    else HookEventType.SUBAGENT_STOP_EVENT,
                    1,
                    True,
                ),
            )
        )
        body_bytes = maximum_hook_context_provider_body_bytes(
            view,
            event_occurrences=event_occurrences,
        )
        if not body_bytes:
            return None
        return _runtime_source_message_upper(
            source_kind=ContextSourceKind.HOOK_CONTEXT,
            trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
            lifecycle=SourceObservationLifecycle.ONE_SHOT,
            maximum_body_utf8_bytes=body_bytes,
        )

    def _quote_post_response_resources(
        self,
        *,
        request: KernelModelExecutionRequest,
        permit: ProcessLocalProviderInputInstallPermit,
        collected: _CollectedModelResponse,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        root_completion_followup_items: int,
        pending_root_dynamic_followup: bool = False,
    ) -> FrozenPostResponseResourceQuote:
        completed = collected.completed
        assistant = _completed_assistant_semantic_message(completed)
        calls = tuple(
            item
            for item in completed.blocks
            if isinstance(item, CompletedToolCallBlock)
        )
        if pending_root_dynamic_followup and (
            calls
            or canonical_facts.canonical_input.identity.conversation_scope_kind
            is not ModelInputScopeKind.ROOT
        ):
            raise ValueError("dynamic ROOT follow-up quote has an invalid response")
        plan_batch = any(call.tool_name in _PLAN_CONTROL_TOOL_NAMES for call in calls)
        canonical_followup = 0
        logical_followup = 0
        followup_items = 0
        suffix_messages: list[LLMMessage] = []
        for call in calls:
            quote = (
                _plan_result_followup_upper(call)
                if plan_batch
                else _ordinary_result_followup_upper(call)
            )
            canonical_followup += quote[0]
            logical_followup += quote[1]
            followup_items += quote[2]
            suffix_messages.extend(quote[3])

        image_calls: list[tuple[int, CompletedToolCallBlock, str]] = []
        for call_ordinal, call in enumerate(calls):
            if call.tool_name not in {"view_image", "visualization_render"}:
                continue
            try:
                binding = request.prepared_call.tool_surface.binding(call.tool_name)
            except KeyError:
                continue
            if not isinstance(binding.execution_policy, BuiltinExecutionPolicyRef):
                continue
            arguments = thaw_json(call.arguments)
            if not isinstance(arguments, dict):
                continue
            try:
                if call.tool_name == "visualization_render":
                    image_source, review = parse_visualization_source(arguments)
                    if not review:
                        continue
                else:
                    image_source = parse_view_image_source(arguments)
            except ValueError:
                continue
            image_calls.append(
                (call_ordinal, call, binding.executor_binding_fingerprint)
            )
            reference_upper = (
                image_source.value
                if image_source.kind in {
                    ViewImageSourceKind.IMAGE_REF,
                    VisualizationSourceKind.VISUALIZATION_REF,
                }
                else "sha256:" + ("0" * 64)
            )
            image_source_upper = LLMMessage(
                role=MessageRole.USER,
                content=(
                    LLMTextPart(
                        canonical_json_bytes(
                            {
                                "tool_image_source": {
                                    "tool_call_id": call.tool_call_id,
                                    **image_source.provider_value(),
                                }
                            }
                        ).decode("utf-8")
                    ),
                    image_reference_digest_part(reference_upper),
                ),
            )
            logical_followup += provider_neutral_message_logical_bytes(
                image_source_upper
            )
            suffix_messages.append(image_source_upper)

        selected_plan_call = next(
            (call for call in calls if call.tool_name in _PLAN_CONTROL_TOOL_NAMES),
            None,
        )
        fresh_enter_plan = (
            selected_plan_call is not None
            and selected_plan_call.tool_name == "enter_plan"
            and canonical_facts.plan_workflow_fact is None
        )
        if fresh_enter_plan:
            continuation_quote = _entered_plan_continuation_followup_upper()
            canonical_followup += continuation_quote[0]
            logical_followup += continuation_quote[1]
            followup_items += continuation_quote[2]
            suffix_messages.extend(continuation_quote[3])

        epoch = self._continuity.current_view(permit.scope)
        if (
            epoch is None
            or epoch.epoch_nonce != permit.epoch_nonce
            or epoch.epoch_revision != permit.epoch_revision
            or epoch.wire_input_plan is not request.wire_input_plan
        ):
            raise ConversationKernelConflict(
                "provider output resource quote lost its installed prefix"
            )
        canonical_input = canonical_facts.canonical_input
        if canonical_input.identity != request.compiled_input.canonical_input_identity:
            raise ConversationKernelConflict(
                "provider output resource quote lost its canonical input"
            )
        assistant_canonical = _completed_assistant_canonical_bytes(completed)
        assistant_logical = provider_neutral_message_logical_bytes(assistant)
        identity = canonical_input.identity
        source_messages: list[LLMMessage] = []
        changed_catalogs = {
            source_kind
            for call in calls
            for source_kind in _capability_catalog_source_kinds(call)
        }
        if ContextSourceKind.SKILL_CATALOG in changed_catalogs:
            source_messages.append(
                _runtime_source_message_upper(
                    source_kind=ContextSourceKind.SKILL_CATALOG,
                    trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
                    lifecycle=SourceObservationLifecycle.SNAPSHOT,
                    maximum_body_utf8_bytes=MAX_SKILL_CATALOG_UTF8_BYTES,
                )
            )
        if ContextSourceKind.MCP_CATALOG in changed_catalogs:
            source_messages.append(
                _runtime_source_message_upper(
                    source_kind=ContextSourceKind.MCP_CATALOG,
                    trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
                    lifecycle=SourceObservationLifecycle.SNAPSHOT,
                    maximum_body_utf8_bytes=MAXIMUM_MCP_CATALOG_FULL_BYTES,
                )
            )
        if fresh_enter_plan:
            source_messages.extend(
                self._context_source_collector.freeze_fresh_entered_plan_source_upper(
                    canonical_facts.run_permission_snapshot
                )
            )
        hook_source = self._hook_output_source_upper(
            call_count=len(calls),
            scope_kind=identity.conversation_scope_kind,
        )
        if hook_source is not None:
            source_messages.append(hook_source)
        if root_completion_followup_items:
            if (
                identity.conversation_scope_kind is not ModelInputScopeKind.ROOT
                or self._subagent_runtime is None
            ):
                raise ValueError("ROOT completion quote belongs to a non-ROOT response")
            completion_quote = _root_completion_followup_upper(
                root_completion_followup_items
            )
            canonical_followup += completion_quote[0]
            logical_followup += completion_quote[1]
            followup_items += completion_quote[2]
            suffix_messages.extend(completion_quote[3])
        no_call_continuation_possible = (
            not calls
            and identity.conversation_scope_kind is ModelInputScopeKind.SUBAGENT_TASK
            and self._subagent_runtime is not None
        )
        needs_followup = (
            bool(calls)
            or hook_source is not None
            or bool(root_completion_followup_items)
            or no_call_continuation_possible
            or pending_root_dynamic_followup
        )
        if needs_followup:
            source_messages.extend(
                self._context_source_collector.freeze_post_response_call_source_upper()
            )
        if any(not isinstance(message, LLMMessage) for message in source_messages):
            raise TypeError("context source upper returned a foreign message")
        logical_followup += sum(
            provider_neutral_message_logical_bytes(message)
            for message in source_messages
        )
        suffix_messages.extend(source_messages)
        wire = (
            quote_provider_followup_wire_resources(
                request=request,
                actual_assistant_message=assistant,
                provider_replay=collected.provider_replay,
                bounded_suffix_messages=tuple(suffix_messages),
            )
            if needs_followup
            else None
        )
        allowances: tuple[FrozenImageToolResourceAllowance, ...] = ()
        if image_calls:
            if wire is None:
                raise ValueError("image response calls require a follow-up wire quote")
            remaining = (
                MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
                - (
                    canonical_input.canonical_expanded_bytes
                    + assistant_canonical
                    + canonical_followup
                ),
                MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES
                - (epoch.logical_bytes + assistant_logical + logical_followup),
                MAXIMUM_PROVIDER_WIRE_INPUT_BYTES - wire.final_wire_utf8_bytes,
                request.wire_input_plan.quote.effective_input_budget_tokens
                - wire.final_wire_estimated_input_tokens,
            )
            if min(remaining) < 0:
                remaining = tuple(max(0, value) for value in remaining)
            count = len(image_calls)
            quote_owner = _ImageToolResourceQuoteOwner(
                request=request,
                assistant=assistant,
                provider_replay=collected.provider_replay,
                base_suffix_messages=tuple(suffix_messages),
                base_wire=wire,
            )

            def share(total: int, index: int) -> int:
                quotient, remainder = divmod(total, count)
                return quotient + (1 if index < remainder else 0)

            allowances = tuple(
                FrozenImageToolResourceAllowance(
                    call_ordinal=call_ordinal,
                    tool_call_id=call.tool_call_id,
                    executor_binding_fingerprint=binding_fingerprint,
                    canonical_bytes=share(remaining[0], index),
                    logical_bytes=share(remaining[1], index),
                    wire_bytes=share(remaining[2], index),
                    input_tokens=share(remaining[3], index),
                    quote_owner=quote_owner,
                )
                for index, (call_ordinal, call, binding_fingerprint) in enumerate(
                    image_calls
                )
            )
        return FrozenPostResponseResourceQuote(
            current_canonical_expanded_bytes=(canonical_input.canonical_expanded_bytes),
            actual_assistant_canonical_bytes=assistant_canonical,
            bounded_followup_canonical_bytes=canonical_followup,
            canonical_upper_after=(
                canonical_input.canonical_expanded_bytes
                + assistant_canonical
                + canonical_followup
            ),
            current_epoch_logical_bytes=epoch.logical_bytes,
            actual_assistant_logical_bytes=assistant_logical,
            bounded_followup_logical_bytes=logical_followup,
            logical_upper_after=(
                epoch.logical_bytes + assistant_logical + logical_followup
            ),
            current_canonical_items=len(canonical_input.items),
            bounded_followup_items=followup_items,
            item_upper_after=(len(canonical_input.items) + 1 + followup_items),
            followup_wire=wire,
            image_call_allowances=allowances,
        )

    @staticmethod
    def _require_post_response_resources(
        quote: FrozenPostResponseResourceQuote,
        *,
        effective_input_budget_tokens: int,
    ) -> None:
        if quote.canonical_upper_after > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
            raise OutputResourceInterruption("CANONICAL_BYTES", quote)
        if quote.logical_upper_after > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
            raise OutputResourceInterruption("EPOCH_LOGICAL_BYTES", quote)
        if quote.item_upper_after > MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS:
            raise OutputResourceInterruption("CANONICAL_ITEMS", quote)
        wire = quote.followup_wire
        if wire is None:
            return
        if wire.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
            raise OutputResourceInterruption("FOLLOWUP_WIRE_BYTES", quote)
        if wire.final_wire_estimated_input_tokens > effective_input_budget_tokens:
            raise OutputResourceInterruption("FOLLOWUP_INPUT_TOKENS", quote)

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
    if isinstance(error, SessionStartBlocked):
        return "HOOK_SESSION_START_BLOCKED"
    if isinstance(error, CompactionContinuationBlocked):
        return "HOOK_COMPACTION_BLOCKED"
    if isinstance(error, OutputResourceInterruption):
        return "PROVIDER_OUTPUT_RESOURCE_EXHAUSTED"
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
    "FrozenPostResponseResourceQuote",
    "KernelRunResult",
    "OutputResourceInterruption",
]
