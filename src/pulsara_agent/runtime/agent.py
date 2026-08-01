"""Claude Code-like activation engine built from scoped run capabilities."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Literal
from uuid import uuid4

from pulsara_agent.capability.call_classifier import DefaultCapabilityCallClassifier
from pulsara_agent.capability.descriptor import CapabilityAvailability
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.runtime import (
    CapabilityRuntime,
    FrozenCapabilityExecutionSurface,
)
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    CapabilityGateDecisionEvent,
    ChildRolloutSubaccountClosedEvent,
    ConfirmResult,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionRequestedEvent,
    ContextCompactionStartedEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowClosedEvent,
    RolloutBudgetAccountClosedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunStartedEvent,
    SubagentRolloutBudgetResolvedEvent,
    EventContext,
    EventType,
    ModelCallRejectedEvent,
    ModelCallStartEvent,
    McpContinuationDispatchReservedEvent,
    McpInputRequiredBindingChangedEvent,
    McpInputRequiredExpiredEvent,
    McpInputRequiredInteractionClosedEvent,
    McpInputRequiredResumeFailedEvent,
    MidTurnContextCompactionSkippedEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    RecalledMemoryProjectionEntryFact,
    ProjectionRequestedEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    ToolResultEndEvent,
    ToolResultTerminalProjectionCommittedEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessMonitorRegisteredEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
    ToolResultDataDeltaEvent,
    ToolExecutionSuspendedEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.event.events import utc_now
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    freeze_event_write_candidate,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.llm.control import (
    ModelCallControlResolutionError,
)
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import (
    Msg,
    SystemMsg,
    ToolCallBlock,
    ToolCallState,
    ToolResultState,
    Usage,
)
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.long_horizon import ToolActionClassificationFact
from pulsara_agent.ports.run_execution import RunTerminalOutcome
from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpPendingTerminalReason,
    McpToolCompletedOutcome,
    McpToolRejectCode,
    McpToolRejectedOutcome,
    McpToolSuspendedOutcome,
    PreparedMcpInputRequiredResolution,
    build_mcp_tool_resume_request,
)
from pulsara_agent.ports.tool_registry import McpToolBindingContract
from pulsara_agent.primitives.mcp import (
    McpBindingIdentityFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    ContextCompactionRequestFact,
    McpInputRequiredSuspensionFact,
    McpInputRequiredTerminalSourceFact,
    PublicationLatchedRunTerminationFact,
    RuntimeEventOperationDeadlineBudget,
    build_bounded_runtime_failure_diagnostic,
    build_runtime_event_deadline_budget,
    ordered_fingerprint_accumulator,
    stable_runtime_event_id,
)
from pulsara_agent.replay.tool_result_receipts import (
    CurrentToolResultBatchReceipt,
    CurrentToolResultReceiptItem,
)
from pulsara_agent.runtime.context_engine.types import (
    CompiledContext,
    ContextBudgetExceeded,
    bind_compiled_context_to_provider_carrier,
)
from pulsara_agent.capability.tool_action import (
    ToolActionClassifierRegistry,
    default_tool_action_classifier_registry,
)
from pulsara_agent.runtime.approval import ApprovalResolution
from pulsara_agent.runtime.compaction.inline import (
    MidTurnCompactionResult,
    NoopRuntimeContextCompactor,
    RuntimeContextCompactorProtocol,
)
from pulsara_agent.ports.memory_hooks import (
    MemoryHookRunView,
    MemoryHooks,
    NoopMemoryHooks,
    build_memory_hook_run_view,
)
from pulsara_agent.ports.host_ingress import (
    HostIngressAdmissionStale,
    build_active_run_monitor_delivery,
)
from pulsara_agent.runtime.provider_input.causal import append_same_batch_user_steer
from pulsara_agent.runtime.loop_helpers import (
    _accumulate_usage,
    _final_text,
    _projection_ids,
    _projection_summary,
)
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    PermissionState,
    PolicyPermissionGate,
    PermissionDecisionKind,
    PermissionDecision,
    PermissionGate,
    TerminalAccess,
    default_permission_policy,
    evaluate_capability_exposure_access,
    mode_for_policy,
    preset_to_policy,
)
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.run_execution.owner import (
    RunActivationCoordinator,
    RunFinalizationOwner,
)
from pulsara_agent.runtime.run_execution.finalization import RunFinalizationService
from pulsara_agent.runtime.run_execution.model_step import ModelStepAttempt
from pulsara_agent.runtime.run_execution.tool_batch import ToolBatchAttempt
from pulsara_agent.ports.run_terminalization import (
    RunFinalOutputMaterializerPort,
    RunFinalOutputMaterializationFull,
    RunFinalOutputMaterializationReconciliationRequired,
    RunFinalOutputMaterializationRetryableUnavailable,
)
from pulsara_agent.runtime.context_input.candidate import (
    DEFAULT_SYSTEM_PROMPT,
    render_plan_revision_instruction,
)
from pulsara_agent.runtime.context_input.compiler import (
    canonical_render_decisions_fingerprint,
    compile_context_from_facts,
    lower_transcript_for_context,
    provider_neutral_payload_fingerprint,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.context_input.live import (
    ContextInputPreparationError,
    descriptor_render_attribution,
)
from pulsara_agent.runtime.context_input.manifest import (
    ContextInputManifestConfirmedAbsent,
    ContextInputManifestWriteResult,
    ContextInputManifestWriteConflict,
    ContextInputManifestWriteDeadlineExceeded,
    ContextInputManifestWriteOutcomeUnknown,
    build_context_compile_input_audit,
    build_context_input_manifest,
    build_context_input_manifest_candidate,
    build_context_input_manifest_projection_reference,
    build_long_horizon_context_attribution,
)
from pulsara_agent.runtime.context_input.transcript_authority import (
    prepare_transcript_projection_input,
)
from pulsara_agent.runtime.context_input.render import (
    apply_tool_observation_projection,
    render_prepared_tool_result_units,
    validate_prepared_tool_result_render_output,
)
from pulsara_agent.runtime.context_input.snapshot import (
    bind_context_invocation,
    build_context_snapshot,
)
from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.primitives.context import (
    CapabilityDescriptorRenderAttributionFact,
    ContextEventReferenceFact,
    ContextCompileFailureStage,
    ContextCompileInputFailureFact,
    ContextInputFailureReasonCode,
    FrozenJsonObjectFact,
    freeze_json,
)
from pulsara_agent.primitives.tool_result import (
    ToolResultRenderVariantCode,
    ToolResultStateFact,
)
from pulsara_agent.capability.result_semantics import (
    build_execution_semantics,
    build_pre_execution_denial_semantics,
    build_unknown_result_semantics,
    tool_origin_for_descriptor_variant,
)
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutUsageHandoffFact,
    ContextWindowCloseReason,
    RolloutBudgetBucket,
    RolloutPhase,
    RolloutReservationFact,
    build_child_rollout_usage_handoff,
    calculate_model_call_reservation,
    default_long_horizon_context_policy,
)
from pulsara_agent.primitives._context_base import context_fingerprint, thaw_json
from pulsara_agent.primitives.subagent import (
    ChildNativeTerminalReferenceFact,
)
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.primitives.run_lifecycle import RunTerminalizationKind
from pulsara_agent.runtime.permission_snapshot import (
    RunPermissionSnapshot,
    require_preset_permission_mode_for_policy,
    snapshot_from_mode,
    validate_preset_policy_payload,
)
from pulsara_agent.runtime.plan import (
    PlanExitResolution,
    PlanInteractionResolution,
    PlanQuestionResolution,
    PlanWorkflowState,
    normalize_plan_question_options,
    plan_workflow_state_fact,
)
from pulsara_agent.runtime.mcp.types import (
    MAX_MCP_INPUT_REQUIRED_ROUNDS,
)
from pulsara_agent.runtime.recovery import (
    AbortKind,
    InRunRecoveryCause,
    InRunRecoveryState,
)
from pulsara_agent.runtime.session import (
    EventBatchCommitOutcome,
    EventPublicationAfterCommitError,
    EventWriteConflict,
)
from pulsara_agent.runtime.session_run_capabilities import (
    RunRuntimeIdentity,
    RuntimeSessionRunAuditPort,
    RuntimeSessionRunContextPort,
    RuntimeSessionRunLedgerPort,
    RuntimeSessionRunLongHorizonPort,
    RuntimeSessionRunModelPort,
    RuntimeSessionRunToolPort,
)
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CommittedRunEntry,
    RunWorkingSet,
)
from pulsara_agent.runtime.long_horizon.rollout import apply_rollout_event
from pulsara_agent.runtime.long_horizon.store import advance_rollout_state
from pulsara_agent.runtime.long_horizon.coordinator import (
    allowed_action_classes_for_phase,
    build_rollout_phase_transition_event,
    plan_root_model_admission,
    plan_root_tool_admission,
    rollout_bucket_remaining,
)
from pulsara_agent.runtime.long_horizon.window_compaction_service import (
    WindowCompactionRequest,
)
from pulsara_agent.runtime.long_horizon.accounting import child_settlement_aggregate
from pulsara_agent.runtime.long_horizon.projection import (
    LongHorizonPreparationBoundExceeded,
    ProjectionTargetUnreachable,
    advance_compile_attempt_index,
    advance_safe_point_revision,
    plan_deterministic_projection_rewrite,
    plan_new_result_ingest,
    prepare_current_run_projection_planning_input,
    projection_target_unreachable_audit,
)
from pulsara_agent.runtime.long_horizon.rollup import (
    default_observation_rollup_renderer_registry,
    derive_rollup_placement_anchor,
    prepared_observation_rollup_cache_key,
    prepare_observation_rollup_artifact,
)
from pulsara_agent.runtime.long_horizon.context_budget import (
    long_horizon_context_diagnostics,
    measure_long_horizon_context_budget,
)
from pulsara_agent.runtime.long_horizon.feasibility import (
    ProductionRolloutBudgetFeasibilityReport,
    require_prevalidated_production_rollout_pair,
)
from pulsara_agent.runtime.tool_execution import build_tool_result_terminal_event
from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate
from pulsara_agent.runtime.long_horizon.run_contract import (
    prepare_child_rollout_reservation,
)
from pulsara_agent.primitives.long_horizon import LongHorizonReducerApplyError
from pulsara_agent.runtime.state import (
    LoopBudget,
    RunActivationWorkingState,
    LoopStatus,
    LoopTransition,
)
from pulsara_agent.runtime.subagent import (
    SubagentRuntime,
    SubagentRuntimeError,
)
from pulsara_agent.capability.builtin_catalog import PLAN_WORKFLOW_TOOL_NAMES
from pulsara_agent.runtime.tool_loop import (
    _ToolBatchTap,
    _duplicate_tool_call_ids,
    _parse_tool_call,
    _remember_tool_result_event_span,
    _tool_batches,
    _tool_call_blocks,
    _tool_result_from_event_slice,
    build_tool_result_error_events,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolRuntimeContext,
    ToolInvocationOwnerKind,
    tool_permission_invocation_from_snapshot,
)
from pulsara_agent.runtime.tool_executor import ToolExecutor
from pulsara_agent.runtime.tool_composition import (
    build_runtime_tool_executor,
)

WorkspaceKind = Literal["project", "transient"]


async def _await_sync_tool_thread(
    operation: Callable[[], ToolExecutionResult | ToolExecutionSuspended],
    *,
    release_borrow: Callable[[], None],
) -> ToolExecutionResult | ToolExecutionSuspended:
    """Keep execution ownership until the real worker thread has returned."""

    thread_coroutine = asyncio.to_thread(operation)
    try:
        thread_task = asyncio.create_task(thread_coroutine)
    except BaseException:
        thread_coroutine.close()
        release_borrow()
        raise
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        # The tool thread may still emit result events or mutate external
        # state.  Keep the run task alive until that real execution boundary
        # closes.  Return its actual outcome so the runtime can durably settle
        # the admitted call before the cancelled batch unwinds.
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        return thread_task.result()
    finally:
        release_borrow()


_PLAN_REVISION_REQUIRED_INSTRUCTION_NAME = "plan_revision_required_instruction"
_SUBAGENT_RESULTS_SECTION_ID = "subagent:results"
_TERMINAL_CAPABILITY_CONTEXT_TOOL_NAMES = frozenset(
    {"terminal", "terminal_process", "terminal_monitor"}
)
_KNOWN_CAPABILITY_GATE_REASON_CODES = frozenset(
    {
        "capability_descriptor_missing",
        "capability_hidden",
        "capability_unavailable",
        "capability_not_callable",
        "permission_denied",
        "permission_wait_for_user",
        "permission_wait_for_user_batch_suspension",
        "subagent_requires_bypass_mode",
        "workflow_control_batch_suppressed",
        "mcp_resume_permission_approval_unsupported",
        "hardline_terminal_command_blocked",
        "hardline_terminal_process_input_blocked",
        "rollout_emergency_hard_stop",
        "rollout_phase_tool_denied",
        "rollout_tool_budget_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityGateDecisionFact:
    tool_call_id: str
    tool_name: str
    descriptor_id: str | None
    decision: PermissionDecisionKind
    reason_code: str | None = None
    reason_message: str | None = None
    suggested_rules: tuple[dict[str, Any], ...] = ()
    result_state: ToolResultState | None = None
    policy_mode: str | None = None
    permission_policy: dict[str, Any] = field(default_factory=dict)
    exposure_generation: int | None = None
    availability: CapabilityAvailability | None = None
    permission_category: str | None = None
    effective_permission_category: str | None = None
    effective_read_only: bool | None = None
    capability_context: dict[str, Any] = field(default_factory=dict)
    action_classification: ToolActionClassificationFact | None = None


def _terminal_capability_context(
    call: ToolCall,
    exposure: CapabilityExposurePlan,
) -> dict[str, object] | None:
    if call.name not in _TERMINAL_CAPABILITY_CONTEXT_TOOL_NAMES:
        return None
    if not exposure.active_injections:
        return None
    active_skill_names = tuple(
        injection.name for injection in exposure.active_injections
    )
    context: dict[str, object] = {
        "active_skill_names": list(active_skill_names),
        "context_kind": "active_skill_present",
    }
    suggested_tools = _merged_skill_values(
        exposure.active_injections, "suggested_tools"
    )
    if suggested_tools:
        context["skill_suggested_tools"] = suggested_tools
    required_binaries = _merged_skill_values(
        exposure.active_injections, "required_binaries"
    )
    if required_binaries:
        context["cli_required_binaries"] = required_binaries
    optional_binaries = _merged_skill_values(
        exposure.active_injections, "optional_binaries"
    )
    if optional_binaries:
        context["cli_optional_binaries"] = optional_binaries
    external_services = _merged_skill_values(
        exposure.active_injections, "external_services"
    )
    if external_services:
        context["cli_external_services"] = external_services
    cli_usage_kinds = sorted(
        {
            injection.cli_usage_kind
            for injection in exposure.active_injections
            if injection.cli_usage_kind != "none"
        }
    )
    if cli_usage_kinds:
        context["cli_usage_kinds"] = cli_usage_kinds
    auth_required = _max_auth_required(exposure.active_injections)
    if auth_required != "none":
        context["auth_required"] = auth_required
    if any(injection.network_required for injection in exposure.active_injections):
        context["network_required"] = True
    return context


def _merged_skill_values(
    injections: tuple[ActiveSkillInjection, ...], field_name: str
) -> list[str]:
    values: set[str] = set()
    for injection in injections:
        values.update(getattr(injection, field_name))
    return sorted(values)


def _max_auth_required(injections: tuple[ActiveSkillInjection, ...]) -> str:
    rank = {"none": 0, "optional": 1, "required": 2}
    return max(
        (injection.auth_required for injection in injections),
        key=lambda value: rank[value],
        default="none",
    )


def _normalize_capability_gate_reason(
    decision: PermissionDecision,
    *,
    reason_code_override: str | None = None,
) -> tuple[str | None, str | None]:
    reason = decision.reason
    if reason_code_override is not None:
        return reason_code_override, reason
    if reason is None:
        return None, None
    if reason in _KNOWN_CAPABILITY_GATE_REASON_CODES:
        return reason, reason
    if "capability_descriptor_missing" in reason:
        return "capability_descriptor_missing", reason
    if (
        reason.startswith("capability_hidden")
        or "capability_hidden_in_current_exposure" in reason
    ):
        return "capability_hidden", reason
    if (
        reason.startswith("capability_unavailable")
        or "capability_unavailable_in_current_exposure" in reason
    ):
        return "capability_unavailable", reason
    if (
        reason.startswith("capability_not_callable")
        or "capability_not_callable_in_current_exposure" in reason
    ):
        return "capability_not_callable", reason
    if reason.startswith("tool call suppressed because workflow control tool"):
        return "workflow_control_batch_suppressed", reason
    if "mcp_resume_permission_approval_unsupported" in reason:
        return "mcp_resume_permission_approval_unsupported", reason
    if reason == "terminal command blocked by hardline permission policy":
        return "hardline_terminal_command_blocked", reason
    if reason == "terminal process input blocked by hardline permission policy":
        return "hardline_terminal_process_input_blocked", reason
    if decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
        return "permission_wait_for_user", reason
    if (
        decision.kind is PermissionDecisionKind.DENY
        and "not allowed by permission policy" in reason
    ):
        return "permission_denied", reason
    return None, reason


def _call_matches_suggested_rule(
    call: ToolCall, suggested_rules: list[dict] | tuple[dict[str, Any], ...]
) -> bool:
    for rule in suggested_rules:
        if rule.get("tool") == call.name:
            return True
    return False


def _suppressed_by_workflow_control_decision(
    workflow_call: ToolCall,
) -> PermissionDecision:
    return PermissionDecision(
        kind=PermissionDecisionKind.DENY,
        reason=(
            f"tool call suppressed because workflow control tool '{workflow_call.name}' "
            "owns this tool batch"
        ),
    )


def _mcp_terminal_reason_from_projection(
    *,
    disposition_kind: Literal["expired", "binding_changed"] | None,
    closure_reason: str | None,
    result_state: ToolResultState,
) -> McpPendingTerminalReason:
    if disposition_kind == "expired":
        return McpPendingTerminalReason.INTERACTION_EXPIRED
    if disposition_kind == "binding_changed":
        return McpPendingTerminalReason.BINDING_CHANGED
    if closure_reason == "child_pending_unsupported":
        return McpPendingTerminalReason.CHILD_PENDING_UNSUPPORTED
    if closure_reason is not None:
        return McpPendingTerminalReason.PUBLICATION_TERMINALIZATION
    if result_state is ToolResultState.DENIED:
        return McpPendingTerminalReason.PERMISSION_DENIED
    return McpPendingTerminalReason.COMPLETED_RESULT


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class AgentRunResult:
    status: LoopStatus
    stop_reason: RunStopReason | None
    run_id: str
    messages: list[Msg]
    final_text: str
    token_usage: Usage
    tool_call_count: int
    pending_interaction_kind: str | None
    finalized: bool
    error_message: str | None = None


def agent_run_result_from_terminal_outcome(
    outcome: RunTerminalOutcome,
) -> AgentRunResult:
    """Render the legacy public shell only from canonical terminal authority."""

    output = outcome.output
    return AgentRunResult(
        status={
            "finished": LoopStatus.FINISHED,
            "failed": LoopStatus.FAILED,
            "aborted": LoopStatus.ABORTED,
        }[output.status],
        stop_reason=output.stop_reason,
        run_id=outcome.owner_identity.run_id,
        messages=[],
        final_text=output.final_text or "",
        token_usage=Usage(
            input_tokens=output.usage.input_tokens,
            output_tokens=output.usage.output_tokens,
            total_tokens=output.usage.total_tokens,
        ),
        tool_call_count=output.tool_call_count,
        pending_interaction_kind=None,
        finalized=True,
        error_message=None,
    )


class AgentRuntime:
    def __init__(
        self,
        *,
        run_identity: RunRuntimeIdentity,
        run_ledger_port: RuntimeSessionRunLedgerPort,
        run_context_port: RuntimeSessionRunContextPort,
        run_model_port: RuntimeSessionRunModelPort,
        run_tool_port: RuntimeSessionRunToolPort,
        run_long_horizon_port: RuntimeSessionRunLongHorizonPort,
        run_audit_port: RuntimeSessionRunAuditPort,
        llm_runtime: LLMRuntime,
        memory_hooks: MemoryHooks | None = None,
        permission_gate: PermissionGate | None = None,
        model_role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        budget: LoopBudget | None = None,
        system_prompt: str | None = None,
        capability_runtime: CapabilityRuntime,
        memory_domain: MemoryDomainContext | None = None,
        workspace_kind: WorkspaceKind = "transient",
        permission_policy: EffectivePermissionPolicy | None = None,
        context_compactor: RuntimeContextCompactorProtocol | None = None,
        subagent_runtime: SubagentRuntime | None = None,
        enable_subagents: bool = True,
        run_execution_registry: RunExecutionRegistry | None = None,
    ) -> None:
        if capability_runtime is None:
            raise ValueError("AgentRuntime requires an explicit CapabilityRuntime")
        self._run_identity = run_identity
        self._run_ledger = run_ledger_port
        self._run_context = run_context_port
        self._run_model = run_model_port
        self._run_tools = run_tool_port
        self._run_long_horizon = run_long_horizon_port
        self._run_audit = run_audit_port
        self.run_execution_registry = run_execution_registry
        self._run_finalization_service = (
            RunFinalizationService(registry=run_execution_registry)
            if run_execution_registry is not None
            else None
        )
        self._run_reconciliation_service = None
        self.llm_runtime = llm_runtime
        self.memory_hooks = memory_hooks or NoopMemoryHooks()
        policy = permission_policy or default_permission_policy()
        permission_mode = require_preset_permission_mode_for_policy(
            policy,
            context="AgentRuntime production permission policy",
        )
        # Session default holder. It is used to resolve the next run's immutable
        # RunPermissionSnapshot; it is no longer the in-run permission fact
        # source.
        self._permission_state = PermissionState(policy=policy, mode=permission_mode)
        self.permission_gate = PolicyPermissionGate(
            self._permission_state,
            inner=permission_gate or AllowAllPermissionGate(),
        )
        self.model_role = model_role
        self.options = options
        self.budget = budget or LoopBudget()
        self.system_prompt = system_prompt
        self.capability_runtime = capability_runtime
        self.context_compactor = context_compactor or NoopRuntimeContextCompactor()
        self.memory_domain = memory_domain
        self.workspace_kind = workspace_kind
        self._is_subagent_child = run_identity.is_subagent_child
        self._subagent_parent_features_enabled = (
            enable_subagents and not self._is_subagent_child
        )
        self.subagent_runtime = subagent_runtime
        if self._subagent_parent_features_enabled and self.subagent_runtime is None:
            self.subagent_runtime = run_tool_port.ensure_subagent_runtime(enabled=True)
        if self._subagent_parent_features_enabled and self.subagent_runtime is not None:
            self.subagent_runtime.bind_rollout_admission(
                self._prepare_child_rollout_admission_events
            )
            self.subagent_runtime.bind_rollout_terminal_augmenter(
                self._prepare_child_rollout_terminal_events
            )
        self._mcp_terminal_commit_outcomes: dict[
            tuple[str, str],
            Literal["not_attempted", "attempting", "none", "full", "untrusted"],
        ] = {}
        self._mcp_terminal_pending_handles: dict[tuple[str, str], object] = {}
        self.tool_action_classifier_registry: ToolActionClassifierRegistry = (
            default_tool_action_classifier_registry()
        )
        self.rollout_budget_feasibility_report: (
            ProductionRolloutBudgetFeasibilityReport | None
        ) = None
        self.observation_rollup_renderer_registry = (
            default_observation_rollup_renderer_registry()
        )
        self.window_compaction_service = run_context_port.window_compaction_service(
            llm_runtime=llm_runtime
        )
        self._tool_composition_input = run_tool_port.build_composition_input(
            memory_hooks=self.memory_hooks,
            subagent_runtime=self.subagent_runtime,
        )
        self.tool_executor = build_runtime_tool_executor(self._tool_composition_input)

    def require_prevalidated_rollout_pair(
        self,
        *,
        execution_profile_kind: Literal["host_root", "subagent_child"],
        execution_profile_id: str,
        primary_target: ResolvedModelTarget,
        summarizer_target: ResolvedModelTarget,
    ) -> None:
        report = self.rollout_budget_feasibility_report
        if report is None:
            return
        require_prevalidated_production_rollout_pair(
            report=report,
            execution_profile_kind=execution_profile_kind,
            execution_profile_id=execution_profile_id,
            primary_target_slot=primary_target.fact.model_role,
            primary_target=primary_target.fact,
            summarizer_target_slot=summarizer_target.fact.model_role,
            summarizer_target=summarizer_target.fact,
        )

    def result_from_owned_state(
        self, state: RunActivationWorkingState | None
    ) -> AgentRunResult:
        if state is None:
            raise RuntimeError("run owner has no resident activation state")
        return self._run_result(state)

    def refresh_capability_runtime(self, capability_runtime: CapabilityRuntime) -> None:
        """Replace per-turn capability facts and rebuild the executor registry.

        MCP descriptors and execution bindings are session-owned and may change
        after a reconnect/backoff sync.  Rebuilding here keeps the model-facing
        exposure plan and the executable ToolRegistry on the same snapshot.
        """
        if capability_runtime is None:
            raise ValueError("AgentRuntime requires an explicit CapabilityRuntime")
        self.capability_runtime = capability_runtime
        self._tool_composition_input = self._run_tools.build_composition_input(
            memory_hooks=self.memory_hooks,
            subagent_runtime=self.subagent_runtime,
        )
        self.tool_executor = build_runtime_tool_executor(self._tool_composition_input)

    async def prepare_run_draft(
        self,
        state: RunActivationWorkingState,
        **kwargs,
    ) -> AgentRunDraft:
        """Freeze one RunStart through the scoped context authority."""

        from pulsara_agent.runtime.run_entry import prepare_agent_run_draft

        frozen_surface = kwargs.get("frozen_execution_surface")
        if not isinstance(frozen_surface, FrozenCapabilityExecutionSurface):
            raise TypeError("run draft requires a frozen execution surface")
        run_identity = replace(
            self._run_identity,
            mcp_installation_id=(frozen_surface.identity.mcp_installation_id),
        )
        return await prepare_agent_run_draft(
            state,
            run_identity=run_identity,
            run_context_port=self._run_context,
            **kwargs,
        )

    async def commit_run_entry_events(
        self, events: tuple[AgentEvent, ...]
    ) -> tuple[AgentEvent, ...]:
        return tuple(await self._run_ledger.emit_many(events))

    def resolve_run_entry_write_failure(self, error: BaseException):
        return self._run_ledger.resolved_write_outcome(error)

    def discard_prepared_run_seed(self, run_id: str) -> None:
        self._run_context.discard_prepared_run_seed(run_id)

    def adopt_committed_run_seed(self, run_start: RunStartEvent) -> None:
        self._run_context.adopt_committed_run_seed(run_start)

    async def request_model_cancel(self, run_id: str, *, reason: str) -> int:
        return await self._run_model.request_cancel_run(run_id, reason=reason)

    @property
    def permission_policy(self) -> EffectivePermissionPolicy:
        return self._permission_state.policy

    @property
    def runtime_session_id(self) -> str:
        return self._run_identity.runtime_session_id

    def event_reconciliation_required(self) -> bool:
        """Expose only the ledger latch needed by the run-owner reducer."""

        return self._run_ledger.reconciliation_required

    @property
    def permission_mode(self) -> PermissionMode | None:
        return self._permission_state.mode

    def set_permission_policy(
        self,
        policy: EffectivePermissionPolicy,
        *,
        mode: PermissionMode | None = None,
    ) -> None:
        """Set the session default permission policy for future runs."""
        resolved_mode = mode if mode is not None else mode_for_policy(policy)
        if resolved_mode is None:
            raise ValueError(
                "AgentRuntime session default requires a preset permission mode"
            )
        validate_preset_policy_payload(
            resolved_mode,
            policy.to_dict(),
            context="AgentRuntime session default",
        )
        self._permission_state.policy = policy
        self._permission_state.mode = resolved_mode

    async def resume_after_approval(
        self,
        state: RunActivationWorkingState,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_approval(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_approval(
        self,
        state: RunActivationWorkingState,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_approval_resolution(state, resolution):
            yield event

    async def resume_after_plan_interaction(
        self,
        state: RunActivationWorkingState,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_plan_interaction(state, resolution):
            pass
        return self._run_result(state)

    async def resume_after_mcp_input_required(
        self,
        state: RunActivationWorkingState,
        resolution: PreparedMcpInputRequiredResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_mcp_input_required(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_plan_interaction(
        self,
        state: RunActivationWorkingState,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_plan_interaction_resolution(state, resolution):
            yield event

    async def stream_after_mcp_input_required(
        self,
        state: RunActivationWorkingState,
        resolution: PreparedMcpInputRequiredResolution,
    ) -> AsyncIterator[AgentEvent]:
        original_pending_tool_calls = list(state.pending_tool_calls)
        original_pending_kind = state.pending_interaction_kind
        original_pending_payload = dict(state.pending_interaction_payload)
        original_status = state.status
        original_stop_reason = state.stop_reason
        original_transition = state.last_transition
        tool_call_id = _required_str(
            state.pending_interaction_payload.get("tool_call_id"),
            "pending MCP tool_call_id",
        )
        tool_name = _required_str(
            state.pending_interaction_payload.get("tool_name"),
            "pending MCP tool_name",
        )
        commit_outcome_key = (state.run_id, tool_call_id)
        pending_handle = state.pending_interaction_payload.get("mcp_pending_handle")
        if pending_handle is None or not hasattr(pending_handle, "identity"):
            raise RuntimeError("MCP resume lost its process-local pending handle")
        if commit_outcome_key in self._mcp_terminal_commit_outcomes:
            raise RuntimeError("MCP terminal result commit is already active")
        self._mcp_terminal_commit_outcomes[commit_outcome_key] = "not_attempted"
        self._mcp_terminal_pending_handles[commit_outcome_key] = pending_handle
        try:
            async for event in self._stream_mcp_input_required_resolution(
                state, resolution
            ):
                yield event
        except BaseException:
            commit_outcome = self._mcp_terminal_commit_outcomes.pop(
                commit_outcome_key,
                "untrusted",
            )
            self._mcp_terminal_pending_handles.pop(commit_outcome_key, None)
            if commit_outcome == "full":
                committed_result_events = self._committed_tool_result_events(
                    state,
                    tool_call_id=tool_call_id,
                    start_event_id=_pending_tool_result_start_event_id(
                        original_pending_payload
                    ),
                )
                if not committed_result_events:
                    self._run_ledger.latch_event_commit_outcome_unknown()
                    state.pending_tool_calls = original_pending_tool_calls
                    state.pending_interaction_kind = original_pending_kind
                    state.pending_interaction_payload = original_pending_payload
                    state.status = original_status
                    state.stop_reason = original_stop_reason
                    state.last_transition = original_transition
                    raise
                state.pending_tool_calls = []
                state.pending_interaction_kind = None
                state.pending_interaction_payload = {}
                state.status = LoopStatus.RUNNING
                state.stop_reason = None
                self._record_tool_result_events(
                    state,
                    stored_events=committed_result_events,
                    tool_call_id=tool_call_id,
                    tool_call_name=tool_name,
                )
            else:
                state.pending_tool_calls = original_pending_tool_calls
                state.pending_interaction_kind = original_pending_kind
                state.pending_interaction_payload = original_pending_payload
                state.status = original_status
                state.stop_reason = original_stop_reason
                state.last_transition = original_transition
            raise
        else:
            self._mcp_terminal_commit_outcomes.pop(commit_outcome_key, None)
            self._mcp_terminal_pending_handles.pop(commit_outcome_key, None)

    async def abort_run(
        self,
        state: RunActivationWorkingState,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult:
        async for _event in self.stream_abort_run(state, reason=reason):
            pass
        return self._run_result(state)

    async def fail_committed_run(
        self,
        state: RunActivationWorkingState,
        *,
        stop_reason: RunStopReason,
        error_message: str,
    ) -> AgentRunResult:
        """Terminalize a committed run with one stable execution-failure fact."""

        if state.finalized:
            return self._run_result(state)
        state.status = LoopStatus.FAILED
        state.stop_reason = stop_reason
        state.error_message = error_message
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.transition(LoopTransition.FAIL)
        async for _event in self._finalize_run(state, run_session_end_hook=False):
            pass
        return self._run_result(state)

    def prepare_failed_run_terminalization(
        self,
        state: RunActivationWorkingState,
        *,
        stop_reason: RunStopReason,
        error_message: str,
    ) -> RunEndEvent:
        """Freeze the one RunEnd owned after a driver can no longer continue."""

        if state.finalized:
            raise RuntimeError("a finalized run cannot freeze another RunEnd")
        state.status = LoopStatus.FAILED
        state.stop_reason = stop_reason
        state.error_message = error_message
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.pending_interaction_source_event_reference = None
        state.pending_interaction_source_event_candidate = None
        if state.last_transition is not LoopTransition.FAIL:
            state.transition(LoopTransition.FAIL)
        return self._freeze_run_end_candidate(state)

    def continue_run_terminalization(
        self,
        state: RunActivationWorkingState,
    ) -> asyncio.Task[tuple[AgentEvent, ...]]:
        """Give retry ownership to the stable finalization service."""

        service = self._run_finalization_service
        if service is None:
            raise RuntimeError("run finalization service is not installed")
        return service.continue_terminalization(
            run_id=state.run_id,
            state=state,
            operation=lambda: self._execute_run_finalization(
                state,
                run_session_end_hook=False,
            ),
        )

    async def retry_run_terminalization(
        self, state: RunActivationWorkingState
    ) -> AgentRunResult:
        """Retry one frozen RunEnd candidate without changing its run outcome."""

        if not state.finalized:
            async for _event in self._finalize_run(
                state,
                run_session_end_hook=False,
            ):
                pass
        return self._run_result(state)

    async def stream_abort_run(
        self,
        state: RunActivationWorkingState,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AsyncIterator[AgentEvent]:
        if state.finalized:
            return
        if state.status in {LoopStatus.FINISHED, LoopStatus.FAILED, LoopStatus.ABORTED}:
            async for event in self._finalize_run(
                state,
                run_session_end_hook=False,
            ):
                yield event
            return
        if state.pending_interaction_kind == "mcp_input_required":
            tool_call_id = _required_str(
                state.pending_interaction_payload.get("tool_call_id"),
                "pending MCP tool call id",
            )
            handle = state.pending_interaction_payload.get("mcp_pending_handle")
            if handle is None or not hasattr(handle, "identity"):
                raise RuntimeError("pending MCP abort lost its physical owner")
            key = (state.run_id, tool_call_id)
            self._mcp_terminal_commit_outcomes[key] = "not_attempted"
            self._mcp_terminal_pending_handles[key] = handle
            try:
                async for event in self._terminalize_pending_mcp_for_abort(
                    state,
                    reason=reason,
                ):
                    yield event
            finally:
                self._mcp_terminal_commit_outcomes.pop(key, None)
                self._mcp_terminal_pending_handles.pop(key, None)
        elif state.pending_interaction_kind == "plan":
            async for event in self._terminalize_pending_plan_for_abort(
                state,
                reason=reason,
            ):
                yield event
        elif state.status is LoopStatus.WAITING_USER and state.pending_tool_calls:
            async for event in self._terminalize_pending_approval_for_abort(
                state,
                reason=reason,
            ):
                yield event
        state.status = LoopStatus.ABORTED
        state.stop_reason = RunStopReason.ABORTED
        state.error_message = None
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.stop_request = None
        state.abort_kind = reason
        async for event in self._finalize_run(state):
            yield event

    async def _terminalize_pending_approval_for_abort(
        self,
        state: RunActivationWorkingState,
        *,
        reason: AbortKind,
    ) -> AsyncIterator[AgentEvent]:
        """Close accepted tool calls before an approval-suspended run aborts."""

        reason_code = f"pending_approval_aborted:{reason.value}"
        for block in tuple(state.pending_tool_calls):
            stored_events = await self._run_ledger.emit_many(
                self._typed_tool_result_error_events(
                    state,
                    tool_call_id=block.id,
                    tool_call_name=block.name,
                    message=(
                        "tool call denied because the pending approval "
                        "was stopped before execution"
                    ),
                    result_state=ToolResultState.DENIED,
                    arguments=_tool_block_arguments_for_semantics(block),
                    failure_stage="permission_denied",
                    reason_code=reason_code,
                ),
            )
            for event in stored_events:
                yield event
            self._record_tool_result_events(
                state,
                stored_events=list(stored_events),
                tool_call_id=block.id,
                tool_call_name=block.name,
            )

    async def _terminalize_pending_mcp_for_abort(
        self,
        state: RunActivationWorkingState,
        *,
        reason: AbortKind,
    ) -> AsyncIterator[AgentEvent]:
        payload = dict(state.pending_interaction_payload)
        reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        tool_call_id = _required_str(
            payload.get("tool_call_id"),
            "pending MCP tool call id",
        )
        tool_name = _required_str(
            payload.get("tool_name"),
            "pending MCP tool name",
        )
        pending_handle = payload.get("mcp_pending_handle")
        if pending_handle is None or not hasattr(
            pending_handle, "suspension_commit_view"
        ):
            raise RuntimeError("pending MCP abort lost its process-local handle")
        timing_seed = dict(payload.get("tool_observation_timing_seed") or {})
        terminal_source = None
        resolution_ref = (
            state.run_working_set.latest_mcp_input_required_resolution_ref
            if state.run_working_set is not None
            else None
        )
        suspension_ref = payload.get("source_suspension_event_reference")
        if isinstance(suspension_ref, ContextEventReferenceFact):
            terminal_source = build_frozen_fact(
                McpInputRequiredTerminalSourceFact,
                schema_version="mcp_input_required_terminal_source.v1",
                source_suspension_event_reference=suspension_ref,
                source_resolution_submitted_event_reference=(
                    resolution_ref
                    if isinstance(resolution_ref, ContextEventReferenceFact)
                    else None
                ),
            )
        finalization = self._require_run_finalization_owner(state)
        closure_reason = finalization.mcp_publication_closure_reason
        finalization.mcp_publication_closure_reason = None
        if closure_reason is not None and closure_reason not in {
            "suspension_publication_unavailable",
            "resume_boundary_publication_unavailable",
            "resume_failed_publication_unavailable",
            "session_reopen_lease_unavailable",
            "child_pending_unsupported",
            "live_pending_lease_unavailable",
        }:
            raise RuntimeError("pending MCP closure reason is invalid")
        deadline_budget = finalization.publication_deadline_budget
        if not isinstance(deadline_budget, RuntimeEventOperationDeadlineBudget):
            deadline_budget = build_runtime_event_deadline_budget(
                admitted_at_monotonic=time.monotonic(),
                total_timeout_seconds=30.0,
                terminal_reserve_seconds=10.0,
            )
        committed: tuple[AgentEvent, ...] = ()
        try:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                output=(
                    "MCP input-required interaction was interrupted because the "
                    f"owning run stopped ({reason.value})."
                ),
                result_state=ToolResultState.INTERRUPTED,
                tool_arguments={},
                tool_observation_timing_seed=(
                    {**timing_seed, "resumed_at": utc_now()} if timing_seed else None
                ),
                rollout_reservation=reservation,
                mcp_input_required_terminal_source=terminal_source,
                mcp_closure_reason=closure_reason,
                mcp_terminal_reason=(
                    McpPendingTerminalReason.PUBLICATION_TERMINALIZATION
                    if closure_reason is not None
                    else McpPendingTerminalReason.HOST_ABORT
                ),
                deadline_budget=deadline_budget,
            ):
                committed = (*committed, event)
                yield event
        except EventPublicationAfterCommitError as exc:
            committed = tuple(exc.result.committed_events)
            for event in exc.result.committed_events:
                yield event
            self._install_mcp_publication_latched_termination(
                state,
                committed_events=committed,
                reason=(
                    "mcp_closure_publication_unavailable"
                    if closure_reason is not None
                    else "mcp_terminal_disposition_publication_unavailable"
                ),
                deadline_budget=deadline_budget,
            )
        closure = next(
            (
                event
                for event in committed
                if isinstance(event, McpInputRequiredInteractionClosedEvent)
            ),
            None,
        )
        if closure is not None:
            finalization.mcp_closure_event_reference = event_reference_from_stored(
                closure,
                runtime_session_id=self._run_identity.runtime_session_id,
            )

    async def _terminalize_pending_plan_for_abort(
        self,
        state: RunActivationWorkingState,
        *,
        reason: AbortKind,
    ) -> AsyncIterator[AgentEvent]:
        payload = dict(state.pending_interaction_payload)
        reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        tool_call_id = _required_str(
            payload.get("tool_call_id"),
            "pending plan tool call id",
        )
        tool_name = (
            "ask_plan_question" if payload.get("kind") == "question" else "exit_plan"
        )
        try:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                output=(
                    "plan interaction was interrupted because the owning run "
                    f"stopped ({reason.value})"
                ),
                result_state=ToolResultState.INTERRUPTED,
                tool_arguments=payload,
                failure_stage="workflow_interrupted",
                rollout_reservation=reservation,
            ):
                yield event
        except EventPublicationAfterCommitError as exc:
            for event in exc.result.committed_events:
                yield event

    def close(self) -> None:
        # RuntimeSession and process-wide services are composition-owned.  This
        # activation engine only owns the run-local attempts it installs.
        return None

    def bind_run_execution_registry(self, registry: RunExecutionRegistry) -> None:
        current = self.run_execution_registry
        if current is not None and current is not registry:
            raise RuntimeError("AgentRuntime is already bound to another run registry")
        self.run_execution_registry = registry
        if self._run_finalization_service is None:
            self._run_finalization_service = RunFinalizationService(registry=registry)

    def bind_run_reconciliation_service(self, service) -> None:
        current = self._run_reconciliation_service
        if current is not None and current is not service:
            raise RuntimeError(
                "AgentRuntime is already bound to another reconciliation owner"
            )
        self._run_reconciliation_service = service

    async def drain_run_finalizations(self, *, deadline_monotonic: float) -> None:
        service = self._run_finalization_service
        if service is not None:
            await service.drain(deadline_monotonic=deadline_monotonic)

    def _prepare_child_rollout_admission_events(
        self,
        started_events: tuple[SubagentRunStartedEvent, ...],
    ) -> tuple[AgentEvent, ...]:
        if not started_events:
            return ()
        ordered = tuple(sorted(started_events, key=lambda event: event.subagent_run_id))
        parent_run_ids = {event.parent_run_id for event in ordered}
        parent_runtime_ids = {event.parent_runtime_session_id for event in ordered}
        if parent_run_ids != {ordered[0].run_id} or parent_runtime_ids != {
            self._run_identity.runtime_session_id
        }:
            raise RuntimeError("child rollout admission parent attribution mismatch")
        parent_run_id = ordered[0].parent_run_id
        parent_start = self._run_long_horizon.store.run_start(parent_run_id)
        if parent_start is None:
            raise RuntimeError("child rollout admission requires one parent RunStart")
        if any(
            started.budget_snapshot.child_rollout_policy
            != parent_start.long_horizon.child_rollout_policy
            for started in ordered
        ):
            raise RuntimeError(
                "child budget snapshot rollout policy differs from parent RunStart"
            )
        account_id = parent_start.long_horizon.rollout_account_id
        account = self._run_long_horizon.store.rollout_account(account_id)
        state = self._run_long_horizon.store.rollout_state(account_id)
        if account is None or state is None:
            raise RuntimeError("child rollout admission lost the parent account")
        if state.phase.value != "exploration":
            raise RuntimeError("child rollout admission requires exploration phase")
        if (
            sum(
                1
                for reservation in state.active_reservations
                if reservation.owner_kind == "subagent_run"
            )
            + len(ordered)
            > account.policy.max_concurrent_subagent_reservations
        ):
            raise RuntimeError("subagent rollout reservation concurrency exceeded")

        child_primary_target = self.resolve_run_model_target().fact
        child_summarizer_target = self.llm_runtime.resolve_target(
            role=ModelRole.FLASH
        ).fact
        child_window_policy = default_long_horizon_context_policy(
            input_budget_tokens=(
                child_primary_target.context_budget.input_budget_tokens
            )
        )
        prepared = tuple(
            (
                started,
                prepare_child_rollout_reservation(
                    child_profile=started.profile_id or "primitive_worker",
                    child_run_id=started.subagent_run_id,
                    child_primary_target=child_primary_target,
                    child_summarizer_target=child_summarizer_target,
                    child_window_policy_fingerprint=(
                        child_window_policy.policy_fingerprint
                    ),
                    parent_account=account,
                    parent_state=state,
                    source_sequence=(self._run_long_horizon.store.through_sequence),
                    child_policy=parent_start.long_horizon.child_rollout_policy,
                ),
            )
            for started in ordered
        )
        exploration_remaining = (
            account.exploration_allowance_milliunits
            - state.exploration_charged_milliunits
            - state.exploration_reserved_milliunits
        )
        if (
            sum(item.reservation.reserved_milliunits for _started, item in prepared)
            > exploration_remaining
        ):
            raise RuntimeError("subagent batch rollout reservation unavailable")

        events: list[AgentEvent] = []
        for started, admission in prepared:
            context = EventContext(
                run_id=started.run_id,
                turn_id=started.turn_id,
                reply_id=started.reply_id,
            )
            events.extend(
                (
                    SubagentRolloutBudgetResolvedEvent(
                        id=(
                            "subagent_rollout_budget_resolved:"
                            f"{started.subagent_run_id}"
                        ),
                        **context.event_fields(),
                        subagent_run_id=started.subagent_run_id,
                        subagent_task_id=started.task_id,
                        budget_snapshot_event_id=started.id,
                        resolved_budget=admission.resolved_budget,
                    ),
                    RolloutBudgetReservationCreatedEvent(
                        id=(
                            "rollout_budget_reservation_created:"
                            f"{admission.reservation.reservation_id}"
                        ),
                        **context.event_fields(),
                        reservation=admission.reservation,
                    ),
                )
            )
        return tuple(events)

    def _prepare_child_rollout_terminal_events(
        self,
        events: tuple[AgentEvent, ...],
    ) -> tuple[AgentEvent, ...]:
        terminal_events = tuple(
            event
            for event in events
            if isinstance(
                event,
                (
                    SubagentRunCompletedEvent,
                    SubagentRunFailedEvent,
                    SubagentRunCancelledEvent,
                ),
            )
        )
        if not terminal_events:
            return events

        existing_settlement_ids = {
            event.reservation_id
            for event in events
            if isinstance(event, RolloutBudgetReservationSettledEvent)
        }
        augmented = list(events)
        for terminal in terminal_events:
            parent_start = _run_start_for_id(
                self._run_long_horizon.store,
                run_id=terminal.run_id,
            )
            account_id = parent_start.long_horizon.rollout_account_id
            account_state = self._run_long_horizon.store.rollout_state(account_id)
            if account_state is None:
                admission = self._run_ledger.get_event(
                    f"subagent_rollout_budget_resolved:{terminal.subagent_run_id}"
                )
                if not isinstance(admission, SubagentRolloutBudgetResolvedEvent):
                    continue
                raise RuntimeError("child terminal settlement lost root account state")
            reservations = tuple(
                reservation
                for reservation in account_state.active_reservations
                if reservation.owner_kind == "subagent_run"
                and reservation.owner_id == terminal.subagent_run_id
            )
            if not reservations:
                # Test-only graph runtimes may not bind the rollout admission port.
                continue
            if len(reservations) != 1:
                raise RuntimeError("child terminal has ambiguous rollout reservation")
            reservation = reservations[0]
            if reservation.reservation_id in existing_settlement_ids:
                continue

            child_terminal_reference = (
                terminal.result_handoff.child_terminal_reference
                if isinstance(terminal, SubagentRunCompletedEvent)
                else terminal.child_terminal_reference
            )
            handoff: ChildRolloutUsageHandoffFact | None = None
            usage_status: Literal["child_terminal_handoff", "child_not_started_zero"]
            charged_milliunits: int
            synthetic_test_terminal = (
                child_terminal_reference is not None
                and child_terminal_reference.terminal_event_id.startswith(
                    "run_end:synthetic:"
                )
            )
            if child_terminal_reference is None or synthetic_test_terminal:
                if terminal.child_runtime_session_id is not None:
                    child_log = self.subagent_runtime.event_log_locator.event_log_for_runtime_session(
                        terminal.child_runtime_session_id
                    )
                    start_snapshot = child_log.read_raw_events_by_types(
                        (EventType.RUN_START.value,),
                        max_events=1,
                        max_payload_bytes=512 * 1024,
                    )
                    if start_snapshot.events:
                        raise RuntimeError(
                            "started child cannot terminalize without native terminal handoff"
                        )
                usage_status = "child_not_started_zero"
                charged_milliunits = 0
            else:
                handoff = self._build_child_rollout_usage_handoff(
                    child_terminal_reference=child_terminal_reference,
                )
                if (
                    handoff.settlement_aggregate.charged_milliunits
                    > reservation.reserved_milliunits
                ):
                    raise RuntimeError("child handoff exceeds parent reservation")
                usage_status = "child_terminal_handoff"
                charged_milliunits = handoff.settlement_aggregate.charged_milliunits

            augmented.append(
                RolloutBudgetReservationSettledEvent(
                    id=(
                        "rollout_budget_reservation_settled:"
                        f"{reservation.reservation_id}"
                    ),
                    created_at=terminal.created_at,
                    run_id=terminal.run_id,
                    turn_id=terminal.turn_id,
                    reply_id=terminal.reply_id,
                    reservation_id=reservation.reservation_id,
                    charged_milliunits=charged_milliunits,
                    usage_status=usage_status,
                    usage_charge=None,
                    source_model_call_end_event_id=None,
                    source_tool_result_event_id=None,
                    child_usage_handoff=handoff,
                )
            )
            existing_settlement_ids.add(reservation.reservation_id)
        return tuple(augmented)

    def _build_child_rollout_usage_handoff(
        self,
        *,
        child_terminal_reference: ChildNativeTerminalReferenceFact,
    ) -> ChildRolloutUsageHandoffFact:
        if self.subagent_runtime is None:
            raise RuntimeError("child rollout handoff requires SubagentRuntime")
        child_log = (
            self.subagent_runtime.event_log_locator.event_log_for_runtime_session(
                child_terminal_reference.child_runtime_session_id
            )
        )
        child_snapshot = child_log.read_raw_events_by_types(
            (
                EventType.RUN_START.value,
                EventType.RUN_END.value,
                EventType.CHILD_ROLLOUT_SUBACCOUNT_CLOSED.value,
            ),
            run_ids=(child_terminal_reference.child_run_id,),
            max_events=3,
            max_payload_bytes=2 * 1024 * 1024,
        )
        child_events = tuple(
            decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
            for raw in child_snapshot.events
        )
        terminals = tuple(
            event
            for event in child_events
            if isinstance(event, RunEndEvent)
            and event.id == child_terminal_reference.terminal_event_id
        )
        closes = tuple(
            event
            for event in child_events
            if isinstance(event, ChildRolloutSubaccountClosedEvent)
        )
        starts = tuple(
            event for event in child_events if isinstance(event, RunStartEvent)
        )
        if len(terminals) != 1 or len(closes) != 1 or len(starts) != 1:
            raise RuntimeError(
                "child rollout handoff requires one Start, close, and native terminal"
            )
        terminal = terminals[0]
        close = closes[0]
        child_start = starts[0]
        subaccount = child_start.child_rollout_subaccount
        if (
            terminal.sequence != child_terminal_reference.terminal_sequence
            or terminal.status != child_terminal_reference.terminal_status
            or terminal.terminalization_kind
            != child_terminal_reference.terminalization_kind
            or terminal.stop_reason != child_terminal_reference.stop_reason
            or close.run_end_event_id != terminal.id
            or subaccount is None
            or close.subaccount_fingerprint != subaccount.subaccount_fingerprint
        ):
            raise RuntimeError("child rollout handoff identity mismatch")
        return build_child_rollout_usage_handoff(
            settlement_aggregate=close.settlement_aggregate,
            child_terminal_reference=child_terminal_reference,
        )

    def new_state(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
    ) -> RunActivationWorkingState:
        payload: dict[str, object] = {
            "session_id": session_id or self._run_identity.runtime_session_id,
            "budget": self.budget,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if reply_id is not None:
            payload["reply_id"] = reply_id
        return RunActivationWorkingState(**payload)  # type: ignore[arg-type]

    def resolve_run_model_target(self) -> ResolvedModelTarget:
        return self.llm_runtime.resolve_target(
            role=self.model_role,
            requested_options=self.options,
        )

    def rebind_run_model_target(
        self, fact: ResolvedModelTargetFact
    ) -> ResolvedModelTarget:
        """Rebind an existing run only from its durable RunStart contract."""

        return self.llm_runtime.rebind_target(fact)

    @staticmethod
    def _require_run_model_target(
        state: RunActivationWorkingState,
    ) -> ResolvedModelTarget:
        if state.run_model_target is None:
            raise RuntimeError("active run is missing its ResolvedModelTarget")
        return state.run_model_target

    def _capture_run_permission_snapshot(
        self, state: RunActivationWorkingState
    ) -> RunPermissionSnapshot:
        if state.permission_snapshot is not None:
            return state.permission_snapshot
        if self._is_subagent_child:
            mode = self._permission_state.mode
            if mode is None:
                raise ValueError(
                    "child AgentRuntime requires a preset child_profile permission mode"
                )
            source = "child_profile"
        elif self._plan_state(state).active:
            mode = PermissionMode.READ_ONLY
            source = "plan_mode"
        else:
            mode = self._permission_state.mode
            if mode is None:
                raise ValueError(
                    "AgentRuntime session default requires a preset permission mode"
                )
            source = "session_default"
        snapshot = snapshot_from_mode(
            runtime_session_id=self._run_identity.runtime_session_id,
            run_id=state.run_id,
            permission_mode=mode,
            permission_snapshot_source=source,
        )
        state.permission_snapshot = snapshot
        return snapshot

    def _require_run_permission_snapshot(
        self, state: RunActivationWorkingState
    ) -> RunPermissionSnapshot:
        if state.permission_snapshot is None:
            raise RuntimeError(
                "missing RunPermissionSnapshot for active run; RunStartEvent permission fields are required"
            )
        return state.permission_snapshot

    def _run_permission_policy(
        self, state: RunActivationWorkingState
    ) -> EffectivePermissionPolicy:
        return preset_to_policy(
            self._require_run_permission_snapshot(state).permission_mode
        )

    def _run_permission_mode(self, state: RunActivationWorkingState) -> PermissionMode:
        return self._require_run_permission_snapshot(state).permission_mode

    def _permission_gate_for_state(
        self, state: RunActivationWorkingState
    ) -> PolicyPermissionGate:
        return PolicyPermissionGate(
            self._require_run_permission_snapshot(state).to_permission_state(),
            inner=self.permission_gate.inner,
        )

    def _tool_runtime_context(
        self,
        state: RunActivationWorkingState,
        *,
        context_id: str | None = None,
        model_call_index: int | None = None,
    ) -> ToolRuntimeContext:
        snapshot = self._require_run_permission_snapshot(state)
        return ToolRuntimeContext(
            runtime_session_id=self._run_identity.runtime_session_id,
            event_context=self._event_context(state),
            permission=tool_permission_invocation_from_snapshot(
                snapshot.to_context_fact()
            ),
            owner_kind=(
                ToolInvocationOwnerKind.SUBAGENT_CHILD
                if self._is_subagent_child
                else ToolInvocationOwnerKind.HOST_MAIN_RUN
            ),
            context_id=context_id,
            model_call_index=model_call_index,
        )

    async def run_committed_entry(
        self,
        draft: AgentRunDraft,
        committed: CommittedRunEntry,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AgentRunResult:
        async for _event in self.stream_committed_entry(
            draft, committed, active_skill_names=active_skill_names
        ):
            pass
        return self._run_result(draft.state)

    async def stream_committed_entry(
        self,
        draft: AgentRunDraft,
        committed: CommittedRunEntry,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = draft.state
        try:
            async for event in self._stream_committed_entry_inner(
                draft,
                committed,
                active_skill_names=active_skill_names,
            ):
                yield event
        except BaseException as exc:
            if not state.finalized:
                if (
                    isinstance(exc, asyncio.CancelledError)
                    and state.stop_request is not None
                ):
                    raise
                if not isinstance(
                    self._require_run_finalization_owner(state).run_end_candidate,
                    RunEndEvent,
                ):
                    state.status = LoopStatus.FAILED
                    state.stop_reason = RunStopReason.RUNTIME_EXECUTION_ERROR
                    state.error_message = (
                        "committed run execution failed: " + type(exc).__name__
                    )
                    if state.last_transition not in {
                        LoopTransition.FAIL,
                        LoopTransition.FINISH,
                    }:
                        state.transition(LoopTransition.FAIL)
                async for terminal in self._finalize_run(
                    state,
                    run_session_end_hook=False,
                ):
                    yield terminal
            raise

    async def _stream_committed_entry_inner(
        self,
        draft: AgentRunDraft,
        committed: CommittedRunEntry,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = draft.state
        if (
            committed.run_start_event.id != draft.run_start_event.id
            or committed.run_start_event.sequence != committed.run_start_sequence
        ):
            raise RuntimeError("committed run entry does not match AgentRunDraft")
        user_input = draft.current_user_message.text
        async for event in self._emit_pending_plan_entry_audit(state):
            yield event
        ok, _result, error_event = await self._run_memory_hook(
            state,
            "on_turn_start",
            lambda: self._call_turn_start_hook(state, user_input),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            async for event in self._finalize_run(state, run_session_end_hook=False):
                yield event
            return
        frozen_surface = draft.frozen_execution_surface
        resolved_exposure = self.capability_runtime.resolve_exposure_projection(
            CapabilityProjectionResolveContext(
                workspace_root=self._run_identity.workspace_root,
                workspace_kind=self.workspace_kind,
                memory_domain=self.memory_domain,
                user_input=user_input,
                prior_messages=draft.prior_messages,
                active_skill_names=(
                    active_skill_names
                    if active_skill_names is not None
                    else frozenset(draft.capability_basis.active_skill_names)
                ),
                plan_active=self._plan_state(state).active,
            ),
            frozen_surface=frozen_surface,
            archive=self._run_long_horizon.archive,
            runtime_session_id=self._run_identity.runtime_session_id,
            owner=draft.capability_basis.owner,
            resolve_basis=draft.capability_basis,
            exposure_id=f"capability_exposure:{uuid4().hex}",
            resolution_kind="initial",
        )
        exposure = resolved_exposure.plan
        exposure_event = CapabilityExposureResolvedEvent(
            **self._event_context(state).event_fields(),
            exposure=resolved_exposure.fact,
            exposure_revision=1,
        )
        try:
            stored_exposure = await self._run_ledger.emit(
                exposure_event,
            )
        except BaseException as exc:
            if isinstance(exc, EventPublicationAfterCommitError):
                confirmed = tuple(exc.result.committed_events)
            else:
                outcome = self._run_ledger.resolved_write_outcome(exc)
                if outcome.status != "full":
                    raise
                confirmed = tuple(outcome.committed_events)
            if len(confirmed) != 1 or not isinstance(
                confirmed[0], CapabilityExposureResolvedEvent
            ):
                raise RuntimeError(
                    "initial capability exposure confirmation was not exact"
                ) from exc
            self._require_run_working_set(state).install_initial_exposure(
                plan=exposure,
                fact=resolved_exposure.fact,
                event_ref=event_reference_from_stored(
                    confirmed[0],
                    runtime_session_id=self._run_identity.runtime_session_id,
                ),
            )
            if self.run_execution_registry is not None:
                self.run_execution_registry.install_initial_authority_full(
                    run_id=state.run_id,
                    stored_exposure=confirmed[0],
                )
            raise
        if not isinstance(stored_exposure, CapabilityExposureResolvedEvent):
            raise RuntimeError("capability exposure commit returned wrong event type")
        self._require_run_working_set(state).install_initial_exposure(
            plan=exposure,
            fact=resolved_exposure.fact,
            event_ref=event_reference_from_stored(
                stored_exposure,
                runtime_session_id=self._run_identity.runtime_session_id,
            ),
        )
        if self.run_execution_registry is not None:
            self.run_execution_registry.install_initial_authority_full(
                run_id=state.run_id,
                stored_exposure=stored_exposure,
            )
        if self._subagent_parent_features_enabled and self.subagent_runtime is not None:
            permission_snapshot = self._require_run_permission_snapshot(state)
            self.subagent_runtime.refresh_parent_capability_snapshot(
                exposure=exposure,
                permission_mode=permission_snapshot.permission_mode.value,
                permission_policy=dict(permission_snapshot.permission_policy),
            )
        yield stored_exposure

        async for event in self._stream_model_loop(state, exposure):
            yield event

    def _require_capability_exposure(
        self, state: RunActivationWorkingState
    ) -> CapabilityExposurePlan:
        working_set = self._require_run_working_set(state)
        exposure = working_set.effective_exposure_plan
        if not isinstance(exposure, CapabilityExposurePlan):
            raise RuntimeError(
                "model/tool continuation requires a committed capability exposure"
            )
        return exposure

    def _commit_prepared_context_caches(
        self,
        *,
        prepared_context_input,
        render_output,
    ) -> None:
        """Commit optimization hints only after durable ContextCompiled."""

        for cache_write in render_output.cache_write_candidates:
            try:
                self._run_context.tool_result_render_cache.put(
                    cache_write.cache_key,
                    cache_write.hint,
                )
            except Exception as exc:
                self._run_context.record_cache_diagnostic(
                    cache_kind="tool_result_render",
                    operation="write",
                    error=exc,
                )
        for cache_write in prepared_context_input.candidate_cache_writes:
            try:
                self._run_context.context_candidate_lifecycle_cache.put(
                    cache_write.key,
                    cache_write.candidate,
                )
            except Exception as exc:
                self._run_context.record_cache_diagnostic(
                    cache_kind="candidate_lifecycle",
                    operation="write",
                    error=exc,
                )

    async def _ingest_new_tool_result_projections(
        self,
        *,
        state: RunActivationWorkingState,
        resolved_call: ResolvedModelCall,
    ) -> tuple[AgentEvent, ...]:
        """Commit every newly terminal observation before context compilation."""

        projection_input = await self._run_context.prepare_live_transcript_projection(
            working_set=self._require_run_working_set(state),
            budget=self.budget,
        )
        rendered = render_prepared_tool_result_units(
            prepared=projection_input.prepared_tool_results,
            transcript=projection_input.normalized_transcript.transcript,
            token_estimator=resolved_call.target.token_estimator,
        )
        store = self._run_long_horizon.store
        window_state = store.window_state(state.run_id)
        if window_state is None or window_state.active_window_id is None:
            raise RuntimeError("projection ingest requires one active context window")
        window = window_state.windows[window_state.active_window_id]
        if (
            window.resolved_model_target_fingerprint
            != resolved_call.target.fact.target_fingerprint
            or window.token_estimator_fingerprint
            != resolved_call.target.fact.token_estimator.estimator_fingerprint
        ):
            raise RuntimeError("projection ingest model target differs from window")
        current = store.projection_state(window.window_id)
        if current is None:
            raise RuntimeError("projection ingest lost the active window baseline")
        working_set = self._require_run_working_set(state)
        planning_input = prepare_current_run_projection_planning_input(
            run_id=state.run_id,
            run_start_sequence=working_set.run_start_sequence,
            window=window,
            current_projection=current,
            canonical_slice=projection_input.authority_slice,
            transcript=projection_input.normalized_transcript.transcript,
            tool_result_units=(
                projection_input.normalized_transcript.tool_result_units
            ),
            context_budget=resolved_call.target.context_budget,
            allocation_policy=working_set.long_horizon_contract.window_policy,
            estimator=resolved_call.target.fact.token_estimator,
            pending_interaction=state.pending_interaction_kind is not None,
            tool_call_in_flight=_tool_call_in_flight(state),
        )
        plan = plan_new_result_ingest(
            event_context=self._event_context(state),
            window=window,
            current_state=current,
            units=projection_input.normalized_transcript.tool_result_units,
            rendered=rendered,
            token_estimator=resolved_call.target.token_estimator,
            policy=planning_input.allocation_policy,
            protection_facts=planning_input.protection_facts,
            source_through_sequence=(projection_input.authority_slice.through_sequence),
        )
        if plan is None:
            return ()
        stored = tuple(await self._run_ledger.emit_many(plan.events))
        if tuple(event.id for event in stored) != tuple(
            event.id for event in plan.events
        ):
            raise RuntimeError("projection ingest committed an unexpected event batch")
        committed_state = store.projection_state(window.window_id)
        if committed_state != plan.final_state:
            raise RuntimeError("projection ingest reducer differs from planned state")
        return stored

    async def _prepare_active_observation_rollups(
        self,
        *,
        state: RunActivationWorkingState,
        resolved_call: ResolvedModelCall,
        normalized_transcript,
        projection_state,
    ):
        rollups = projection_state.rollups
        if not rollups:
            return ()
        carrier = resolved_call.target.fact.runtime_observation_carrier
        if carrier is None:
            raise RuntimeError(
                "active observation rollups require a resolved runtime carrier"
            )
        units = {unit.unit_id: unit for unit in normalized_transcript.tool_result_units}
        policy = self._require_run_working_set(
            state
        ).long_horizon_contract.window_policy
        prepared_units = []
        for durable in rollups:
            try:
                member_units = tuple(
                    units[member.unit_id] for member in durable.member_facts
                )
            except KeyError as exc:
                raise RuntimeError(
                    "active rollup references a result outside the transcript"
                ) from exc
            placement_anchor = derive_rollup_placement_anchor(
                transcript=normalized_transcript.transcript,
                member_units=member_units,
            )
            cache_key = prepared_observation_rollup_cache_key(
                durable_rollup_fingerprint=durable.semantic_fingerprint,
                member_unit_fingerprints=tuple(
                    unit.unit_fingerprint for unit in member_units
                ),
                placement_basis_fingerprint=placement_anchor.anchor_fingerprint,
                policy_fingerprint=policy.policy_fingerprint,
                estimator_fingerprint=(
                    resolved_call.target.fact.token_estimator.estimator_fingerprint
                ),
                carrier_contract_fingerprint=carrier.contract_fingerprint,
            )
            cached = self._run_context.prepared_observation_rollup_cache.get(cache_key)
            if cached is not None:
                if cached.rollup != durable:
                    raise RuntimeError(
                        "prepared rollup cache differs from durable authority"
                    )
                prepared_units.append(cached)
                continue
            prepared = prepare_observation_rollup_artifact(
                window_id=projection_state.window_id,
                member_units=member_units,
                transcript=normalized_transcript.transcript,
                policy=policy,
                token_estimator=resolved_call.target.token_estimator,
                registry=self.observation_rollup_renderer_registry,
                placement_anchor=placement_anchor,
            )
            if prepared.fact != durable:
                raise RuntimeError(
                    "active rollup differs from deterministic source materialization"
                )
            prepared_unit = await self._run_context.materialize_observation_rollup(
                run_id=state.run_id,
                prepared=prepared,
                carrier=carrier,
                artifact_mode="read_confirm",
            )
            self._run_context.prepared_observation_rollup_cache.put(
                cache_key, prepared_unit
            )
            prepared_units.append(prepared_unit)
        return tuple(prepared_units)

    def _descriptor_render_attribution(
        self,
        state: RunActivationWorkingState,
        descriptor,
    ) -> CapabilityDescriptorRenderAttributionFact:
        working_set = self._require_run_working_set(state)
        exposure = working_set.effective_exposure_fact
        event_ref = working_set.effective_exposure_event_ref
        if exposure is None or event_ref is None:
            raise RuntimeError(
                "tool execution requires committed descriptor render attribution"
            )
        return descriptor_render_attribution(
            descriptor=descriptor,
            exposure_event_ref=event_ref,
            exposure_fact=exposure,
        )

    def _typed_tool_result_error_events(
        self,
        state: RunActivationWorkingState,
        *,
        tool_call_id: str,
        tool_call_name: str,
        message: str,
        result_state: ToolResultState = ToolResultState.ERROR,
        arguments: dict[str, Any] | None = None,
        failure_stage: Literal[
            "malformed_arguments",
            "exposure_denied",
            "permission_denied",
            "policy_denied",
            "adapter_initialization",
        ] = "permission_denied",
        reason_code: str | None = None,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        mcp_input_required_terminal_source: (
            McpInputRequiredTerminalSourceFact | None
        ) = None,
    ) -> list[AgentEvent | ToolResultEndCandidate]:
        exposure = self._require_capability_exposure(state)
        descriptor = exposure.descriptors_by_name.get(tool_call_name)
        low_state = ToolResultStateFact(result_state.value)
        if descriptor is None:
            semantics = build_unknown_result_semantics(result_state=low_state)
        else:
            frozen_arguments = freeze_json(arguments or {})
            if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                raise AssertionError("tool arguments must freeze as an object")
            attribution = self._descriptor_render_attribution(state, descriptor)
            tool_observation_timing_seed = {
                **(tool_observation_timing_seed or {}),
                "tool_origin": tool_origin_for_descriptor_variant(
                    descriptor,
                    descriptor.result_render_contract.pre_execution_denial_variant_code,
                ),
            }
            semantics = None

            def semantics_factory(timing):
                return build_pre_execution_denial_semantics(
                    descriptor=descriptor,
                    descriptor_attribution=attribution,
                    requested_arguments=frozen_arguments,
                    message=message,
                    result_state=low_state,
                    reason_code=reason_code or failure_stage,
                    failure_stage=failure_stage,
                    capture_policy=self.tool_executor.essential_capture_policy,
                    registry=self.tool_executor.semantics_registry,
                    observation_timing=timing,
                )

        if descriptor is None:
            semantics_factory = None
        return build_tool_result_error_events(
            self._event_context(state),
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
            message=message,
            state=result_state,
            tool_observation_timing_seed=tool_observation_timing_seed,
            semantics=semantics,
            semantics_factory=semantics_factory,
            mcp_input_required_terminal_source=mcp_input_required_terminal_source,
        )

    @staticmethod
    def _require_run_working_set(state: RunActivationWorkingState) -> RunWorkingSet:
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("committed run requires a typed RunWorkingSet")
        return working_set

    def _capability_gate_decision_fact(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        result_state: ToolResultState | None = None,
        reason_code_override: str | None = None,
    ) -> CapabilityGateDecisionFact:
        descriptor = exposure.descriptors_by_name.get(call.name)
        action_classification = None
        if descriptor is not None:
            action_classification = self.tool_action_classifier_registry.classify(
                call=call,
                descriptor_id=descriptor.id,
                descriptor_fingerprint=descriptor.fingerprint(),
                policy=descriptor.long_horizon_policy,
            )
        classification = None
        if descriptor is not None:
            try:
                classification = DefaultCapabilityCallClassifier().classify(
                    call, descriptor
                )
            except Exception:
                classification = None
        reason_code, reason_message = _normalize_capability_gate_reason(
            decision,
            reason_code_override=reason_code_override,
        )
        capability_context = _terminal_capability_context(call, exposure)
        subagent_context = self._run_identity.default_event_metadata.get("subagent")
        if isinstance(subagent_context, dict):
            capability_context = dict(capability_context or {})
            capability_context["subagent"] = dict(subagent_context)
        return CapabilityGateDecisionFact(
            tool_call_id=call.id,
            tool_name=call.name,
            descriptor_id=descriptor.id if descriptor is not None else None,
            decision=decision.kind,
            reason_code=reason_code,
            reason_message=reason_message,
            suggested_rules=tuple(dict(rule) for rule in decision.suggested_rules),
            result_state=result_state,
            policy_mode=self._run_permission_mode(state).value,
            permission_policy=dict(
                self._require_run_permission_snapshot(state).permission_policy
            ),
            exposure_generation=exposure.registry_generation,
            availability=descriptor.availability if descriptor is not None else None,
            permission_category=descriptor.permission_category
            if descriptor is not None
            else None,
            effective_permission_category=(
                classification.effective_permission_category
                if classification is not None
                else None
            ),
            effective_read_only=classification.effective_read_only
            if classification is not None
            else None,
            capability_context=capability_context or {},
            action_classification=action_classification,
        )

    async def _emit_capability_gate_decision(
        self,
        state: RunActivationWorkingState,
        fact: CapabilityGateDecisionFact,
    ) -> AsyncIterator[AgentEvent]:
        yield await self._run_ledger.emit(
            self._capability_gate_decision_event(state, fact),
        )

    def _capability_gate_decision_event(
        self,
        state: RunActivationWorkingState,
        fact: CapabilityGateDecisionFact,
    ) -> CapabilityGateDecisionEvent:
        return CapabilityGateDecisionEvent(
            **self._event_context(state).event_fields(),
            tool_call_id=fact.tool_call_id,
            tool_name=fact.tool_name,
            descriptor_id=fact.descriptor_id,
            decision=fact.decision.value,
            reason_code=fact.reason_code,
            reason_message=fact.reason_message,
            suggested_rules=list(fact.suggested_rules),
            result_state=fact.result_state,
            policy_mode=fact.policy_mode,
            permission_policy=fact.permission_policy,
            exposure_generation=fact.exposure_generation,
            availability=(
                fact.availability.value if fact.availability is not None else None
            ),
            permission_category=fact.permission_category,
            effective_permission_category=fact.effective_permission_category,
            effective_read_only=fact.effective_read_only,
            capability_context=fact.capability_context,
            action_classification=fact.action_classification,
        )

    async def _emit_capability_access_denial(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        rollout_reservation: RolloutReservationFact | None = None,
        mcp_input_required_terminal_source: (
            McpInputRequiredTerminalSourceFact | None
        ) = None,
        deadline_budget: RuntimeEventOperationDeadlineBudget | None = None,
    ) -> AsyncIterator[AgentEvent]:
        result_state = (
            ToolResultState.ERROR
            if decision.reason and "capability_descriptor_missing" in decision.reason
            else ToolResultState.DENIED
        )
        stored_events = await self._commit_tool_denial(
            state,
            call,
            exposure=exposure,
            decision=decision,
            message=decision.reason or "tool call denied by capability exposure",
            result_state=result_state,
            failure_stage="exposure_denied",
            tool_observation_timing_seed=tool_observation_timing_seed,
            rollout_reservation=rollout_reservation,
            mcp_input_required_terminal_source=mcp_input_required_terminal_source,
            deadline_budget=deadline_budget,
        )
        for event in stored_events:
            yield event
        self._record_tool_result_events(
            state,
            stored_events=list(stored_events),
            tool_call_id=call.id,
            tool_call_name=call.name,
        )

    async def _emit_permission_gate_denial(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        rollout_reservation: RolloutReservationFact | None = None,
        mcp_input_required_terminal_source: (
            McpInputRequiredTerminalSourceFact | None
        ) = None,
        deadline_budget: RuntimeEventOperationDeadlineBudget | None = None,
    ) -> AsyncIterator[AgentEvent]:
        result_state = (
            ToolResultState.ERROR
            if decision.reason and "capability_descriptor_missing" in decision.reason
            else ToolResultState.DENIED
        )
        stored_events = await self._commit_tool_denial(
            state,
            call,
            exposure=exposure,
            decision=decision,
            message=decision.reason or "tool call denied by permission gate",
            result_state=result_state,
            failure_stage="permission_denied",
            tool_observation_timing_seed=tool_observation_timing_seed,
            rollout_reservation=rollout_reservation,
            mcp_input_required_terminal_source=mcp_input_required_terminal_source,
            deadline_budget=deadline_budget,
        )
        for event in stored_events:
            yield event
        self._record_tool_result_events(
            state,
            stored_events=list(stored_events),
            tool_call_id=call.id,
            tool_call_name=call.name,
        )

    async def _commit_tool_denial(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        message: str,
        result_state: ToolResultState,
        failure_stage: Literal[
            "malformed_arguments",
            "exposure_denied",
            "permission_denied",
            "policy_denied",
            "adapter_initialization",
        ],
        tool_observation_timing_seed: dict[str, Any] | None,
        rollout_reservation: RolloutReservationFact | None = None,
        mcp_input_required_terminal_source: (
            McpInputRequiredTerminalSourceFact | None
        ) = None,
        deadline_budget: RuntimeEventOperationDeadlineBudget | None = None,
    ) -> tuple[AgentEvent, ...]:
        fact = self._capability_gate_decision_fact(
            state,
            call,
            exposure=exposure,
            decision=decision,
            result_state=result_state,
        )
        gate_event = self._capability_gate_decision_event(state, fact)
        terminal_candidates = tuple(
            self._typed_tool_result_error_events(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                message=message,
                result_state=result_state,
                arguments=call.arguments,
                failure_stage=failure_stage,
                reason_code=fact.reason_code,
                tool_observation_timing_seed=tool_observation_timing_seed,
                mcp_input_required_terminal_source=(mcp_input_required_terminal_source),
            )
        )
        run_start = _run_start_for_id(
            self._run_long_horizon.store,
            run_id=state.run_id,
        )
        account_id = run_start.long_horizon.rollout_account_id
        rollout_state = self._run_long_horizon.store.rollout_state(account_id)
        terminal_event = next(
            event
            for event in terminal_candidates
            if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
        )
        settlement = (
            self._tool_rollout_settlement_event(
                state,
                terminal_event=terminal_event,
                reservation=rollout_reservation,
            )
            if rollout_reservation is not None
            else None
        )
        write_candidates: tuple[AgentEvent, ...] = (gate_event, *terminal_candidates)
        if settlement is not None:
            write_candidates = (*write_candidates, settlement)
        track_mcp_terminal = (
            state.run_id,
            call.id,
        ) in self._mcp_terminal_commit_outcomes
        if track_mcp_terminal:
            self._mark_mcp_terminal_commit_attempt(state, call.id)
        mcp_pending_handle = self._mcp_terminal_pending_handles.get(
            (state.run_id, call.id)
        )
        if track_mcp_terminal and mcp_pending_handle is None:
            raise RuntimeError("MCP denial lost its pending handle owner")
        terminal_registry = self._run_tools.tool_execution_terminal_registry
        if (
            rollout_reservation is not None
            or mcp_input_required_terminal_source is not None
        ):
            write_candidates = (
                await self._run_tools.tool_terminal_projection_service.prepare_batch(
                    write_candidates,
                    deadline_monotonic=(
                        deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else None
                    ),
                )
            )
            write_candidates = self._attach_mcp_terminal_disposition(
                state,
                prepared_candidates=write_candidates,
                source=mcp_input_required_terminal_source,
                disposition_kind=None,
            )
        candidate_owner = None
        prepared_mcp_settlement = None
        if rollout_reservation is not None:
            candidate_owner = terminal_registry.freeze_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
                candidates=write_candidates,
                physical_owner_identity_fingerprint=(
                    mcp_pending_handle.identity.identity_fingerprint
                    if mcp_pending_handle is not None
                    else None
                ),
            )
            if mcp_pending_handle is not None:
                mcp_port = self._run_tools.mcp_tool_execution_port
                if mcp_port is None:
                    raise RuntimeError("MCP denial lost its execution port")
                prepared_mcp_settlement = mcp_port.prepare_terminal_settlement(
                    pending_handle=mcp_pending_handle,
                    reason=McpPendingTerminalReason.PERMISSION_DENIED,
                    candidate_owner_identity=candidate_owner,
                    terminal_event_id=terminal_event.id,
                )
                terminal_registry.bind_transaction_companion(
                    owner_identity=candidate_owner,
                    transaction_companion=(
                        prepared_mcp_settlement.transaction_companion
                    ),
                )
        try:
            if rollout_reservation is not None:
                result = await self._run_tools.event_commit_port().commit_terminal_batch_and_settlement(
                    terminal_candidates=tuple(
                        event for event in write_candidates if event.id != settlement.id
                    ),
                    settlement_candidate=settlement,
                    expected_reservation_fingerprint=(
                        rollout_reservation.semantic_fingerprint
                    ),
                    deadline_monotonic=(
                        deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else None
                    ),
                    transaction_companion=(
                        prepared_mcp_settlement.transaction_companion
                        if prepared_mcp_settlement is not None
                        else None
                    ),
                )
            elif rollout_state is None:
                result = await self._run_ledger.write_events_with_deadline(
                    write_candidates,
                    deadline_monotonic=(
                        deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else self._run_ledger.new_write_deadline_monotonic()
                    ),
                    expected_last_sequence=(
                        self._run_long_horizon.store.through_sequence
                    ),
                )
            else:
                result = await self._run_tools.event_commit_port().commit_gate_and_denial(
                    gate_candidate=gate_event,
                    denied_terminal_candidates=tuple(
                        event for event in write_candidates if event.id != gate_event.id
                    ),
                    expected_account_state_fingerprint=rollout_state.state_fingerprint,
                    account_id=account_id,
                )
        except BaseException as exc:
            outcome = self._run_ledger.resolved_write_outcome(exc)
            if candidate_owner is not None and prepared_mcp_settlement is not None:
                receipt = terminal_registry.confirm_stable_candidate_write(
                    owner_identity=candidate_owner,
                    outcome=outcome,
                )
                transition = (
                    self._run_tools.mcp_tool_execution_port.confirm_terminal_commit(
                        settlement=prepared_mcp_settlement,
                        commit_receipt=receipt,
                    )
                )
                terminal_registry.accept_physical_owner_handoff(
                    transition.handoff_receipt
                )
            elif rollout_reservation is not None and outcome.status == "unknown":
                terminal_registry.mark_commit_outcome_unknown(
                    run_id=state.run_id,
                    reservation=rollout_reservation,
                )
            if track_mcp_terminal:
                self._resolve_mcp_terminal_commit_failure(
                    state,
                    tool_call_id=call.id,
                    candidates=write_candidates,
                    error=exc,
                )
            raise
        if candidate_owner is not None and prepared_mcp_settlement is not None:
            receipt = terminal_registry.confirm_stable_candidate_write(
                owner_identity=candidate_owner,
                outcome=EventBatchCommitOutcome(
                    status="full",
                    deadline_monotonic=(
                        deadline_budget.terminal_deadline_monotonic
                        if deadline_budget is not None
                        else time.monotonic()
                    ),
                    result=result,
                ),
            )
            transition = (
                self._run_tools.mcp_tool_execution_port.confirm_terminal_commit(
                    settlement=prepared_mcp_settlement,
                    commit_receipt=receipt,
                )
            )
            terminal_registry.accept_physical_owner_handoff(transition.handoff_receipt)
        if track_mcp_terminal:
            self._mark_mcp_terminal_commit_full(state, call.id)
        if result.reconciliation_required:
            if rollout_reservation is not None and prepared_mcp_settlement is None:
                terminal_registry.mark_commit_outcome_unknown(
                    run_id=state.run_id,
                    reservation=rollout_reservation,
                )
            raise RuntimeError("tool denial committed without a healthy reducer fold")
        if rollout_reservation is not None and prepared_mcp_settlement is None:
            terminal_registry.complete_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
            )
        return result.committed_events

    async def _stream_capability_access_filtered_calls(
        self,
        state: RunActivationWorkingState,
        parsed_calls: list[ToolCall],
        *,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent | tuple[list[ToolCall]]]:
        executable_calls: list[ToolCall] = []
        for call in parsed_calls:
            local_decision = evaluate_capability_exposure_access(call, exposure)
            if local_decision is None:
                executable_calls.append(call)
                continue
            async for event in self._emit_capability_access_denial(
                state,
                call,
                exposure=exposure,
                decision=local_decision,
            ):
                yield event
        yield (executable_calls,)

    async def _emit_pending_plan_entry_audit(
        self, state: RunActivationWorkingState
    ) -> AsyncIterator[AgentEvent]:
        payload = state.plan_progress.entry_audit
        if payload is None:
            return
        if state.plan_progress.entry_audit_emitted:
            return
        event = await self._run_ledger.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="user",
                previous_permission_mode=payload.previous_permission_mode,
                previous_permission_policy=dict(payload.previous_permission_policy),
                reason=payload.reason,
            ),
        )
        plan_state = self._plan_state(state)
        plan_state.apply_durable_event(event)
        if state.run_working_set is not None:
            state.run_working_set.plan_snapshot = plan_workflow_state_fact(plan_state)
        state.plan_progress.entry_audit_emitted = True
        yield event

    async def _prepare_rollout_phase_for_model_call(
        self,
        *,
        state: RunActivationWorkingState,
        resolved_call: ResolvedModelCall,
    ) -> tuple[AgentEvent | None, str | None]:
        for _attempt in range(3):
            binding = self._run_long_horizon.resolve_rollout_binding(
                run_id=state.run_id,
            )
            if binding.child_state is not None:
                if binding.parent_state.phase in {
                    RolloutPhase.FINALIZATION_ONLY,
                    RolloutPhase.EXHAUSTED,
                    RolloutPhase.EMERGENCY_HARD_STOP,
                }:
                    return None, "child_rollout_parent_finalization"
                quote = calculate_model_call_reservation(
                    target=resolved_call.target.fact,
                    resolved_model_call_id=(resolved_call.fact.resolved_model_call_id),
                    policy=binding.account.policy,
                )
                if quote.reserved_milliunits > binding.child_state.remaining_milliunits:
                    return None, "child_rollout_subaccount_exhausted"
                return None, None

            quote = calculate_model_call_reservation(
                target=resolved_call.target.fact,
                resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
                policy=binding.account.policy,
            )
            plan = plan_root_model_admission(
                account=binding.account,
                state=binding.parent_state,
                quote=quote,
                purpose=resolved_call.fact.purpose,
            )
            if plan.action == "admit":
                return None, None
            if plan.action == "blocked":
                if await self._await_reclaimable_rollout_reservations(
                    state=state,
                    budget_bucket=plan.budget_bucket,
                ):
                    continue
                return None, "rollout_admission_reconciliation_blocked"
            if plan.action == "terminal":
                reason = (
                    "rollout_emergency_hard_stop"
                    if binding.parent_state.phase is RolloutPhase.EMERGENCY_HARD_STOP
                    else "rollout_budget_exhausted"
                )
                return None, reason
            candidate = build_rollout_phase_transition_event(
                event_context=self._event_context(state),
                account=binding.account,
                state=binding.parent_state,
                plan=plan,
            )
            try:
                stored = await self._run_ledger.emit(candidate)
            except LongHorizonReducerApplyError:
                # Terminal monitor/completion writers can advance the canonical
                # ledger between rollout planning and this queued commit.  A
                # stale candidate was not appended, so rebuild it from the new
                # reducer head; a same-head failure is a real contract fault.
                if (
                    self._run_long_horizon.store.through_sequence
                    > candidate.source_through_sequence
                ):
                    continue
                raise
            return stored, None
        return None, "rollout_admission_reconciliation_blocked"

    async def _await_reclaimable_rollout_reservations(
        self,
        *,
        state: RunActivationWorkingState,
        budget_bucket: RolloutBudgetBucket | None,
    ) -> bool:
        if budget_bucket is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        cancellation_takeover_at = loop.time() + 0.25
        cancelled_children: set[str] = set()
        while loop.time() < deadline:
            binding = self._run_long_horizon.resolve_rollout_binding(
                run_id=state.run_id,
            )
            blockers = tuple(
                item
                for item in binding.parent_state.active_reservations
                if item.budget_bucket is budget_bucket
            )
            if not blockers:
                return (
                    binding.parent_state.model_stream_reconciliation_blocker_count == 0
                )
            if loop.time() < cancellation_takeover_at:
                # A terminal owner or batch-repair path may already be folding
                # the exact settlement. Give that owner a bounded opportunity
                # to finish before the rollout coordinator takes over child
                # cancellation; competing terminal owners corrupt attribution.
                await asyncio.sleep(min(0.05, cancellation_takeover_at - loop.time()))
                continue
            child_ids = tuple(
                sorted(
                    item.owner_id
                    for item in blockers
                    if item.owner_kind == "subagent_run"
                    and item.owner_id not in cancelled_children
                )
            )
            if child_ids and self.subagent_runtime is not None:
                for child_id in child_ids:
                    cancelled_children.add(child_id)
                    try:
                        await self.subagent_runtime.cancel(
                            child_id,
                            reason_code="subagent_rollout_reservation_reclaimed",
                            reason_message=(
                                "Parent rollout finalization requires its reserved "
                                "model-call capacity."
                            ),
                            cancelled_by="runtime",
                            drain_timeout_seconds=max(
                                0.0, min(0.5, deadline - loop.time())
                            ),
                        )
                    except (KeyError, TimeoutError, SubagentRuntimeError):
                        # A task-batch repair or child terminal owner may have
                        # won the cancellation race. Keep waiting for its
                        # durable settlement; the bounded deadline below still
                        # fails closed if that owner never completes.
                        pass
                continue
            await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
        return False

    async def _stream_model_loop(
        self,
        state: RunActivationWorkingState,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent]:
        for recovered_event in await self.window_compaction_service.recover_interrupted(
            state=state
        ):
            yield recovered_event
        phase_restart_call: ResolvedModelCall | None = None
        phase_restart_model_call_index: int | None = None
        while state.status is LoopStatus.RUNNING:
            if self._apply_stop_request(state):
                break

            async for event in self._project_memory(state):
                yield event

            if phase_restart_call is None:
                model_call_index = _next_model_call_index(state)
                resolved_call = self.llm_runtime.resolve_call(
                    target=self._require_run_model_target(state),
                    purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                )
            else:
                if phase_restart_model_call_index is None:
                    raise RuntimeError("phase restart lost its model call index")
                resolved_call = phase_restart_call
                model_call_index = phase_restart_model_call_index
            (
                phase_event,
                rollout_terminal_reason,
            ) = await self._prepare_rollout_phase_for_model_call(
                state=state,
                resolved_call=resolved_call,
            )
            if phase_event is not None:
                phase_restart_call = resolved_call
                phase_restart_model_call_index = model_call_index
                yield phase_event
                continue
            phase_restart_call = None
            phase_restart_model_call_index = None
            if rollout_terminal_reason is not None:
                state.status = LoopStatus.FAILED
                state.stop_reason = (
                    RunStopReason.EMERGENCY_HARD_STOP
                    if rollout_terminal_reason == "rollout_emergency_hard_stop"
                    else RunStopReason.RUNTIME_EXECUTION_ERROR
                    if rollout_terminal_reason
                    == "rollout_admission_reconciliation_blocked"
                    else RunStopReason.ROLLOUT_EXHAUSTED
                )
                state.error_message = rollout_terminal_reason
                state.transition(LoopTransition.FAIL)
                yield await self._run_ledger.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=rollout_terminal_reason,
                        code=rollout_terminal_reason,
                    ),
                )
                break
            for event in await self._ingest_new_tool_result_projections(
                state=state,
                resolved_call=resolved_call,
            ):
                yield event
            active_run_monitor_lease = (
                await self._run_model.borrow_active_run_monitor_safe_point(
                    run_id=state.run_id,
                    next_model_call_index=model_call_index,
                )
            )
            active_run_prompt_steer_lease = (
                await self._run_model.borrow_active_run_prompt_steer_safe_point(
                    run_id=state.run_id,
                    next_model_call_index=model_call_index,
                )
            )
            memory_prompt = getattr(
                self.memory_hooks, "memory_context_prompt", lambda: None
            )()
            working_set = self._require_run_working_set(state)
            window_policy = working_set.long_horizon_contract.window_policy
            await self._run_context.transcript_projection_checkpoint_service.checkpoint_if_needed(
                context=self._event_context(state),
                run_seed_semantic=working_set.run_transcript_seed_semantic,
                run_seed_reference=working_set.run_transcript_seed_reference,
            )
            compiled_context = None
            compile_attempt_index = 0
            context_retry_index = 0
            safe_point_revision = 0
            provider_input_planning_bundle = None
            provider_input_start_bundle = None
            while state.status is LoopStatus.RUNNING:
                context_id = f"context:{uuid4().hex}"
                provider_input_planning_bundle = None
                provider_input_start_bundle = None
                input_audit = None
                render_output = None
                prepared_context_input = None
                pre_manifest_failure_stage = ContextCompileFailureStage.EVENT_SLICE
                pre_manifest_failure_reason = (
                    ContextInputFailureReasonCode.EVENT_SLICE_INVALID
                )
                try:
                    try:
                        compile_attempt_index = advance_compile_attempt_index(
                            compile_attempt_index,
                            policy=window_policy,
                        )
                    except LongHorizonPreparationBoundExceeded as exc:
                        raise _long_horizon_preparation_error(
                            prepared_context_input=None,
                            reason_code=exc.reason_code,
                            message=(
                                "long-horizon context compile attempt cap exhausted"
                            ),
                        ) from exc
                    local_clock = datetime.now().astimezone()
                    offset = local_clock.strftime("%z")
                    offset_text = (
                        f"UTC{offset[:3]}:{offset[3:]}"
                        if offset
                        else "UTC offset unknown"
                    )
                    timezone_name = local_clock.tzname() or offset_text
                    prepared_context_input = await self._run_context.prepare_live_context_snapshot(
                        working_set=self._require_run_working_set(state),
                        resolved_call=resolved_call,
                        budget=self.budget,
                        system_prompt=self.system_prompt or DEFAULT_SYSTEM_PROMPT,
                        context_id=context_id,
                        model_call_index=model_call_index,
                        compile_attempt_index=compile_attempt_index,
                        context_retry_index=context_retry_index,
                        compiled_at_utc=utc_now(),
                        compiled_local_date=local_clock.date().isoformat(),
                        session_timezone=f"{timezone_name} ({offset_text})",
                        workspace_kind=self.workspace_kind,
                        terminal_current_cwd=str(
                            self._run_model.terminal_sessions.current_cwd(
                                owner_host_session_id=(
                                    self._run_identity.terminal_owner_host_session_id
                                )
                            )
                        ),
                        raw_suspended_state_token_for_validation=(
                            _pending_interaction_authority_fingerprint(self, state)
                        ),
                        memory_scope_instruction=memory_prompt,
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.LONG_HORIZON_FOLD
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.LONG_HORIZON_FOLD_FAILED
                    )
                    (
                        active_window,
                        projection_state,
                        rollout_state,
                    ) = _resolve_prepared_long_horizon_context_facts(
                        prepared_context_input=prepared_context_input,
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.TOOL_RESULT_RENDER
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.TOOL_RESULT_INVALID
                    )
                    render_output = render_prepared_tool_result_units(
                        prepared=prepared_context_input.prepared_tool_results,
                        transcript=(
                            prepared_context_input.normalized_transcript.transcript
                        ),
                        token_estimator=resolved_call.target.token_estimator,
                    )
                    long_horizon_store = self._run_long_horizon.store
                    base_render_output = render_output
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.TOOL_OBSERVATION_PROJECTION
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.TOOL_OBSERVATION_PROJECTION_FAILED
                    )
                    render_output = apply_tool_observation_projection(
                        units=(
                            prepared_context_input.normalized_transcript.tool_result_units
                        ),
                        rendered=base_render_output,
                        projection_state=projection_state,
                        policy=(
                            prepared_context_input.prepared_tool_results.resolved_policy
                        ),
                        token_estimator=resolved_call.target.token_estimator,
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.OBSERVATION_ROLLUP
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.OBSERVATION_ROLLUP_FAILED
                    )
                    prepared_rollups = await self._prepare_active_observation_rollups(
                        state=state,
                        resolved_call=resolved_call,
                        normalized_transcript=(
                            prepared_context_input.normalized_transcript
                        ),
                        projection_state=projection_state,
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.WINDOW_COMPACTION_PLANNING
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.WINDOW_COMPACTION_PLANNING_FAILED
                    )
                    current_run_planning = prepare_current_run_projection_planning_input(
                        run_id=state.run_id,
                        run_start_sequence=(
                            self._require_run_working_set(state).run_start_sequence
                        ),
                        window=active_window,
                        current_projection=projection_state,
                        canonical_slice=prepared_context_input.authority_slice,
                        transcript=(
                            prepared_context_input.normalized_transcript.transcript
                        ),
                        tool_result_units=(
                            prepared_context_input.normalized_transcript.tool_result_units
                        ),
                        context_budget=resolved_call.target.context_budget,
                        allocation_policy=window_policy,
                        estimator=resolved_call.target.fact.token_estimator,
                        pending_interaction=(
                            state.pending_interaction_kind is not None
                        ),
                        tool_call_in_flight=_tool_call_in_flight(state),
                    )
                    projected_soft_target = (
                        resolved_call.target.context_budget.input_budget_tokens
                        * window_policy.tool_projection_soft_ratio_ppm
                        // 1_000_000
                    )
                    projected_post_target = (
                        resolved_call.target.context_budget.input_budget_tokens
                        * window_policy.tool_projection_post_rewrite_ratio_ppm
                        // 1_000_000
                    )
                    projection_unreachable = None
                    if projection_state.total_projected_tokens > projected_soft_target:
                        planning = plan_deterministic_projection_rewrite(
                            event_context=self._event_context(state),
                            window=active_window,
                            current_state=projection_state,
                            units=(
                                prepared_context_input.normalized_transcript.tool_result_units
                            ),
                            base_rendered=base_render_output,
                            render_policy=(
                                prepared_context_input.prepared_tool_results.resolved_policy
                            ),
                            transcript=(
                                prepared_context_input.normalized_transcript.transcript
                            ),
                            token_estimator=resolved_call.target.token_estimator,
                            policy=window_policy,
                            protection_facts=(current_run_planning.protection_facts),
                            target_projected_tokens=projected_post_target,
                            source_through_sequence=(
                                prepared_context_input.authority_slice.through_sequence
                            ),
                            rollup_registry=(self.observation_rollup_renderer_registry),
                            runtime_observation_carrier_available=(
                                resolved_call.target.fact.runtime_observation_carrier
                                is not None
                            ),
                        )
                        plan = (
                            planning.minimum_plan
                            if isinstance(planning, ProjectionTargetUnreachable)
                            else planning
                        )
                        if isinstance(planning, ProjectionTargetUnreachable):
                            projection_unreachable = planning
                        if plan is not None:
                            try:
                                next_safe_point_revision = advance_safe_point_revision(
                                    safe_point_revision,
                                    policy=window_policy,
                                )
                            except LongHorizonPreparationBoundExceeded as exc:
                                raise _long_horizon_preparation_error(
                                    prepared_context_input=prepared_context_input,
                                    reason_code=exc.reason_code,
                                    message=(
                                        "long-horizon safe-point revision cap exhausted"
                                    ),
                                ) from exc
                            carrier = (
                                resolved_call.target.fact.runtime_observation_carrier
                            )
                            if plan.prepared_rollup_artifacts and carrier is None:
                                raise RuntimeError(
                                    "rollup rewrite requires runtime observation carrier"
                                )
                            for prepared_rollup in plan.prepared_rollup_artifacts:
                                assert carrier is not None
                                await self._run_context.materialize_observation_rollup(
                                    run_id=state.run_id,
                                    prepared=prepared_rollup,
                                    carrier=carrier,
                                )
                            stored_rewrite = tuple(
                                await self._run_ledger.emit_many(
                                    plan.events,
                                )
                            )
                            if tuple(event.id for event in stored_rewrite) != tuple(
                                event.id for event in plan.events
                            ):
                                raise RuntimeError(
                                    "projection rewrite committed unexpected events"
                                )
                            if (
                                long_horizon_store.projection_state(
                                    active_window.window_id
                                )
                                != plan.final_state
                            ):
                                raise RuntimeError(
                                    "projection rewrite reducer differs from plan"
                                )
                            safe_point_revision = next_safe_point_revision
                            # Rebuild the draft from a fresh authority slice;
                            # no v2 manifest is persisted for the old generation.
                            continue
                    validate_prepared_tool_result_render_output(
                        output=render_output,
                        resolved_call=resolved_call,
                        context_id=context_id,
                        model_call_index=model_call_index,
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.CONTEXT_COMPILE
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.CANDIDATE_INVALID
                    )
                    historical_provider_source_heads = self._run_model.provider_input_generation_coordinator.committed_source_heads_for_compiled_call(
                        prepared_context_input=prepared_context_input
                    )
                    draft_compiled_context = compile_context_from_facts(
                        facts=prepared_context_input.invocation,
                        transcript=prepared_context_input.normalized_transcript.transcript,
                        rendered_tool_results=render_output,
                        prepared_rollups=prepared_rollups,
                        section_candidates=prepared_context_input.prepared_candidates,
                        context_source_hydrated_contents=(
                            prepared_context_input.context_source_hydrated_contents
                        ),
                        transcript_stable_entries=(
                            prepared_context_input.transcript_projection_evidence.stable_entries
                        ),
                        historical_provider_source_heads=(
                            historical_provider_source_heads
                        ),
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.CONTEXT_BUDGET
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.CONTEXT_BUDGET_EXCEEDED
                    )
                    working_set = self._require_run_working_set(state)
                    long_horizon_budget = measure_long_horizon_context_budget(
                        call=resolved_call,
                        context=draft_compiled_context.llm_context,
                        estimate=draft_compiled_context.final_token_estimate,
                        window=active_window,
                        projection_state=projection_state,
                        policy=window_policy,
                    )
                    window_compaction_required = (
                        long_horizon_budget.decision.decision
                        == "window_compaction_required"
                        or long_horizon_budget.decision.unit_count_limit_exceeded
                    )
                    if window_compaction_required:
                        try:
                            next_safe_point_revision = advance_safe_point_revision(
                                safe_point_revision,
                                policy=window_policy,
                            )
                        except LongHorizonPreparationBoundExceeded as exc:
                            raise _long_horizon_preparation_error(
                                prepared_context_input=prepared_context_input,
                                reason_code=exc.reason_code,
                                message=(
                                    "window compaction safe-point revision cap exhausted"
                                ),
                            ) from exc
                        outcome = await self.window_compaction_service.compact(
                            WindowCompactionRequest(
                                event_context=self._event_context(state),
                                state=state,
                                run_contract=(working_set.long_horizon_contract),
                                source_window=active_window,
                                source_projection=projection_state,
                                transcript=(
                                    prepared_context_input.normalized_transcript.transcript
                                ),
                                tool_result_units=(
                                    prepared_context_input.normalized_transcript.tool_result_units
                                ),
                                rendered_tool_results=render_output,
                                prepared_rollups=prepared_rollups,
                                protection_facts=(
                                    current_run_planning.protection_facts
                                ),
                                source_through_sequence=(
                                    prepared_context_input.authority_slice.through_sequence
                                ),
                                source_context_fingerprint=(
                                    provider_neutral_payload_fingerprint(
                                        draft_compiled_context.llm_context
                                    )
                                ),
                                estimated_tokens_before=(
                                    draft_compiled_context.final_token_estimate.total_input_tokens
                                ),
                                non_transcript_baseline_tokens=(
                                    draft_compiled_context.budget.non_transcript_baseline_tokens
                                ),
                                transcript_tokens_before=(
                                    draft_compiled_context.budget.transcript_estimated_tokens
                                ),
                                pending_interaction=(
                                    state.pending_interaction_kind is not None
                                ),
                                tool_call_in_flight=_tool_call_in_flight(state),
                            )
                        )
                        if outcome.status == "compacted":
                            safe_point_revision = next_safe_point_revision
                            # The success batch has already folded the new active
                            # window. Rebuild every context fact from a new slice.
                            continue
                        if outcome.status == "phase_transitioned":
                            safe_point_revision = next_safe_point_revision
                            # The account phase changed durably. Rebuild the
                            # window, projection, snapshot and reservation basis.
                            continue
                        if outcome.status == "source_stale":
                            safe_point_revision = next_safe_point_revision
                            # Background facts advanced the ledger after the
                            # authority slice froze. Rebuild instead of charging
                            # the compaction failure circuit.
                            continue
                        if (
                            long_horizon_budget.decision.unit_count_limit_exceeded
                            or draft_compiled_context.final_token_estimate.total_input_tokens
                            > resolved_call.target.context_budget.input_budget_tokens
                        ):
                            raise _long_horizon_preparation_error(
                                prepared_context_input=prepared_context_input,
                                reason_code=(
                                    ContextInputFailureReasonCode.CONTEXT_BUDGET_EXCEEDED
                                ),
                                message=(
                                    "required window compaction did not produce a usable window: "
                                    f"{outcome.reason_code or outcome.status}"
                                ),
                            )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.PAYLOAD_CONSISTENCY
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.PAYLOAD_CONSISTENCY_FAILED
                    )
                    long_horizon_attribution = build_long_horizon_context_attribution(
                        run_contract_fingerprint=(
                            working_set.long_horizon_contract.contract_fingerprint
                        ),
                        active_window=active_window,
                        projection_state=projection_state,
                        projection_rewrite_event_refs=(
                            _active_projection_rewrite_refs(
                                prepared_context_input=prepared_context_input,
                                window_id=active_window.window_id,
                                projection_generation=(
                                    projection_state.projection_generation
                                ),
                            )
                        ),
                        rollout_account_owner_runtime_session_id=(
                            working_set.long_horizon_contract.rollout_account_owner_runtime_session_id
                        ),
                        rollout_state=rollout_state,
                        subagent_graph_semantic_source=(
                            prepared_context_input.snapshot_build_input.subagent_graph_semantic_source
                        ),
                        context_budget_decision=long_horizon_budget.decision,
                    )
                    try:
                        snapshot_fact = build_context_snapshot(
                            prepared_context_input.snapshot_build_input,
                            long_horizon_attribution=long_horizon_attribution,
                        )
                    except Exception as exc:
                        raise _context_finalization_preparation_error(
                            prepared_context_input,
                            failure_stage="snapshot_build",
                            reason_code=(
                                ContextInputFailureReasonCode.SNAPSHOT_JOIN_MISMATCH
                            ),
                            cause=exc,
                        ) from exc
                    prepared_context_input = replace(
                        prepared_context_input,
                        invocation=bind_context_invocation(
                            fact=snapshot_fact,
                            resolved_call=resolved_call,
                            materialized_tool_specs=(
                                prepared_context_input.invocation.materialized_tool_specs
                            ),
                        ),
                    )
                    final_compiled_context = compile_context_from_facts(
                        facts=prepared_context_input.invocation,
                        transcript=prepared_context_input.normalized_transcript.transcript,
                        rendered_tool_results=render_output,
                        prepared_rollups=prepared_rollups,
                        section_candidates=prepared_context_input.prepared_candidates,
                        context_source_hydrated_contents=(
                            prepared_context_input.context_source_hydrated_contents
                        ),
                        transcript_stable_entries=(
                            prepared_context_input.transcript_projection_evidence.stable_entries
                        ),
                        historical_provider_source_heads=(
                            historical_provider_source_heads
                        ),
                    )
                    if active_run_monitor_lease is not None:
                        final_compiled_context = replace(
                            final_compiled_context,
                            active_run_monitor_attachments=(
                                active_run_monitor_lease.attachments
                            ),
                        )
                    if active_run_prompt_steer_lease is not None:
                        ordered_projection = final_compiled_context.prepared_ordered_transcript_projection
                        if ordered_projection is None:
                            raise RuntimeError(
                                "active-run prompt steer lacks ordered transcript projection"
                            )
                        final_compiled_context = replace(
                            final_compiled_context,
                            prepared_ordered_transcript_projection=(
                                append_same_batch_user_steer(
                                    prepared=ordered_projection,
                                    runtime_session_id=(
                                        self._run_identity.runtime_session_id
                                    ),
                                    context_id=context_id,
                                    queue_item_id=(
                                        active_run_prompt_steer_lease.queue_item_id
                                    ),
                                    reservation_fingerprint=(
                                        active_run_prompt_steer_lease.reservation_fingerprint
                                    ),
                                    expected_user_steer_event_id=(
                                        active_run_prompt_steer_lease.expected_user_steer_event_id
                                    ),
                                    message_id=(
                                        active_run_prompt_steer_lease.message_id
                                    ),
                                    text=active_run_prompt_steer_lease.text,
                                    content_semantic_fingerprint=(
                                        active_run_prompt_steer_lease.content_semantic_fingerprint
                                    ),
                                    policy=(
                                        final_compiled_context.provider_causal_physical_policy
                                    ),
                                )
                            ),
                        )
                    if (
                        provider_neutral_payload_fingerprint(
                            draft_compiled_context.llm_context
                        )
                        != provider_neutral_payload_fingerprint(
                            final_compiled_context.llm_context
                        )
                        or draft_compiled_context.final_token_estimate
                        != final_compiled_context.final_token_estimate
                    ):
                        raise RuntimeError(
                            "final long-horizon attribution changed compiled payload"
                        )
                    _validate_prepared_context_input(
                        prepared_context_input=prepared_context_input,
                        compiled_context=final_compiled_context,
                    )
                    provider_input_planning_bundle = await self._run_model.provider_input_generation_coordinator.prepare_compiled_call(
                        call=resolved_call,
                        compiled_context=final_compiled_context,
                        prepared_context_input=prepared_context_input,
                        event_context=self._event_context(state),
                    )
                    final_compiled_context = _bind_compiled_context_to_provider_input(
                        compiled_context=final_compiled_context,
                        provider_input_start_bundle=provider_input_planning_bundle,
                        resolved_call=resolved_call,
                    )
                    actual_long_horizon_budget = measure_long_horizon_context_budget(
                        call=resolved_call,
                        context=final_compiled_context.llm_context,
                        estimate=final_compiled_context.final_token_estimate,
                        window=active_window,
                        projection_state=projection_state,
                        policy=window_policy,
                    )
                    if (
                        actual_long_horizon_budget.decision.decision
                        == "window_compaction_required"
                        and long_horizon_budget.decision.decision
                        != "window_compaction_required"
                    ):
                        provider_input_planning_bundle = None
                        raise _long_horizon_preparation_error(
                            prepared_context_input=prepared_context_input,
                            reason_code=(
                                ContextInputFailureReasonCode.CONTEXT_BUDGET_EXCEEDED
                            ),
                            message=(
                                "canonical provider input crossed the window "
                                "compaction trigger after generation planning"
                            ),
                        )
                    long_horizon_budget = actual_long_horizon_budget
                    long_horizon_attribution = build_long_horizon_context_attribution(
                        run_contract_fingerprint=(
                            working_set.long_horizon_contract.contract_fingerprint
                        ),
                        active_window=active_window,
                        projection_state=projection_state,
                        projection_rewrite_event_refs=(
                            _active_projection_rewrite_refs(
                                prepared_context_input=prepared_context_input,
                                window_id=active_window.window_id,
                                projection_generation=(
                                    projection_state.projection_generation
                                ),
                            )
                        ),
                        rollout_account_owner_runtime_session_id=(
                            working_set.long_horizon_contract.rollout_account_owner_runtime_session_id
                        ),
                        rollout_state=rollout_state,
                        subagent_graph_semantic_source=(
                            prepared_context_input.snapshot_build_input.subagent_graph_semantic_source
                        ),
                        context_budget_decision=long_horizon_budget.decision,
                    )
                    snapshot_fact = build_context_snapshot(
                        prepared_context_input.snapshot_build_input,
                        long_horizon_attribution=long_horizon_attribution,
                    )
                    prepared_context_input = replace(
                        prepared_context_input,
                        invocation=bind_context_invocation(
                            fact=snapshot_fact,
                            resolved_call=resolved_call,
                            materialized_tool_specs=(
                                prepared_context_input.invocation.materialized_tool_specs
                            ),
                        ),
                    )
                    try:
                        if (
                            final_compiled_context.prepared_ordered_transcript_projection
                            is None
                            or provider_input_planning_bundle is None
                        ):
                            raise RuntimeError(
                                "compiled session call lacks ordered provider-input plan"
                            )
                        projection_unreachable_audit = (
                            projection_target_unreachable_audit(projection_unreachable)
                            if projection_unreachable is not None
                            else None
                        )
                        prepared_transcript_projection = prepare_transcript_projection_input(
                            evidence=(
                                prepared_context_input.transcript_projection_evidence
                            ),
                            normalized=(prepared_context_input.normalized_transcript),
                            provider_projection=(
                                final_compiled_context.prepared_transcript_provider_projection
                            ),
                            semantic_selection=(
                                final_compiled_context.model_visible_named_fact_semantic_selection
                            ),
                            prepared_candidates=(
                                prepared_context_input.prepared_candidates
                            ),
                            prepared_artifacts=(
                                prepared_context_input.prepared_named_fact_artifacts
                            ),
                            fallback_source_ref=(snapshot_fact.run_entry.run_start),
                            authority_events=(
                                *tuple(prepared_context_input.authority_slice.events),
                                *tuple(
                                    event
                                    for event_slice in prepared_context_input.named_slices
                                    for event in event_slice.events
                                ),
                                *prepared_context_input.exact_named_authority_events,
                            ),
                        )
                        input_manifest = build_context_input_manifest(
                            snapshot=snapshot_fact,
                            prepared_transcript_projection=(
                                prepared_transcript_projection
                            ),
                            prepared_tool_results=(
                                prepared_context_input.prepared_tool_results
                            ),
                            rendered_tool_results=render_output,
                            active_window=active_window,
                            window_policy=window_policy,
                            projection_state=projection_state,
                            prepared_rollups=prepared_rollups,
                            rollout_state=rollout_state,
                            context_budget_decision=long_horizon_budget.decision,
                            projection_pressure_shadow=(
                                long_horizon_budget.pressure_shadow
                            ),
                            projection_target_unreachable=(
                                projection_unreachable_audit
                            ),
                            safe_point_revision=safe_point_revision,
                            prepared_candidates=(
                                prepared_context_input.prepared_candidates
                            ),
                            ordered_transcript_projection=(
                                final_compiled_context.prepared_ordered_transcript_projection.projection
                            ),
                            ordered_transcript_projection_identity=(
                                final_compiled_context.prepared_ordered_transcript_projection.identity
                            ),
                            prepared_provider_input_plan=(
                                provider_input_planning_bundle.prepared_plan
                            ),
                        )
                        manifest_candidate = build_context_input_manifest_candidate(
                            input_manifest
                        )
                    except Exception as exc:
                        raise _context_manifest_preparation_error(
                            prepared_context_input,
                            cause=exc,
                        ) from exc
                    try:
                        manifest_write = await (
                            self._run_context.context_input_manifest_service.persist(
                                manifest_candidate,
                                deadline_monotonic=time.monotonic() + 30.0,
                            )
                        )
                    except (
                        ContextInputManifestConfirmedAbsent,
                        ContextInputManifestWriteConflict,
                        ContextInputManifestWriteDeadlineExceeded,
                        ContextInputManifestWriteOutcomeUnknown,
                    ) as exc:
                        input_failure = _context_manifest_input_failure(
                            snapshot=prepared_context_input,
                            manifest=input_manifest,
                            candidate=manifest_candidate,
                            error=exc,
                        )
                        state.status = LoopStatus.FAILED
                        state.stop_reason = RunStopReason.MODEL_ERROR
                        state.error_message = str(exc)
                        state.transition(LoopTransition.FAIL)
                        yield await self._run_ledger.emit(
                            ContextCompiledEvent(
                                **self._event_context(state).event_fields(),
                                status="failed",
                                failure_stage="input_manifest_write",
                                context_id=context_id,
                                model_call_index=model_call_index,
                                compile_attempt_index=compile_attempt_index,
                                context_retry_index=context_retry_index,
                                resolved_call=resolved_call.fact,
                                budget=_empty_context_budget_report(resolved_call),
                                input_failure=input_failure,
                            ),
                        )
                        yield await self._run_ledger.emit(
                            RunErrorEvent(
                                **self._event_context(state).event_fields(),
                                message=str(exc),
                                code="context_input_manifest_write_failed",
                            ),
                        )
                        if isinstance(
                            exc,
                            (
                                ContextInputManifestWriteConflict,
                                ContextInputManifestWriteOutcomeUnknown,
                            ),
                        ):
                            self._require_run_finalization_owner(
                                state
                            ).context_input_latch_after_terminalization = True
                        break
                    manifest_projection_reference = (
                        build_context_input_manifest_projection_reference(
                            manifest=input_manifest,
                            candidate=manifest_candidate,
                            write_result=ContextInputManifestWriteResult(
                                outcome=manifest_write.outcome,
                                artifact_id=manifest_write.artifact_id,
                                content_fingerprint=manifest_write.content_fingerprint,
                            ),
                        )
                    )
                    provider_input_start_bundle = await self._run_model.provider_input_generation_coordinator.finalize_compiled_call(
                        call=resolved_call,
                        compiled_context=final_compiled_context,
                        prepared_context_input=prepared_context_input,
                        event_context=self._event_context(state),
                        planning_bundle=provider_input_planning_bundle,
                        manifest_projection_reference=manifest_projection_reference,
                    )
                    input_audit = build_context_compile_input_audit(
                        manifest=input_manifest,
                        candidate=manifest_candidate,
                        write_result=ContextInputManifestWriteResult(
                            outcome=manifest_write.outcome,
                            artifact_id=manifest_write.artifact_id,
                            content_fingerprint=manifest_write.content_fingerprint,
                        ),
                        transcript_message_count=len(
                            prepared_context_input.normalized_transcript.transcript.messages
                        ),
                        transcript_pair_count=len(
                            prepared_context_input.normalized_transcript.transcript.tool_pairs
                        ),
                        tool_result_unit_count=len(
                            prepared_context_input.prepared_tool_results.units
                        ),
                    )
                    compiled_context = final_compiled_context
                    break
                except ContextInputPreparationError as exc:
                    if (
                        exc.reason_code
                        is ContextInputFailureReasonCode.LEDGER_UNTRUSTED
                        or self._run_ledger.reconciliation_required
                    ):
                        raise
                    input_failure = _context_pre_manifest_input_failure(
                        error=exc,
                        context_id=context_id,
                        resolved_model_call_id=(
                            resolved_call.fact.resolved_model_call_id
                        ),
                        model_call_index=model_call_index,
                        compile_attempt_index=compile_attempt_index,
                        context_retry_index=context_retry_index,
                    )
                    state.status = LoopStatus.FAILED
                    state.stop_reason = RunStopReason.MODEL_ERROR
                    state.error_message = str(exc)
                    state.transition(LoopTransition.FAIL)
                    yield await self._run_ledger.emit(
                        ContextCompiledEvent(
                            **self._event_context(state).event_fields(),
                            status="failed",
                            failure_stage=exc.failure_stage,
                            context_id=context_id,
                            model_call_index=model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                            resolved_call=resolved_call.fact,
                            budget=_empty_context_budget_report(resolved_call),
                            diagnostics=[
                                {
                                    "severity": "error",
                                    "code": exc.reason_code.value,
                                    "message": str(exc)[:512],
                                    "failure_stage": exc.failure_stage,
                                }
                            ],
                            input_failure=input_failure,
                        ),
                    )
                    yield await self._run_ledger.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=str(exc),
                            code=f"context_input_{exc.reason_code.value}",
                        ),
                    )
                    break
                except ContextBudgetExceeded as exc:
                    failed_context_id = (
                        exc.context_id or f"context:failed:{uuid4().hex}"
                    )
                    failed_model_call_index = exc.model_call_index or model_call_index
                    pressure_diagnostics = [
                        diagnostic.to_event_value() for diagnostic in exc.diagnostics
                    ]
                    pressure_tool_result_render_decisions = [
                        dict(decision) for decision in exc.tool_result_render_decisions
                    ]
                    pressure_tool_result_budget_report = dict(
                        exc.tool_result_budget_report
                    )
                    if exc.budget_report is None:
                        raise RuntimeError(
                            "ContextBudgetExceeded is missing its resolved budget report"
                        ) from exc
                    pressure_input_failure = input_audit is None
                    if pressure_input_failure:
                        input_failure = _context_budget_input_failure(
                            prepared_context_input=prepared_context_input,
                            context_id=failed_context_id,
                            resolved_model_call_id=(
                                resolved_call.fact.resolved_model_call_id
                            ),
                            model_call_index=failed_model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                        )
                    yield await self._run_ledger.emit(
                        ContextCompiledEvent(
                            **self._event_context(state).event_fields(),
                            status="pressure",
                            failure_stage=(
                                "context_budget" if pressure_input_failure else None
                            ),
                            context_id=failed_context_id,
                            model_call_index=failed_model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                            resolved_call=resolved_call.fact,
                            budget=exc.budget_report.to_event_value(),
                            sections=[],
                            tool_specs=[],
                            diagnostics=pressure_diagnostics,
                            lifecycle_decisions=[],
                            tool_result_render_decisions=pressure_tool_result_render_decisions,
                            tool_result_budget_report=pressure_tool_result_budget_report,
                            input_audit=input_audit,
                            input_failure=(
                                input_failure if pressure_input_failure else None
                            ),
                        ),
                    )
                    if (
                        context_retry_index == 0
                        and _context_budget_pressure_is_recoverable(exc)
                    ):
                        compaction_result = (
                            await self._maybe_compact_mid_turn_before_followup(state)
                        )
                        for event in compaction_result.events:
                            yield event
                        if compaction_result.compacted:
                            context_retry_index += 1
                            continue
                    state.status = LoopStatus.FAILED
                    state.stop_reason = RunStopReason.MODEL_ERROR
                    state.error_message = str(exc)
                    state.transition(LoopTransition.FAIL)
                    yield await self._run_ledger.emit(
                        ContextCompiledEvent(
                            **self._event_context(state).event_fields(),
                            status="failed",
                            failure_stage="context_budget",
                            context_id=failed_context_id,
                            model_call_index=failed_model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                            resolved_call=resolved_call.fact,
                            budget=exc.budget_report.to_event_value(),
                            sections=[],
                            tool_specs=[],
                            diagnostics=pressure_diagnostics,
                            lifecycle_decisions=[],
                            tool_result_render_decisions=pressure_tool_result_render_decisions,
                            tool_result_budget_report=pressure_tool_result_budget_report,
                            input_audit=input_audit,
                            input_failure=(
                                input_failure if input_audit is None else None
                            ),
                        ),
                    )
                    yield await self._run_ledger.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=str(exc),
                            code="context_budget_exceeded",
                        ),
                    )
                    break
                except Exception as exc:
                    if self._run_ledger.reconciliation_required:
                        raise
                    input_failure = None
                    if input_audit is None:
                        preparation_error = _context_stage_preparation_error(
                            prepared_context_input=prepared_context_input,
                            failure_stage=pre_manifest_failure_stage,
                            reason_code=pre_manifest_failure_reason,
                            cause=exc,
                        )
                        failure_stage = preparation_error.failure_stage
                        diagnostic_code = preparation_error.reason_code.value
                        input_failure = _context_pre_manifest_input_failure(
                            error=preparation_error,
                            context_id=context_id,
                            resolved_model_call_id=(
                                resolved_call.fact.resolved_model_call_id
                            ),
                            model_call_index=model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                        )
                    else:
                        failure_stage = (
                            "tool_result_render"
                            if render_output is None
                            else "context_compile"
                        )
                        diagnostic_code = f"context_{failure_stage}_failed"
                    state.status = LoopStatus.FAILED
                    state.stop_reason = RunStopReason.MODEL_ERROR
                    state.error_message = str(exc)
                    state.transition(LoopTransition.FAIL)
                    yield await self._run_ledger.emit(
                        ContextCompiledEvent(
                            **self._event_context(state).event_fields(),
                            status="failed",
                            failure_stage=failure_stage,
                            context_id=context_id,
                            model_call_index=model_call_index,
                            compile_attempt_index=compile_attempt_index,
                            context_retry_index=context_retry_index,
                            resolved_call=resolved_call.fact,
                            budget=_empty_context_budget_report(resolved_call),
                            diagnostics=[
                                {
                                    "severity": "error",
                                    "code": diagnostic_code,
                                    "message": (f"{type(exc).__name__}: {exc}")[:512],
                                    "failure_stage": failure_stage,
                                }
                            ],
                            input_audit=input_audit,
                            input_failure=input_failure,
                        ),
                    )
                    yield await self._run_ledger.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=f"{type(exc).__name__}: {exc}",
                            code=diagnostic_code,
                        ),
                    )
                    break
            if compiled_context is None:
                if active_run_monitor_lease is not None:
                    self._run_model.release_active_run_monitor_safe_point(
                        active_run_monitor_lease
                    )
                if active_run_prompt_steer_lease is not None:
                    self._run_model.release_active_run_prompt_steer_safe_point(
                        active_run_prompt_steer_lease
                    )
                if provider_input_start_bundle is not None:
                    await self._run_model.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                        provider_input_start_bundle.prepared_candidate.preparation_ownership.preparation_id,
                        reason="run_terminated_before_start",
                    )
                break
            if provider_input_start_bundle is None:
                raise RuntimeError(
                    "compiled context lacks its prepared provider-input owner"
                )
            context = provider_input_start_bundle.carrier.to_llm_context(
                replace(
                    compiled_context.llm_context,
                    resolved_model_call_id=(resolved_call.fact.resolved_model_call_id),
                    target_fingerprint=(resolved_call.target.fact.target_fingerprint),
                )
            )
            actual_provider_estimate = (
                resolved_call.target.token_estimator.estimate_context(context)
            )
            if (
                actual_provider_estimate.total_input_tokens
                > resolved_call.target.context_budget.input_budget_tokens
            ):
                if active_run_monitor_lease is not None:
                    self._run_model.release_active_run_monitor_safe_point(
                        active_run_monitor_lease
                    )
                if active_run_prompt_steer_lease is not None:
                    self._run_model.release_active_run_prompt_steer_safe_point(
                        active_run_prompt_steer_lease
                    )
                await self._run_model.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                    provider_input_start_bundle.prepared_candidate.preparation_ownership.preparation_id,
                    reason="resolved_target_invalidated_before_start",
                )
                raise ContextBudgetExceeded(
                    "Canonical append-only provider input exceeds the resolved input budget.",
                    context_id=compiled_context.context_id,
                    model_call_index=model_call_index,
                    diagnostics=(),
                    tool_result_render_decisions=(
                        compiled_context.tool_result_render_decisions
                    ),
                    tool_result_budget_report=dict(
                        compiled_context.tool_result_budget_report
                    ),
                    budget_report=replace(
                        compiled_context.budget,
                        final_payload_estimated_tokens=(
                            actual_provider_estimate.total_input_tokens
                        ),
                    ),
                )
            context = replace(
                context,
                compiler_estimated_input_tokens=(
                    actual_provider_estimate.total_input_tokens
                ),
            )
            long_horizon_diagnostics = list(
                long_horizon_context_diagnostics(
                    measurement=long_horizon_budget,
                    target_unreachable=input_manifest.projection_target_unreachable,
                )
            )
            state.model_tool_progress.current_context_id = compiled_context.context_id
            state.model_tool_progress.current_model_call_index = model_call_index
            context_compiled_candidate = ContextCompiledEvent(
                **self._event_context(state).event_fields(),
                context_id=compiled_context.context_id,
                model_call_index=model_call_index,
                compile_attempt_index=compile_attempt_index,
                context_retry_index=context_retry_index,
                resolved_call=resolved_call.fact,
                budget=compiled_context.budget.to_event_value(),
                sections=[
                    section.to_event_value() for section in compiled_context.sections
                ],
                tool_specs=[
                    tool.to_event_value() for tool in compiled_context.tool_specs
                ],
                diagnostics=[
                    diagnostic.to_event_value()
                    for diagnostic in compiled_context.diagnostics
                ]
                + long_horizon_diagnostics,
                lifecycle_decisions=[
                    dict(decision) for decision in compiled_context.lifecycle_decisions
                ],
                tool_result_render_decisions=[
                    dict(decision)
                    for decision in compiled_context.tool_result_render_decisions
                ],
                tool_result_budget_report=dict(
                    compiled_context.tool_result_budget_report
                ),
                tool_result_render_decision_facts=(
                    compiled_context.tool_result_render_decision_facts
                ),
                tool_result_render_operational_facts=(
                    compiled_context.tool_result_render_operational_facts
                ),
                long_horizon_context_budget_decision=(long_horizon_budget.decision),
                long_horizon_projection_pressure_shadow=(
                    long_horizon_budget.pressure_shadow
                ),
                input_audit=input_audit,
                provider_neutral_payload_fingerprint=(
                    provider_neutral_payload_fingerprint(context)
                ),
                canonical_render_decisions_fingerprint=(
                    canonical_render_decisions_fingerprint(
                        compiled_context.tool_result_render_decision_facts
                    )
                ),
                prepared_provider_input=(
                    provider_input_start_bundle.prepared_candidate
                ),
                manifest_projection_reference=manifest_projection_reference,
                prepared_provider_input_plan_fingerprint=(
                    provider_input_start_bundle.prepared_plan.plan_fingerprint
                ),
                prepared_provider_input_candidate_fingerprint=(
                    provider_input_start_bundle.prepared_candidate.candidate_fingerprint
                ),
            )
            try:
                stored_context_compiled = await self._run_ledger.emit(
                    context_compiled_candidate,
                )
            except EventPublicationAfterCommitError as exc:
                if any(
                    event.id == context_compiled_candidate.id
                    for event in exc.result.committed_events
                ):
                    self._commit_prepared_context_caches(
                        prepared_context_input=prepared_context_input,
                        render_output=render_output,
                    )
                raise
            self._commit_prepared_context_caches(
                prepared_context_input=prepared_context_input,
                render_output=render_output,
            )
            yield stored_context_compiled
            selected_subagent_result_ids = (
                prepared_context_input.invocation.fact.candidate_source_selections[
                    0
                ].selected_source_ids
            )
            if selected_subagent_result_ids and self.subagent_runtime is None:
                raise RuntimeError(
                    "canonical subagent selection lacks a bound graph runtime"
                )
            selected_subagent_results = (
                self.subagent_runtime.materialize_result_selection(
                    selected_subagent_result_ids
                )
                if selected_subagent_result_ids
                else ()
            )
            deliverable_subagent_results = (
                selected_subagent_results
                if _compiled_source_included(compiled_context, "subagent_results")
                else ()
            )
            delivered_subagent_results = False

            reply_had_run_error = False
            accepted_control_permit = None
            active_run_monitor_replan_required = False
            model_step_attempt = ModelStepAttempt.install(
                self._require_active_activation_coordinator(state),
                model_step_ordinal=model_call_index,
            )
            model_step_attempt.begin_dispatch(
                self._require_active_activation_coordinator(state)
            )
            try:
                run_activation = (
                    state.run_working_set.run_execution_activation
                    if state.run_working_set is not None
                    else None
                )
                active_run_monitor_delivery = (
                    build_active_run_monitor_delivery(
                        lease=active_run_monitor_lease,
                        provider_input_start_bundle=provider_input_start_bundle,
                    )
                    if active_run_monitor_lease is not None
                    else None
                )
                active_run_monitor_source_references = (
                    tuple(
                        event_reference_from_stored(
                            event,
                            runtime_session_id=self._run_identity.runtime_session_id,
                        )
                        for event in active_run_monitor_lease.source_events
                    )
                    if active_run_monitor_lease is not None
                    else ()
                )
                active_run_prompt_steer_guard = None
                active_run_prompt_steer_extra_candidates = ()
                active_run_prompt_steer_transaction_companion = None
                if active_run_prompt_steer_lease is not None:
                    (
                        active_run_prompt_steer_guard,
                        active_run_prompt_steer_extra_candidates,
                        prompt_steer_queue_companion,
                    ) = self._run_model.prepare_active_run_prompt_steer_start(
                        lease=active_run_prompt_steer_lease,
                        provider_input_start_bundle=provider_input_start_bundle,
                        event_context=self._event_context(state),
                    )
                start_bundle = self._run_model.prepare_lifecycle_start_bundle(
                    call=resolved_call,
                    context=context,
                    event_context=self._event_context(state),
                    lifecycle_kind="main_assistant_reply",
                    run_execution_activation=run_activation,
                    provider_input_start_bundle=provider_input_start_bundle,
                    active_run_monitor_delivery=active_run_monitor_delivery,
                    active_run_monitor_source_event_references=(
                        active_run_monitor_source_references
                    ),
                    active_run_prompt_steer_guard=(active_run_prompt_steer_guard),
                    extra_companion_candidates=(
                        active_run_prompt_steer_extra_candidates
                    ),
                )
                if active_run_prompt_steer_lease is not None:
                    active_run_prompt_steer_transaction_companion = (
                        self._run_model.build_prompt_steer_start_transaction_companion(
                            queue_companion=prompt_steer_queue_companion,
                            resolved_model_call_id=(
                                resolved_call.fact.resolved_model_call_id
                            ),
                            model_call_start_event_id=(
                                start_bundle.recovery_plan.model_call_start_event_id
                            ),
                        )
                    )
                model_stream_handle = self.llm_runtime.start_stream(
                    call=resolved_call,
                    context=context,
                    event_context=self._event_context(state),
                    start_bundle=start_bundle,
                    commit_port=self._run_model.event_commit_port(
                        start_transaction_companion=(
                            active_run_prompt_steer_transaction_companion
                        )
                    ),
                    execution_registry=(
                        self._run_model.model_stream_execution_registry
                    ),
                )
                subscription = model_stream_handle.subscribe()
                try:
                    async for stored in subscription:
                        if isinstance(stored, RunErrorEvent):
                            reply_had_run_error = True
                        yield stored
                        if (
                            isinstance(stored, ModelCallStartEvent)
                            and deliverable_subagent_results
                            and not delivered_subagent_results
                            and self.subagent_runtime is not None
                            and self._subagent_parent_features_enabled
                            and stored.context_id == compiled_context.context_id
                            and stored.model_call_index == model_call_index
                        ):
                            delivered_subagent_results = True
                            delivered_events = (
                                await self.subagent_runtime.mark_results_delivered(
                                    deliverable_subagent_results,
                                    event_context=self._event_context(state),
                                    context_id=compiled_context.context_id,
                                    model_call_index=model_call_index,
                                    section_id=_SUBAGENT_RESULTS_SECTION_ID,
                                )
                            )
                            for delivered_event in delivered_events:
                                yield delivered_event
                    completion = await model_stream_handle.wait_completed()
                    if completion.terminal_outcome in {
                        "completed",
                        "provider_error",
                        "cancelled",
                        "runtime_error",
                    }:
                        committed_model_result = await model_stream_handle.wait_result()
                    elif completion.terminal_outcome == "rejected_before_start":
                        # Final validation failures are deterministic for this
                        # exact call/context pair. Surface the typed failure to
                        # the caller instead of treating an empty stream as a
                        # retryable provider failure.
                        await model_stream_handle.wait_result()
                        raise RuntimeError(
                            "rejected model stream unexpectedly produced a result"
                        )
                    else:
                        committed_model_result = None
                    if completion.terminal_outcome == "completed":
                        if committed_model_result is None:
                            raise RuntimeError(
                                "completed model stream lacks a committed result"
                            )
                        working_set = state.run_working_set
                        control_owner = (
                            working_set.model_call_control_owner
                            if working_set is not None
                            else None
                        )
                        if control_owner is None:
                            raise RuntimeError(
                                "main model call lacks its live control owner"
                            )
                        control_resolution = (
                            await self._run_model.resolve_completed_control_call(
                                control_owner,
                                result=committed_model_result,
                                model_call_index=model_call_index,
                                event_context=self._event_context(state),
                            )
                        )
                        progress = state.model_tool_progress
                        progress.latest_model_control_disposition_event_id = (
                            control_resolution.disposition_event.id
                        )
                        progress.latest_model_control_disposition_model_call_index = (
                            model_call_index
                        )
                        yield control_resolution.disposition_event
                        accepted_control_permit = control_resolution.accepted_permit
                        if accepted_control_permit is None:
                            reply_had_run_error = True
                    else:
                        reply_had_run_error = True
                finally:
                    await subscription.detach()
            except (
                ModelInputBudgetExceeded,
                ModelInputEstimateMismatch,
                ModelContextIdentityMismatch,
                ModelTargetCapabilityMismatch,
                ModelTargetBindingMismatch,
            ) as exc:
                estimate = getattr(exc, "estimate", None)
                estimated_input_tokens = (
                    estimate.total_input_tokens if estimate is not None else None
                )
                yield await self._run_ledger.emit(
                    ModelCallRejectedEvent(
                        **self._event_context(state).event_fields(),
                        resolved_call=resolved_call.fact,
                        context_id=compiled_context.context_id,
                        model_call_index=model_call_index,
                        reason_code=exc.reason_code,
                        estimated_input_tokens=estimated_input_tokens,
                        input_budget_tokens=(
                            resolved_call.target.fact.context_budget.input_budget_tokens
                        ),
                        diagnostics=(
                            ModelCallDiagnosticFact(
                                code=exc.reason_code,
                                message=str(exc)[:512],
                            ),
                        ),
                    ),
                )
                state.status = LoopStatus.FAILED
                state.stop_reason = RunStopReason.MODEL_ERROR
                state.error_message = str(exc)
                state.transition(LoopTransition.FAIL)
                reply_had_run_error = True
                yield await self._run_ledger.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code=exc.reason_code,
                    ),
                )
            except ModelCallControlResolutionError:
                # The completed provider result remains owned by its stable
                # disposition candidate.  A later model call or RunEnd would
                # cross that unresolved control fact, so fail closed here.
                raise
            except Exception as exc:
                from pulsara_agent.runtime.terminal.notification import (
                    TerminalNotificationAdmissionStale,
                )

                if isinstance(
                    exc,
                    (HostIngressAdmissionStale, TerminalNotificationAdmissionStale),
                ):
                    active_run_monitor_replan_required = True
                else:
                    event = await self._run_ledger.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=f"{type(exc).__name__}: {exc}",
                            code=str(getattr(exc, "reason_code", "model_stream_error")),
                        ),
                    )
                    reply_had_run_error = True
                    yield event
            finally:
                if active_run_monitor_lease is not None:
                    self._run_model.release_active_run_monitor_safe_point(
                        active_run_monitor_lease
                    )
                if active_run_prompt_steer_lease is not None:
                    self._run_model.release_active_run_prompt_steer_safe_point(
                        active_run_prompt_steer_lease
                    )
                await self._run_model.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                    provider_input_start_bundle.prepared_candidate.preparation_ownership.preparation_id,
                    reason=(
                        "run_terminated_before_start"
                        if state.status is not LoopStatus.RUNNING
                        else "caller_cancelled_before_start"
                    ),
                )
                coordinator = self._require_active_activation_coordinator(state)
                if sys.exc_info()[0] is not None:
                    model_step_disposition = "reconciliation_required"
                elif active_run_monitor_replan_required:
                    model_step_disposition = "replan_required"
                elif state.status is not LoopStatus.RUNNING:
                    model_step_disposition = "terminal_stop"
                elif reply_had_run_error:
                    model_step_disposition = "model_error"
                else:
                    model_step_disposition = "reply_ready"
                model_step_attempt.settle(
                    coordinator,
                    disposition=model_step_disposition,
                )

            if active_run_monitor_replan_required:
                continue

            if self._apply_stop_request(state):
                break
            if state.status is not LoopStatus.RUNNING:
                break
            if reply_had_run_error:
                if accepted_control_permit is None and self._apply_stop_request(state):
                    break
                if not self._recover_or_fail_model(state):
                    break
                state.begin_next_turn()
                continue

            working_set = state.run_working_set
            control_owner = (
                working_set.model_call_control_owner
                if working_set is not None
                else None
            )
            if (
                accepted_control_permit is None
                or control_owner is None
                or not await control_owner.permit_is_active(accepted_control_permit)
            ):
                if self._apply_stop_request(state):
                    break
                raise RuntimeError("accepted model result lost its live control permit")

            assistant = self._run_ledger.replay(state.reply_id)
            state.messages.append(assistant)
            _accumulate_usage(state, assistant)
            ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "after_model_reply",
                lambda: self.memory_hooks.after_model_reply(
                    self._memory_hook_view(state), assistant
                ),
            )
            for event in hook_events:
                yield event
            if not ok:
                break

            tool_blocks = _tool_call_blocks(assistant)
            if not tool_blocks:
                if self._plan_revision_required(state):
                    if state.messages and state.messages[-1].role == "assistant":
                        state.messages.pop()
                    state.messages.append(
                        SystemMsg(
                            _PLAN_REVISION_REQUIRED_INSTRUCTION_NAME,
                            render_plan_revision_instruction(
                                str(state.plan_progress.revision_feedback)
                            ),
                            metadata={"runtime_instruction": "plan_revision_required"},
                        )
                    )
                    state.transition(LoopTransition.CONTINUE_AFTER_RECOVERY)
                    state.begin_next_turn()
                    continue
                state.status = LoopStatus.FINISHED
                state.stop_reason = RunStopReason.FINAL
                state.transition(LoopTransition.FINISH)
                break

            state.pending_tool_calls = tool_blocks
            state.transition(LoopTransition.CONTINUE_AFTER_MODEL)
            async for event in self._execute_tool_batch_attempt(state, tool_blocks):
                yield event
            if self._apply_stop_request(state):
                break
            if state.status is not LoopStatus.RUNNING:
                break

            async for event in self._after_tool_results(state):
                yield event
            if self._apply_stop_request(state):
                break
            if state.status is not LoopStatus.RUNNING:
                break
            # The complete ToolResult batch is now durable and all post-tool
            # hooks have accepted it.  Pending calls represent only in-flight
            # or suspended work; clear them before exposing the one legal
            # active-run steer safe point for the follow-up model step.
            state.pending_tool_calls = []
            async for event in self._continue_after_tool_before_followup(state):
                yield event

        if state.status is LoopStatus.WAITING_USER:
            return
        async for event in self._finalize_run(state):
            yield event

    def _apply_stop_request(self, state: RunActivationWorkingState) -> bool:
        request = state.stop_request
        if request is None:
            return False
        state.stop_request = None
        if state.status is not LoopStatus.RUNNING:
            return state.status is LoopStatus.ABORTED
        state.status = LoopStatus.ABORTED
        state.stop_reason = RunStopReason.ABORTED
        state.error_message = None
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.abort_kind = request.reason
        return True

    def _require_active_activation_coordinator(
        self,
        state: RunActivationWorkingState,
    ) -> RunActivationCoordinator:
        registry = self.run_execution_registry
        if registry is None:
            raise RuntimeError("one-step attempt requires RunExecutionRegistry")
        owner = registry.require(state.run_id)
        coordinator = owner.active_segment
        if coordinator is None:
            raise RuntimeError("one-step attempt requires an active activation")
        if (
            coordinator.state_carrier is None
            or coordinator.state_owner_token is None
            or coordinator.state_carrier.borrow(
                owner_token=coordinator.state_owner_token
            )
            is not state
        ):
            raise RuntimeError("one-step attempt working-state authority mismatch")
        return coordinator

    async def _execute_tool_batch_attempt(
        self,
        state: RunActivationWorkingState,
        tool_blocks: list[ToolCallBlock],
    ) -> AsyncIterator[AgentEvent]:
        coordinator = self._require_active_activation_coordinator(state)
        attempt = ToolBatchAttempt.install(
            coordinator,
            ordered_tool_call_ids=tuple(block.id for block in tool_blocks),
        )
        attempt.begin_dispatch(coordinator)
        failed = False
        try:
            async for event in self._execute_tool_blocks(state, tool_blocks):
                yield event
        except BaseException:
            failed = True
            raise
        finally:
            coordinator = self._require_active_activation_coordinator(state)
            if failed:
                disposition = "reconciliation_required"
            elif state.status is LoopStatus.WAITING_USER:
                disposition = "suspended"
            elif state.status is not LoopStatus.RUNNING:
                disposition = "terminalization_pending"
            else:
                disposition = "completed"
            attempt.settle(coordinator, disposition=disposition)

    async def _stream_approval_resolution(
        self,
        state: RunActivationWorkingState,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        if state.status is not LoopStatus.WAITING_USER:
            raise ValueError("approval resolution requires a waiting state")
        pending_by_id = {call.id: call for call in state.pending_tool_calls}
        if not pending_by_id:
            raise ValueError("approval resolution requires pending tool calls")
        decisions_by_id = {
            decision.tool_call_id: decision for decision in resolution.decisions
        }
        unknown_ids = set(decisions_by_id).difference(pending_by_id)
        if unknown_ids:
            raise ValueError(
                f"approval resolution referenced unknown tool calls: {sorted(unknown_ids)}"
            )
        missing_ids = set(pending_by_id).difference(decisions_by_id)
        if missing_ids:
            raise ValueError(
                f"approval resolution missing decisions for tool calls: {sorted(missing_ids)}"
            )

        confirm_results = [
            ConfirmResult(
                confirmed=decisions_by_id[call.id].confirmed,
                tool_call=call.model_copy(deep=True),
                rules=list(decisions_by_id[call.id].rules) or None,
            )
            for call in state.pending_tool_calls
        ]
        event = await self._run_ledger.emit(
            UserConfirmResultEvent(
                **self._event_context(state).event_fields(),
                confirm_results=confirm_results,
            ),
        )
        yield event

        state.status = LoopStatus.RUNNING
        state.stop_reason = None
        async for event in self._stream_confirmed_tool_blocks(state, decisions_by_id):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return

        async for event in self._after_tool_results(state):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return
        state.pending_tool_calls = []
        async for event in self._continue_after_tool_before_followup(state):
            yield event
        exposure = self._require_capability_exposure(state)
        async for event in self._stream_model_loop(state, exposure):
            yield event

    async def _stream_plan_interaction_resolution(
        self,
        state: RunActivationWorkingState,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        if state.status is not LoopStatus.WAITING_USER:
            raise ValueError("plan interaction resolution requires a waiting state")
        if state.pending_interaction_kind != "plan":
            raise ValueError(
                "waiting state does not contain a pending plan interaction"
            )
        payload = dict(state.pending_interaction_payload)
        if resolution.interaction_id != payload.get("interaction_id"):
            raise ValueError(
                "plan interaction id does not match the pending interaction"
            )
        kind = payload.get("kind")
        if kind == "question":
            if not isinstance(resolution, PlanQuestionResolution):
                raise ValueError("question interaction requires PlanQuestionResolution")
            async for event in self._resolve_plan_question(state, payload, resolution):
                yield event
        elif kind == "exit":
            if not isinstance(resolution, PlanExitResolution):
                raise ValueError("exit interaction requires PlanExitResolution")
            async for event in self._resolve_plan_exit(state, payload, resolution):
                yield event
        else:
            raise ValueError("pending plan interaction has invalid kind")

        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.pending_interaction_source_event_reference = None
        state.pending_interaction_source_event_candidate = None
        if state.status is LoopStatus.WAITING_USER:
            state.status = LoopStatus.RUNNING
            state.stop_reason = None

        async for event in self._after_tool_results(state):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return
        async for event in self._continue_after_tool_before_followup(state):
            yield event
        exposure = self._require_capability_exposure(state)
        async for event in self._stream_model_loop(state, exposure):
            yield event

    async def _stream_mcp_input_required_resolution(
        self,
        state: RunActivationWorkingState,
        resolution: PreparedMcpInputRequiredResolution,
    ) -> AsyncIterator[AgentEvent]:
        if state.status is not LoopStatus.WAITING_USER:
            raise ValueError("MCP input-required resolution requires a waiting state")
        if state.pending_interaction_kind != "mcp_input_required":
            raise ValueError("waiting state does not contain MCP input-required")
        payload = dict(state.pending_interaction_payload)
        if resolution.interaction_id != payload.get("interaction_id"):
            raise ValueError(
                "MCP input-required interaction id does not match the pending interaction"
            )

        pending_handle = payload.get("mcp_pending_handle")
        if pending_handle is None or not hasattr(
            pending_handle, "suspension_commit_view"
        ):
            raise ValueError("pending MCP interaction lost its process-local owner")
        suspension_view = pending_handle.suspension_commit_view
        suspension_fact = payload.get("suspension_fact")
        if not isinstance(suspension_fact, McpInputRequiredSuspensionFact):
            raise ValueError("pending MCP interaction lost its typed suspension fact")
        if (
            resolution.source_suspension_event_reference
            != payload.get("source_suspension_event_reference")
            or resolution.source_suspension_fact_fingerprint
            != suspension_fact.suspension_fact_fingerprint
        ):
            raise ValueError("MCP resolution source suspension drifted")
        tool_call_id = suspension_view.interaction.tool_call_id
        tool_name = suspension_view.interaction.tool_name
        original_arguments: dict[str, Any] = {}
        deadline_monotonic = suspension_view.deadline_monotonic
        timing_seed = dict(payload.get("tool_observation_timing_seed") or {})
        original_pending_payload = dict(state.pending_interaction_payload)
        rollout_reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        terminal_source = self._mcp_terminal_source(state, payload=payload)
        deadline_budget = build_runtime_event_deadline_budget(
            admitted_at_monotonic=time.monotonic(),
            total_timeout_seconds=30.0,
            terminal_reserve_seconds=10.0,
        )

        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.status = LoopStatus.RUNNING
            state.stop_reason = None
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                output="MCP input-required interaction expired before it was resumed.",
                result_state=ToolResultState.ERROR,
                tool_arguments=original_arguments,
                tool_observation_timing_seed={**timing_seed, "resumed_at": utc_now()}
                if timing_seed
                else None,
                rollout_reservation=rollout_reservation,
                mcp_input_required_terminal_source=terminal_source,
                mcp_disposition_kind="expired",
                deadline_budget=deadline_budget,
            ):
                yield event
            async for event in self._after_mcp_resume_terminal_result(
                state,
                interaction_id=resolution.interaction_id,
            ):
                yield event
            return

        gate_call = ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=original_arguments,
        )
        current_binding = self.tool_executor.registry.binding_contract(tool_name)
        recovery_rebind = getattr(
            pending_handle,
            "recovery_rebind_receipt",
            None,
        )
        recovered_binding_matches = bool(
            isinstance(current_binding, McpToolBindingContract)
            and recovery_rebind is not None
            and recovery_rebind.source_binding_identity
            == suspension_fact.binding_identity
            and recovery_rebind.effective_binding_identity
            == current_binding.binding_identity
            and recovery_rebind.source_suspension_event_reference
            == resolution.source_suspension_event_reference
            and recovery_rebind.source_suspension_fact_fingerprint
            == suspension_fact.suspension_fact_fingerprint
            and recovery_rebind.source_binding_contract_fingerprint
            == suspension_fact.durable_continuation.binding_contract_fingerprint
            and recovery_rebind.effective_binding_contract_fingerprint
            == current_binding.contract_fact_fingerprint
        )
        if isinstance(current_binding, McpToolBindingContract) and (
            current_binding.binding_identity != suspension_fact.binding_identity
            and not recovered_binding_matches
        ):
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.status = LoopStatus.RUNNING
            state.stop_reason = None
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                output=(
                    "MCP input-required resume denied because the original "
                    "binding generation changed."
                ),
                result_state=ToolResultState.ERROR,
                tool_arguments=gate_call.arguments,
                tool_observation_timing_seed=(
                    {**timing_seed, "resumed_at": utc_now()} if timing_seed else None
                ),
                rollout_reservation=rollout_reservation,
                mcp_input_required_terminal_source=terminal_source,
                mcp_disposition_kind="binding_changed",
                mcp_source_binding=suspension_fact.binding_identity,
                mcp_effective_binding=current_binding.binding_identity,
                deadline_budget=deadline_budget,
            ):
                yield event
            async for event in self._after_mcp_resume_terminal_result(
                state,
                interaction_id=resolution.interaction_id,
            ):
                yield event
            return
        exposure = self._require_capability_exposure(state)
        exposure_decision = evaluate_capability_exposure_access(gate_call, exposure)
        resume_timing_seed = (
            {**timing_seed, "resumed_at": utc_now()} if timing_seed else None
        )
        if exposure_decision is not None:
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.status = LoopStatus.RUNNING
            state.stop_reason = None
            async for event in self._emit_capability_access_denial(
                state,
                gate_call,
                exposure=exposure,
                decision=exposure_decision,
                tool_observation_timing_seed=resume_timing_seed,
                rollout_reservation=rollout_reservation,
                mcp_input_required_terminal_source=terminal_source,
                deadline_budget=deadline_budget,
            ):
                yield event
        else:
            permission_decision = await self._permission_gate_for_state(state).evaluate(
                [gate_call], exposure=exposure
            )
            if permission_decision.kind is PermissionDecisionKind.DENY:
                state.pending_interaction_kind = None
                state.pending_interaction_payload = {}
                state.status = LoopStatus.RUNNING
                state.stop_reason = None
                async for event in self._emit_permission_gate_denial(
                    state,
                    gate_call,
                    exposure=exposure,
                    decision=permission_decision,
                    tool_observation_timing_seed=resume_timing_seed,
                    rollout_reservation=rollout_reservation,
                    mcp_input_required_terminal_source=terminal_source,
                    deadline_budget=deadline_budget,
                ):
                    yield event
            elif permission_decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
                state.pending_interaction_kind = None
                state.pending_interaction_payload = {}
                state.status = LoopStatus.RUNNING
                state.stop_reason = None
                reason = "mcp_resume_permission_approval_unsupported"
                if permission_decision.reason:
                    reason = f"{reason}: {permission_decision.reason}"
                async for event in self._emit_permission_gate_denial(
                    state,
                    gate_call,
                    exposure=exposure,
                    decision=PermissionDecision(
                        kind=PermissionDecisionKind.DENY,
                        reason=reason,
                        suggested_rules=[
                            {
                                "tool": gate_call.name,
                                "reason": "mcp_resume_permission_approval_unsupported",
                            }
                        ],
                    ),
                    tool_observation_timing_seed=resume_timing_seed,
                    rollout_reservation=rollout_reservation,
                    mcp_input_required_terminal_source=terminal_source,
                    deadline_budget=deadline_budget,
                ):
                    yield event
            else:
                if not isinstance(current_binding, McpToolBindingContract):
                    state.pending_interaction_kind = None
                    state.pending_interaction_payload = {}
                    state.status = LoopStatus.RUNNING
                    state.stop_reason = None
                    async for event in self._emit_tool_result_and_record(
                        state,
                        tool_call_id=tool_call_id,
                        tool_call_name=tool_name,
                        output=f"tool {tool_name!r} cannot resume MCP input-required",
                        result_state=ToolResultState.ERROR,
                        tool_observation_timing_seed=resume_timing_seed,
                        tool_arguments=gate_call.arguments,
                        rollout_reservation=rollout_reservation,
                        mcp_input_required_terminal_source=terminal_source,
                        deadline_budget=deadline_budget,
                    ):
                        yield event
                else:
                    mcp_port = self._run_tools.mcp_tool_execution_port
                    if mcp_port is None:
                        raise RuntimeError("MCP resume lost its execution port owner")
                    descriptor = exposure.descriptors_by_name.get(tool_name)
                    timeout_ms = (
                        descriptor.timeout_ms
                        if descriptor is not None and descriptor.timeout_ms is not None
                        else 30_000
                    )
                    runtime_context = self._tool_runtime_context(state)
                    resolution_ref = (
                        terminal_source.source_resolution_submitted_event_reference
                    )
                    if resolution_ref is None:
                        raise RuntimeError(
                            "MCP resume lost its durable resolution reference"
                        )
                    commit_port = self._run_tools.event_commit_port()
                    dispatch_receipt = None
                    dispatch_generation = 0
                    dispatch_backoff_seconds = 0.01
                    dispatch_publication_failed = False
                    while (
                        dispatch_receipt is None
                        and time.monotonic()
                        < deadline_budget.ordinary_deadline_monotonic
                    ):
                        dispatch_generation += 1
                        guard = commit_port.build_mcp_dispatch_commit_guard(
                            interaction_id=resolution.interaction_id,
                            tool_call_id=tool_call_id,
                            guard_generation=dispatch_generation,
                        )
                        prepared_dispatch = mcp_port.prepare_dispatch(
                            pending_handle=pending_handle,
                            prepared_resolution=resolution,
                            source_resolution_event_reference=resolution_ref,
                            commit_guard=guard,
                        )
                        dispatch_event = McpContinuationDispatchReservedEvent(
                            id=prepared_dispatch.dispatch_event_id,
                            **self._event_context(state).event_fields(),
                            dispatch_reservation=(
                                prepared_dispatch.dispatch_reservation
                            ),
                        )
                        try:
                            dispatch_result = await commit_port.commit_mcp_continuation_dispatch_reservation(
                                dispatch_candidate=dispatch_event,
                                commit_guard=guard,
                                transaction_companion=(
                                    prepared_dispatch.transaction_companion
                                ),
                                deadline_monotonic=(
                                    deadline_budget.ordinary_deadline_monotonic
                                ),
                            )
                        except BaseException as dispatch_error:
                            dispatch_outcome = self._run_ledger.resolved_write_outcome(
                                dispatch_error
                            )
                            dispatch_receipt = mcp_port.confirm_dispatch_commit(
                                pending_handle=pending_handle,
                                prepared_dispatch=prepared_dispatch,
                                outcome=dispatch_outcome.status,
                            )
                            if dispatch_outcome.status == "none":
                                remaining = (
                                    deadline_budget.ordinary_deadline_monotonic
                                    - time.monotonic()
                                )
                                if remaining <= 0:
                                    break
                                await asyncio.sleep(
                                    min(dispatch_backoff_seconds, remaining)
                                )
                                dispatch_backoff_seconds = min(
                                    0.25, dispatch_backoff_seconds * 2
                                )
                                continue
                            if dispatch_outcome.status != "full":
                                raise
                            dispatch_stored = next(
                                event
                                for event in dispatch_outcome.committed_events
                                if event.id == dispatch_event.id
                            )
                        else:
                            dispatch_receipt = mcp_port.confirm_dispatch_commit(
                                pending_handle=pending_handle,
                                prepared_dispatch=prepared_dispatch,
                                outcome="full",
                            )
                            dispatch_stored = next(
                                event
                                for event in dispatch_result.committed_events
                                if event.id == dispatch_event.id
                            )
                            dispatch_publication_failed = (
                                bool(dispatch_result.publication_errors)
                                or dispatch_result.publication_status == "unavailable"
                            )
                        if dispatch_receipt is not None:
                            yield dispatch_stored
                    if dispatch_receipt is None:
                        rejection_payload = {
                            "error_code": McpToolRejectCode.REQUEST_TIMEOUT.value,
                            "sanitized_message": (
                                "MCP continuation dispatch could not be durably reserved"
                            ),
                            "retryable_in_same_live_owner": False,
                        }
                        outcome = McpToolRejectedOutcome(
                            outcome_kind="rejected",
                            error_code=McpToolRejectCode.REQUEST_TIMEOUT,
                            sanitized_message=rejection_payload["sanitized_message"],
                            retryable_in_same_live_owner=False,
                            outcome_fingerprint=context_fingerprint(
                                "mcp-tool-rejected-outcome:v1", rejection_payload
                            ),
                        )
                    elif dispatch_publication_failed:
                        rejection_payload = {
                            "error_code": McpToolRejectCode.PROTOCOL_ERROR.value,
                            "sanitized_message": (
                                "MCP continuation dispatch authority was not published"
                            ),
                            "retryable_in_same_live_owner": False,
                        }
                        outcome = McpToolRejectedOutcome(
                            outcome_kind="rejected",
                            error_code=McpToolRejectCode.PROTOCOL_ERROR,
                            sanitized_message=rejection_payload["sanitized_message"],
                            retryable_in_same_live_owner=False,
                            outcome_fingerprint=context_fingerprint(
                                "mcp-tool-rejected-outcome:v1", rejection_payload
                            ),
                        )
                    else:
                        outcome = await mcp_port.resume(
                            build_mcp_tool_resume_request(
                                owner=McpInvocationOwner(
                                    runtime_session_id=(
                                        runtime_context.runtime_session_id
                                    ),
                                    run_id=runtime_context.event_context.run_id,
                                    tool_call_id=tool_call_id,
                                    event_context=runtime_context.event_context,
                                ),
                                pending_handle=pending_handle,
                                binding=current_binding,
                                source_suspension_event_reference=(
                                    resolution.source_suspension_event_reference
                                ),
                                source_suspension=suspension_fact,
                                prepared_resolution=resolution,
                                dispatch_receipt=dispatch_receipt,
                                timeout_ms=timeout_ms,
                            )
                        )
                    if isinstance(outcome, McpToolRejectedOutcome) and (
                        outcome.retryable_in_same_live_owner
                    ):
                        resolution_ref = (
                            terminal_source.source_resolution_submitted_event_reference
                        )
                        if resolution_ref is None:
                            raise RuntimeError(
                                "MCP resume failure lost its resolution reference"
                            )
                        diagnostic_error = RuntimeError(outcome.sanitized_message)
                        resume_failed = McpInputRequiredResumeFailedEvent(
                            id=stable_runtime_event_id(
                                "mcp-input-required-resume-failed-event:v1",
                                resolution_ref.event_id,
                                outcome.error_code.value,
                                outcome.sanitized_message,
                            ),
                            **self._event_context(state).event_fields(),
                            resolution_submitted_event_reference=resolution_ref,
                            failure_reason="adapter_resume_error",
                            diagnostic=build_bounded_runtime_failure_diagnostic(
                                error=diagnostic_error,
                                redaction_profile_id=(
                                    "mcp_input_required_resume_error.v1"
                                ),
                                redacted_message=outcome.sanitized_message,
                            ),
                        )
                        audit_receipt = await self._run_audit.mandatory_owner.commit(
                            resume_failed,
                            deadline_budget=deadline_budget,
                            state=state,
                        )
                        if (
                            audit_receipt.status != "full"
                            or audit_receipt.committed_event_reference is None
                        ):
                            raise RuntimeError(
                                "MCP resume-failed audit requires reconciliation"
                            )
                        working_set = self._require_run_working_set(state)
                        working_set.latest_mcp_resume_failure_event_ref = (
                            audit_receipt.committed_event_reference
                        )
                        stored_resume_failed = self._run_ledger.get_event(
                            resume_failed.id
                        )
                        if not isinstance(
                            stored_resume_failed,
                            McpInputRequiredResumeFailedEvent,
                        ):
                            raise RuntimeError(
                                "MCP resume-failed audit reference cannot be rebound"
                            )
                        yield stored_resume_failed
                        if audit_receipt.publication_summary not in {
                            "completed",
                            "enqueued",
                        }:
                            state.pending_interaction_kind = "mcp_input_required"
                            state.pending_interaction_payload = original_pending_payload
                            state.status = LoopStatus.WAITING_USER
                            state.stop_reason = RunStopReason.WAITING_USER
                            finalization = self._require_run_finalization_owner(state)
                            finalization.mcp_publication_closure_reason = (
                                "resume_failed_publication_unavailable"
                            )
                            finalization.publication_deadline_budget = deadline_budget
                            closure_events: tuple[AgentEvent, ...] = ()
                            async for event in self._terminalize_pending_mcp_for_abort(
                                state,
                                reason=AbortKind.HOST_TEARDOWN,
                            ):
                                closure_events = (*closure_events, event)
                                yield event
                            if finalization.publication_latched_termination is None:
                                self._install_mcp_publication_latched_termination(
                                    state,
                                    committed_events=(
                                        stored_resume_failed,
                                        *closure_events,
                                    ),
                                    reason=(
                                        "mcp_active_interaction_publication_unavailable"
                                    ),
                                    deadline_budget=deadline_budget,
                                )
                            state.status = LoopStatus.ABORTED
                            state.stop_reason = RunStopReason.ABORTED
                            state.error_message = None
                            state.pending_tool_calls = []
                            state.pending_interaction_kind = None
                            state.pending_interaction_payload = {}
                            state.abort_kind = AbortKind.HOST_TEARDOWN
                            async for event in self._after_mcp_resume_terminal_result(
                                state,
                                interaction_id=resolution.interaction_id,
                            ):
                                yield event
                            return
                        state.pending_interaction_kind = "mcp_input_required"
                        state.pending_interaction_payload = original_pending_payload
                        state.status = LoopStatus.WAITING_USER
                        state.stop_reason = RunStopReason.WAITING_USER
                        return
                    state.pending_interaction_kind = None
                    state.pending_interaction_payload = {}
                    state.status = LoopStatus.RUNNING
                    state.stop_reason = None
                    if isinstance(outcome, McpToolSuspendedOutcome):
                        next_round = outcome.pending_handle.suspension_commit_view.interaction.round_count
                        if next_round > MAX_MCP_INPUT_REQUIRED_ROUNDS:
                            self._mcp_terminal_pending_handles[
                                (state.run_id, tool_call_id)
                            ] = outcome.pending_handle
                            async for event in self._emit_tool_result_and_record(
                                state,
                                tool_call_id=tool_call_id,
                                tool_call_name=tool_name,
                                output="MCP input-required interaction exceeded the maximum round count.",
                                result_state=ToolResultState.ERROR,
                                tool_arguments=gate_call.arguments,
                                tool_observation_timing_seed=(
                                    {**timing_seed, "resumed_at": utc_now()}
                                    if timing_seed
                                    else None
                                ),
                                rollout_reservation=rollout_reservation,
                                mcp_input_required_terminal_source=terminal_source,
                                mcp_terminal_reason=(
                                    McpPendingTerminalReason.MAXIMUM_ROUNDS_EXCEEDED
                                ),
                                deadline_budget=deadline_budget,
                            ):
                                yield event
                            async for event in self._after_mcp_resume_terminal_result(
                                state,
                                interaction_id=resolution.interaction_id,
                            ):
                                yield event
                            return
                        async for event in self._suspend_tool_execution(
                            state,
                            ToolExecutionSuspended(
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                interaction_kind="mcp_input_required",
                                mcp_pending_handle=outcome.pending_handle,
                                tool_observation_timing_seed=(
                                    freeze_json(resume_timing_seed)
                                    if resume_timing_seed is not None
                                    else None
                                ),
                            ),
                            reservation=rollout_reservation,
                        ):
                            yield event
                        return
                    if isinstance(outcome, McpToolRejectedOutcome):
                        result = ToolExecutionResult(
                            call_id=tool_call_id,
                            tool_name=tool_name,
                            status=ToolResultState.ERROR,
                            output=(
                                f"[MCP_ERROR:{outcome.error_code.value}] "
                                f"{outcome.sanitized_message}"
                            ),
                            metadata={
                                "provider_kind": "mcp",
                                "mcp_reject_code": outcome.error_code.value,
                            },
                        )
                    elif isinstance(outcome, McpToolCompletedOutcome):
                        result = ToolExecutionResult(
                            call_id=tool_call_id,
                            tool_name=tool_name,
                            status=outcome.result_state,
                            output=outcome.normalized_output,
                            metadata=outcome.normalized_metadata,
                            artifact_candidates=outcome.artifact_candidates,
                            display_payload=outcome.frozen_display_payload,
                            semantics_input=outcome.semantics_input,
                        )
                    else:
                        raise TypeError("MCP resume returned an unknown outcome")
                    async for event in self._emit_tool_result_and_record(
                        state,
                        tool_call_id=tool_call_id,
                        tool_call_name=tool_name,
                        output=result.output,
                        result_state=result.status,
                        tool_arguments=gate_call.arguments,
                        execution_result=result,
                        tool_observation_timing_seed={
                            **timing_seed,
                            "resumed_at": utc_now(),
                        }
                        if timing_seed
                        else None,
                        rollout_reservation=rollout_reservation,
                        mcp_input_required_terminal_source=terminal_source,
                        deadline_budget=deadline_budget,
                    ):
                        yield event

        async for event in self._after_mcp_resume_terminal_result(
            state,
            interaction_id=resolution.interaction_id,
        ):
            yield event

    async def _after_mcp_resume_terminal_result(
        self,
        state: RunActivationWorkingState,
        *,
        interaction_id: str,
    ) -> AsyncIterator[AgentEvent]:
        del interaction_id
        async for event in self._after_tool_results(state):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return
        async for event in self._continue_after_tool_before_followup(state):
            yield event
        exposure = self._require_capability_exposure(state)
        async for event in self._stream_model_loop(state, exposure):
            yield event

    async def _maybe_compact_mid_turn_before_followup(
        self,
        state: RunActivationWorkingState,
    ) -> MidTurnCompactionResult:
        model_visible_messages = [
            message.model_copy(deep=True) for message in state.messages
        ]
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = ()
        if state.run_model_target is not None:
            projection = await self._run_context.prepare_live_transcript_projection(
                working_set=self._require_run_working_set(state),
                budget=self.budget,
            )
            rendered = render_prepared_tool_result_units(
                prepared=projection.prepared_tool_results,
                transcript=projection.normalized_transcript.transcript,
                token_estimator=state.run_model_target.token_estimator,
            )
            lowered = lower_transcript_for_context(
                transcript=projection.normalized_transcript.transcript,
                rendered_tool_results=rendered,
                prepared_rollups=(),
            )
            protected_model_visible_messages_after = (
                *lowered.current_user_messages,
                *lowered.current_run_tail_messages,
            )
        result = await self.context_compactor.maybe_compact_before_followup(
            state=state,
            model_visible_messages=model_visible_messages,
            protected_model_visible_messages_after=(
                protected_model_visible_messages_after
            ),
        )
        if result.rewritten_messages is not None:
            state.messages = [
                message.model_copy(deep=True) for message in result.rewritten_messages
            ]
        return result

    async def _continue_after_tool_before_followup(
        self,
        state: RunActivationWorkingState,
    ) -> AsyncIterator[AgentEvent]:
        state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
        compaction_result = await self._maybe_compact_mid_turn_before_followup(state)
        for event in compaction_result.events:
            yield event
        if compaction_result.mandatory_audit_publication_failed:
            diagnostic_events = tuple(
                event
                for event in compaction_result.events
                if isinstance(event, MidTurnContextCompactionSkippedEvent)
            )
            if len(diagnostic_events) != 1:
                raise RuntimeError(
                    "mid-turn mandatory audit publication failure lacks "
                    "one committed skip event"
                )
            deadline_budget = compaction_result.mandatory_audit_deadline_budget
            if not isinstance(
                deadline_budget,
                RuntimeEventOperationDeadlineBudget,
            ):
                raise RuntimeError(
                    "mid-turn mandatory audit publication failure lost "
                    "its frozen deadline budget"
                )
            self._install_mandatory_audit_publication_latched_termination(
                state,
                committed_event=diagnostic_events[0],
                deadline_budget=deadline_budget,
            )
            return
        publication_failure = compaction_result.publication_failure
        if publication_failure is not None:
            references = tuple(
                event_reference_from_stored(
                    event,
                    runtime_session_id=self._run_identity.runtime_session_id,
                )
                for event in publication_failure.core_committed_events
                if isinstance(
                    event,
                    (
                        ContextCompactionStartedEvent,
                        ContextCompactionCompletedEvent,
                        ContextCompactionFailedEvent,
                    ),
                )
            )
            if not references or not isinstance(
                publication_failure.terminal_event,
                (
                    ContextCompactionCompletedEvent,
                    ContextCompactionFailedEvent,
                ),
            ):
                raise RuntimeError(
                    "mid-turn compaction publication failure lacks exact core refs"
                )
            termination = build_frozen_fact(
                PublicationLatchedRunTerminationFact,
                schema_version="publication_latched_run_termination.v1",
                reason="compaction_publication_unavailable",
                source_event_references=references,
                source_events_accumulator=ordered_fingerprint_accumulator(
                    "publication-latched-run-termination-sources:v1",
                    tuple(item.payload_fingerprint for item in references),
                ),
            )
            finalization = self._require_run_finalization_owner(state)
            finalization.publication_latched_termination = termination
            terminal_deadline_budget = (
                publication_failure.terminal_event_deadline_budget
            )
            if terminal_deadline_budget is None:
                raise RuntimeError(
                    "compaction publication failure lost terminal write authority"
                )
            finalization.publication_deadline_budget = terminal_deadline_budget
            state.status = LoopStatus.ABORTED
            state.stop_reason = RunStopReason.ABORTED
            state.abort_kind = AbortKind.HOST_TEARDOWN
            state.error_message = None
            return
        state.begin_next_turn()

    async def _resolve_plan_question(
        self,
        state: RunActivationWorkingState,
        payload: dict,
        resolution: PlanQuestionResolution,
    ) -> AsyncIterator[AgentEvent]:
        rollout_reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        question_id = str(payload.get("question_id") or "")
        tool_call_id = str(payload["tool_call_id"])
        tool_name = "ask_plan_question"
        yield await self._run_ledger.emit(
            PlanQuestionAnsweredEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                answer_text=resolution.answer_text,
                selected_option=resolution.selected_option,
            ),
        )
        output = json.dumps(
            {
                "answer_text": resolution.answer_text,
                "selected_option": resolution.selected_option,
            },
            ensure_ascii=False,
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=tool_call_id,
            tool_call_name=tool_name,
            output=output,
            result_state=ToolResultState.SUCCESS,
            tool_arguments=dict(payload),
            rollout_reservation=rollout_reservation,
        ):
            yield event

    async def _resolve_plan_exit(
        self,
        state: RunActivationWorkingState,
        payload: dict,
        resolution: PlanExitResolution,
    ) -> AsyncIterator[AgentEvent]:
        rollout_reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        exit_request_id = str(payload.get("exit_request_id") or "")
        tool_call_id = str(payload["tool_call_id"])
        yield await self._run_ledger.emit(
            PlanExitResolvedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=tool_call_id,
                decision=resolution.decision,
                user_feedback=resolution.user_feedback,
            ),
        )
        if resolution.decision == "revise":
            revisions = state.plan_progress.exit_revisions + 1
            state.plan_progress.exit_revisions = revisions
            if revisions > state.budget.max_plan_exit_revisions_per_run:
                yield await self._mark_plan_budget_exceeded(state, kind="exit_revision")
            else:
                state.plan_progress.revision_required = True
                state.plan_progress.revision_feedback = resolution.user_feedback
        if resolution.decision in {"approve", "cancel"}:
            plan_state = self._plan_state(state)
            event_context = self._event_context(state)
            accepted_summary = str(payload.get("summary") or "")
            accepted_plan_text = str(payload.get("plan_text") or "")
            accepted_artifact_id = None
            if resolution.decision == "approve":
                accepted_artifact_id = _accepted_plan_artifact_id(
                    event_context.run_id,
                    exit_request_id,
                )
                self._run_long_horizon.archive.put_text(
                    accepted_artifact_id,
                    accepted_plan_text,
                    session_id=self._run_identity.runtime_session_id,
                    run_id=event_context.run_id,
                    media_type="text/plain; charset=utf-8",
                    metadata={
                        "kind": "accepted_plan",
                        "exit_request_id": exit_request_id,
                        "tool_call_id": tool_call_id,
                        "summary": accepted_summary,
                    },
                )
            restored_mode = plan_state.pre_plan_permission_mode
            restored_policy = self._policy_from_plan_state(plan_state)
            restored_mode_value = parse_permission_mode(restored_mode).value
            stored_exit = await self._run_ledger.emit(
                PlanModeExitedEvent(
                    **event_context.event_fields(),
                    source="approved_exit_plan"
                    if resolution.decision == "approve"
                    else "user_cancel",
                    exit_request_id=exit_request_id,
                    restored_permission_mode=restored_mode_value,
                    restored_permission_policy=restored_policy.to_dict(),
                    accepted_plan_summary=accepted_summary
                    if resolution.decision == "approve"
                    else "",
                    accepted_plan_artifact_id=accepted_artifact_id,
                    transition_owner="agent_run",
                    host_workflow_operation_id=None,
                ),
            )
            plan_state.apply_durable_event(stored_exit)
            yield stored_exit
            _remove_plan_runtime_instructions(state)
            state.status = LoopStatus.FINISHED
            state.stop_reason = RunStopReason.FINAL
            state.transition(LoopTransition.FINISH)
        output = json.dumps(
            _plan_exit_resolution_output(resolution),
            ensure_ascii=False,
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=tool_call_id,
            tool_call_name="exit_plan",
            output=output,
            result_state=ToolResultState.SUCCESS,
            tool_arguments=dict(payload),
            rollout_reservation=rollout_reservation,
        ):
            yield event

    def _current_tool_result_batch_receipt(
        self,
        state: RunActivationWorkingState,
    ) -> CurrentToolResultBatchReceipt | None:
        spans = state.model_tool_progress.tool_result_event_spans
        if not spans:
            return None
        consumed = state.model_tool_progress.tool_result_audit_consumed_call_ids
        items: list[CurrentToolResultReceiptItem] = []
        for result_block in state.tool_results:
            if result_block.id in consumed:
                continue
            span = spans.get(result_block.id)
            if span is None:
                raise RuntimeError(
                    "current ToolResult lacks its exact durable event span"
                )
            snapshot = self._run_ledger.read_raw_range_snapshot(
                minimum_sequence=span.start_sequence,
                through_sequence=span.end_sequence,
                max_events=4_096,
                max_payload_bytes=16 * 1_024 * 1_024,
            )
            decoded = tuple(
                decode_raw_stored_event_envelope(item, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for item in snapshot.events
            )
            ends = tuple(
                item
                for item in decoded
                if isinstance(item, ToolResultEndEvent)
                and item.tool_call_id == result_block.id
            )
            projections = tuple(
                item
                for item in decoded
                if isinstance(item, ToolResultTerminalProjectionCommittedEvent)
                and item.tool_call_id == result_block.id
            )
            if len(ends) != 1 or len(projections) != 1:
                raise RuntimeError("current ToolResult durable projection is not exact")
            end = ends[0]
            projection = projections[0]
            end_ref = event_reference_from_stored(
                end,
                runtime_session_id=self._run_identity.runtime_session_id,
            )
            projection_ref = event_reference_from_stored(
                projection,
                runtime_session_id=self._run_identity.runtime_session_id,
            )
            payload = {
                "result_block": result_block.model_copy(deep=True),
                "tool_result_end_reference": end_ref,
                "terminal_projection_reference": projection_ref,
                "tool_call_id": result_block.id,
                "result_semantic_fingerprint": (
                    projection.projection_reference.semantic_join.semantic_fingerprint
                ),
            }
            items.append(
                CurrentToolResultReceiptItem(
                    **payload,
                    item_fingerprint=context_fingerprint(
                        "current-tool-result-receipt-item:v1",
                        {
                            key: (
                                value.model_dump(mode="json")
                                if hasattr(value, "model_dump")
                                else value
                            )
                            for key, value in payload.items()
                        },
                    ),
                )
            )
        if not items:
            return None
        return CurrentToolResultBatchReceipt(
            ordered_items=tuple(items),
            ordered_item_fingerprints_accumulator=ordered_fingerprint_accumulator(
                "current-tool-result-batch:v1",
                tuple(item.item_fingerprint for item in items),
            ),
        )

    async def _after_tool_results(
        self, state: RunActivationWorkingState
    ) -> AsyncIterator[AgentEvent]:
        if self._finish_child_run_after_report_result(state):
            return
        current_receipt = self._current_tool_result_batch_receipt(state)

        tool_error_count = sum(
            1
            for result in state.tool_results
            if result.state is not ToolResultState.SUCCESS
        )
        if tool_error_count:
            state.consecutive_tool_failures += tool_error_count
            state.in_run_recovery = InRunRecoveryState(
                cause=InRunRecoveryCause.TOOL_FAILURE,
                consecutive_failures=state.consecutive_tool_failures,
            )
            if (
                state.consecutive_tool_failures
                > self.budget.max_consecutive_tool_failures
            ):
                state.status = LoopStatus.FAILED
                state.stop_reason = RunStopReason.TOOL_ERROR_BUDGET
                state.error_message = "tool error budget exceeded"
                state.transition(LoopTransition.FAIL)
                return
        else:
            state.consecutive_tool_failures = 0
            state.in_run_recovery = None

        ok, hook_events = await self._run_memory_hook_and_emit_events(
            state,
            "after_tool_results",
            lambda: self.memory_hooks.after_tool_results(
                self._memory_hook_view(state), state.tool_results
            ),
        )
        for event in hook_events:
            yield event
        if not ok:
            return
        ok, should_compact, error_event = await self._run_memory_hook(
            state,
            "should_compact",
            lambda: self.memory_hooks.should_compact(self._memory_hook_view(state)),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            return
        if should_compact:
            state.compacted = True
            if current_receipt is None:
                raise RuntimeError(
                    "compaction request requires the current ToolResult receipt"
                )
            terminal_refs = tuple(
                item.tool_result_end_reference for item in current_receipt.ordered_items
            )
            request = build_frozen_fact(
                ContextCompactionRequestFact,
                schema_version="context_compaction_request.v1",
                source="memory_hook_should_compact",
                safe_point="after_tool_results",
                basis_tool_result_terminal_event_references=terminal_refs,
                basis_event_ids_accumulator=ordered_fingerprint_accumulator(
                    "context-compaction-request-basis:v1",
                    tuple(item.event_id for item in terminal_refs),
                ),
            )
            candidate = ContextCompactionRequestedEvent(
                id=stable_runtime_event_id(
                    "context-compaction-requested-event:v1",
                    state.run_id,
                    request.request_semantic_fingerprint,
                ),
                **self._event_context(state).event_fields(),
                request=request,
            )
            deadline_budget = build_runtime_event_deadline_budget(
                admitted_at_monotonic=time.monotonic(),
                total_timeout_seconds=30.0,
                terminal_reserve_seconds=10.0,
            )
            receipt = await self._run_audit.mandatory_owner.commit(
                candidate,
                deadline_budget=deadline_budget,
                state=state,
            )
            if receipt.status != "full":
                raise RuntimeError(
                    "context compaction request audit requires reconciliation"
                )
            stored = self._run_ledger.get_event(candidate.id)
            if not isinstance(stored, ContextCompactionRequestedEvent):
                raise RuntimeError("context compaction request cannot be rebound")
            yield stored
            if receipt.publication_summary not in {"completed", "enqueued"}:
                self._install_mandatory_audit_publication_latched_termination(
                    state,
                    committed_event=stored,
                    deadline_budget=deadline_budget,
                )
                return
        if current_receipt is not None:
            consumed = state.model_tool_progress.tool_result_audit_consumed_call_ids
            consumed.update(item.tool_call_id for item in current_receipt.ordered_items)

    def _finish_child_run_after_report_result(
        self, state: RunActivationWorkingState
    ) -> bool:
        if not self._is_subagent_child or self.subagent_runtime is None:
            return False
        subagent_context = self._run_identity.default_event_metadata.get("subagent")
        if not isinstance(subagent_context, dict):
            return False
        subagent_run_id = subagent_context.get("subagent_run_id")
        if not isinstance(subagent_run_id, str):
            return False
        if self.subagent_runtime.submitted_result(subagent_run_id) is None:
            return False
        state.status = LoopStatus.FINISHED
        state.stop_reason = RunStopReason.FINAL
        state.transition(LoopTransition.FINISH)
        return True

    def _require_run_finalization_owner(
        self, state: RunActivationWorkingState
    ) -> RunFinalizationOwner:
        registry = self.run_execution_registry
        if registry is None:
            raise RuntimeError("run finalization requires RunExecutionRegistry")
        run_owner = registry.get(state.run_id)
        if run_owner is None:
            raise RuntimeError("run finalization requires a committed RunOwner")
        finalization = run_owner.finalization_slot.owner
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("committed RunOwner lost its finalization owner")
        if finalization.owner_identity != run_owner.identity:
            raise RuntimeError("run finalization owner identity mismatch")
        return finalization

    async def _finalize_run(
        self,
        state: RunActivationWorkingState,
        *,
        run_session_end_hook: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        service = self._run_finalization_service
        if service is None:
            raise RuntimeError("run finalization service is not installed")
        committed = await service.finalize(
            run_id=state.run_id,
            state=state,
            operation=lambda: self._execute_run_finalization(
                state,
                run_session_end_hook=run_session_end_hook,
            ),
        )
        for event in committed:
            yield event

    async def _execute_run_finalization(
        self,
        state: RunActivationWorkingState,
        *,
        run_session_end_hook: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        if state.finalized:
            return
        await self._run_model.provider_input_generation_coordinator.settle_run_preparations(
            state.run_id,
            reason="run_terminated_before_start",
        )
        finalization = self._require_run_finalization_owner(state)
        if run_session_end_hook and not finalization.finalization_hook_done:
            _ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "on_turn_end",
                lambda: self._call_turn_end_hook(state),
            )
            for event in hook_events:
                yield event
        finalization.finalization_hook_done = True
        terminalization_kind = self._terminalization_kind(state)
        if (
            not self._is_subagent_child
            and self.subagent_runtime is not None
            and not finalization.long_horizon_child_drain_done
        ):
            await self.subagent_runtime.drain_children_for_parent_run(
                state.run_id,
                timeout_seconds=5.0,
            )
            finalization.long_horizon_child_drain_done = True
        while True:
            pending = finalization.terminal_candidates
            if pending:
                candidates = pending
                if not isinstance(candidates[-1], RunEndEvent):
                    raise RuntimeError("pending run terminal batch has invalid shape")
                candidate = candidates[-1]
            else:
                candidate = (
                    finalization.run_end_candidate
                    or self._freeze_run_end_candidate(state)
                )
                candidates = self._build_run_terminal_candidates(
                    state=state,
                    run_end=candidate,
                    terminalization_kind=terminalization_kind,
                )
            if not isinstance(candidates[0], ContextWindowClosedEvent):
                raise RuntimeError("run terminal batch must start with window close")
            expected_last_sequence = candidates[0].source_through_sequence
            finalization.terminal_candidates = candidates
            finalization.run_end_candidate = candidate
            finalization.state = "candidate_frozen"
            try:
                stored = await self._write_run_terminal_candidates(
                    state=state,
                    candidates=candidates,
                    expected_last_sequence=expected_last_sequence,
                )
            except EventWriteConflict:
                self._prepare_run_terminal_replan(state)
                await asyncio.sleep(0)
                continue
            except BaseException as exc:
                outcome = self._run_ledger.resolved_write_outcome(exc)
                if outcome.status == "unknown":
                    finalization.state = "reconciliation_required"
                    raise
                if outcome.status == "none":
                    finalization.state = "retry_wait"
                    try:
                        stored_retry = await self._write_run_terminal_candidates(
                            state=state,
                            candidates=candidates,
                            expected_last_sequence=expected_last_sequence,
                        )
                    except EventWriteConflict:
                        self._prepare_run_terminal_replan(state)
                        await asyncio.sleep(0)
                        continue
                    except BaseException as retry_error:
                        retry_outcome = self._run_ledger.resolved_write_outcome(
                            retry_error
                        )
                        if retry_outcome.status != "full":
                            raise
                        retry_confirmed = tuple(retry_outcome.committed_events)
                        if not _is_exact_run_terminal_batch(
                            retry_confirmed,
                            candidates,
                        ):
                            self._run_ledger.latch_event_commit_outcome_unknown()
                            raise RuntimeError(
                                "run terminal retry confirmation was not exact"
                            ) from retry_error
                        await self._mark_run_terminal_committed(state)
                        raise
                    if not _is_exact_run_terminal_batch(stored_retry, candidates):
                        raise RuntimeError(
                            "run terminal bounded retry returned wrong batch"
                        )
                    await self._mark_run_terminal_committed(state)
                    for event in stored_retry:
                        yield event
                    return
                confirmed = tuple(outcome.committed_events)
                if not _is_exact_run_terminal_batch(confirmed, candidates):
                    self._run_ledger.latch_event_commit_outcome_unknown()
                    raise RuntimeError(
                        "run terminal confirmation was not exact"
                    ) from exc
                await self._mark_run_terminal_committed(state)
                raise
            if not _is_exact_run_terminal_batch(stored, candidates):
                raise RuntimeError("run terminal commit returned wrong batch")
            await self._mark_run_terminal_committed(state)
            for event in stored:
                yield event
            return

    async def _write_run_terminal_candidates(
        self,
        *,
        state: RunActivationWorkingState,
        candidates: tuple[AgentEvent, ...],
        expected_last_sequence: int,
    ) -> tuple[AgentEvent, ...]:
        finalization = self._require_run_finalization_owner(state)
        termination = finalization.publication_latched_termination
        if termination is None:
            return tuple(
                await self._run_ledger.emit_many(
                    candidates,
                    expected_last_sequence=expected_last_sequence,
                )
            )
        if not isinstance(termination, PublicationLatchedRunTerminationFact):
            raise RuntimeError("publication-latched RunEnd fact is invalid")
        budget = finalization.publication_deadline_budget
        if not isinstance(budget, RuntimeEventOperationDeadlineBudget):
            raise RuntimeError(
                "publication-latched RunEnd lost its frozen deadline budget"
            )
        lease = finalization.publication_maintenance_lease
        if lease is None:
            owner_kind = (
                "compaction_publication_latched_run_termination_bundle"
                if termination.reason == "compaction_publication_unavailable"
                else (
                    "mandatory_audit_publication_latched_run_termination_bundle"
                    if termination.reason
                    == "mandatory_runtime_audit_publication_unavailable"
                    else "mcp_publication_latched_run_termination_bundle"
                )
            )
            lease = self._run_ledger.issue_publication_terminal_maintenance_lease(
                owner_kind=owner_kind,
                ordered_events=candidates,
                transaction_companion=None,
                deadline_budget=budget,
            )
            finalization.publication_maintenance_lease = lease
        finalization.state = "committing"
        result = await self._run_ledger.write_events_with_deadline(
            candidates,
            deadline_monotonic=budget.terminal_deadline_monotonic,
            expected_last_sequence=expected_last_sequence,
            publication_terminal_maintenance_lease=lease,
        )
        return tuple(result.committed_events)

    def _prepare_run_terminal_replan(self, state: RunActivationWorkingState) -> None:
        finalization = self._require_run_finalization_owner(state)
        attempt = finalization.terminal_replan_count + 1
        if attempt > 8:
            raise RuntimeError("run terminalization exceeded its replan bound")
        finalization.terminal_replan_count = attempt
        finalization.state = "retry_wait"
        finalization.terminal_candidates = ()

    def _freeze_run_end_candidate(
        self,
        state: RunActivationWorkingState,
    ) -> RunEndEvent:
        finalization = self._require_run_finalization_owner(state)
        existing = finalization.run_end_candidate
        if existing is not None:
            if (
                existing.id != finalization.terminal_event_id
                or existing.run_id != state.run_id
            ):
                raise RuntimeError("run finalization candidate identity drifted")
            return existing
        if state.stop_reason is None:
            raise RuntimeError("run terminalization requires a stop reason")
        candidate = RunEndEvent(
            id=finalization.terminal_event_id,
            **self._event_context(state).event_fields(),
            status=state.status.value,
            stop_reason=state.stop_reason,
            terminalization_kind=self._terminalization_kind(state),
            abort_kind=(
                state.abort_kind.value if state.abort_kind is not None else None
            ),
            error_message=state.error_message,
            mcp_input_required_closure_event_reference=(
                finalization.mcp_closure_event_reference
            ),
            publication_latched_termination=(
                finalization.publication_latched_termination
            ),
        )
        finalization.candidate_generation += 1
        finalization.run_end_candidate = candidate
        finalization.state = "candidate_frozen"
        finalization.commit_state = "candidate_frozen"
        registry = self.run_execution_registry
        if registry is not None:
            owner = registry.require(state.run_id)
            owner.finalization_slot.state = "active"
            owner.lifecycle = "terminalizing"
        return candidate

    @staticmethod
    def _terminalization_kind(
        state: RunActivationWorkingState,
    ) -> RunTerminalizationKind:
        if state.status is LoopStatus.FINISHED:
            return RunTerminalizationKind.NORMAL
        if state.status is LoopStatus.ABORTED:
            return (
                RunTerminalizationKind.HOST_TEARDOWN
                if state.abort_kind is AbortKind.HOST_TEARDOWN
                else RunTerminalizationKind.USER_STOP
            )
        return RunTerminalizationKind.EXECUTION_FAILURE

    async def _mark_run_terminal_committed(
        self, state: RunActivationWorkingState
    ) -> None:
        state.finalized = True
        self._latch_context_input_after_terminalization(state)
        finalization = self._require_run_finalization_owner(state)
        finalization.state = "full_output_pending"
        stored = self._run_ledger.get_event(finalization.terminal_event_id)
        if not isinstance(stored, RunEndEvent) or stored.sequence is None:
            finalization.state = "reconciliation_required"
            raise RuntimeError("confirmed run terminal batch lost its RunEnd authority")
        run_end_reference = event_reference_from_stored(
            stored,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        finalization.confirmed_run_end_event_reference = run_end_reference
        if self.run_execution_registry is not None:
            owner = self.run_execution_registry.get(state.run_id)
            if owner is not None:
                owner.finalization_owner.commit_state = "confirmed"
                owner.lifecycle = "terminal"
                owner.finalization_slot.state = "run_end_full_pending_output"
        finalization.terminal_replan_count = 0
        finalization.run_end_candidate = None
        finalization.terminal_candidates = ()
        finalization.publication_maintenance_lease = None
        materializer = self._run_ledger.final_output_materializer()
        settled = await self._attempt_run_final_output_materialization(
            state=state,
            stored_run_end=stored,
            materializer=materializer,
            run_end_reference=run_end_reference,
        )
        if settled:
            return
        service = self._run_finalization_service
        if service is None:
            raise RuntimeError("run finalization service is not installed")
        service.continue_output_materialization(
            run_id=state.run_id,
            operation=lambda: self._continue_run_final_output_materialization(
                state=state,
                stored_run_end=stored,
                materializer=materializer,
                run_end_reference=run_end_reference,
            ),
        )

    async def _continue_run_final_output_materialization(
        self,
        *,
        state: RunActivationWorkingState,
        stored_run_end: RunEndEvent,
        materializer: RunFinalOutputMaterializerPort,
        run_end_reference: ContextEventReferenceFact,
    ) -> None:
        delay_seconds = 0.01
        while True:
            await asyncio.sleep(delay_seconds)
            if await self._attempt_run_final_output_materialization(
                state=state,
                stored_run_end=stored_run_end,
                materializer=materializer,
                run_end_reference=run_end_reference,
            ):
                return
            delay_seconds = min(delay_seconds * 2.0, 0.25)

    async def _attempt_run_final_output_materialization(
        self,
        *,
        state: RunActivationWorkingState,
        stored_run_end: RunEndEvent,
        materializer: RunFinalOutputMaterializerPort,
        run_end_reference: ContextEventReferenceFact,
    ) -> bool:
        finalization = self._require_run_finalization_owner(state)
        finalization.materialization_attempt_generation += 1
        outcome = await materializer.materialize(
            owner_identity=finalization.owner_identity,
            run_end_event_reference=run_end_reference,
            deadline_monotonic=time.monotonic() + 30.0,
        )
        finalization.materialization_owner = outcome.owner
        if isinstance(outcome, RunFinalOutputMaterializationReconciliationRequired):
            finalization.state = "reconciliation_required"
            finalization.materialization_last_diagnostic_code = outcome.diagnostic_code
            if self.run_execution_registry is not None:
                owner = self.run_execution_registry.get(state.run_id)
                if owner is not None:
                    service = self._run_reconciliation_service
                    if service is None:
                        raise RuntimeError(
                            "final-output reconciliation lacks its session owner"
                        )
                    horizon = await asyncio.to_thread(
                        service.current_ledger_horizon,
                        deadline_monotonic=time.monotonic() + 30.0,
                    )
                    service.install(
                        run_id=state.run_id,
                        attempt_kind="final_output_materialization",
                        stable_candidate_id=(
                            outcome.owner.run_end_event_reference.event_id
                        ),
                        stable_candidate_fingerprint=(outcome.owner.owner_fingerprint),
                        expected_ledger_horizon=horizon,
                        repair_mode="live_resident",
                        resident_owner_generation=(owner.next_segment_generation or 1),
                    )
            raise RuntimeError(
                "run final-output authority requires reconciliation: "
                f"{outcome.diagnostic_code}"
            )
        if isinstance(outcome, RunFinalOutputMaterializationRetryableUnavailable):
            finalization.materialization_last_diagnostic_code = outcome.diagnostic_code
            return False
        if not isinstance(outcome, RunFinalOutputMaterializationFull):
            raise RuntimeError("unknown run final-output materialization outcome")
        finalization.materialization_last_diagnostic_code = None
        finalization.terminal_receipt = outcome.receipt
        finalization.state = "completed"
        if self.run_execution_registry is not None:
            owner = self.run_execution_registry.get(state.run_id)
            if owner is not None:
                self.run_execution_registry.complete_terminal_output(
                    state.run_id,
                    receipt=outcome.receipt,
                )
        return True

    def _build_run_terminal_candidates(
        self,
        *,
        state: RunActivationWorkingState,
        run_end: RunEndEvent,
        terminalization_kind: RunTerminalizationKind,
    ) -> tuple[AgentEvent, ...]:
        if state.run_working_set is None:
            raise RuntimeError("run terminalization requires committed working set")
        store = self._run_long_horizon.store
        window_state = store.window_state(state.run_id)
        if window_state is None or window_state.active_window_id is None:
            raise RuntimeError("run terminalization requires one active context window")
        window = window_state.windows[window_state.active_window_id]
        projection_state = store.projection_state(window.window_id)
        if projection_state is None:
            raise RuntimeError("run terminalization lost projection state")
        source_through_sequence = self._run_ledger.next_sequence() - 1
        event_fields = self._event_context(state).event_fields()
        window_close = ContextWindowClosedEvent(
            id=window.stable_close_event_id,
            **event_fields,
            window_id=window.window_id,
            window_generation=window.generation,
            close_reason=_context_window_terminal_reason(
                terminalization_kind=terminalization_kind,
                status=state.status,
            ),
            final_projection_generation=projection_state.projection_generation,
            final_projection_state_fingerprint=(
                projection_state.state_semantic_fingerprint
            ),
            source_through_sequence=source_through_sequence,
            next_window_id=None,
            compaction_terminal_event_id=None,
        )
        run_start = _run_start_for_id(
            self._run_long_horizon.store,
            run_id=state.run_id,
        )
        contract = run_start.long_horizon
        if run_start.child_rollout_subaccount is None:
            rollout_state = store.rollout_state(contract.rollout_account_id)
            rollout_account = store.rollout_account(contract.rollout_account_id)
            if rollout_state is None or rollout_account is None:
                raise RuntimeError("root run terminalization lost rollout account")
            if rollout_state.active_reservations:
                raise RuntimeError(
                    "root rollout account cannot close with active reservations: "
                    + ", ".join(
                        f"{item.owner_kind}:{item.owner_id}:{item.reservation_id}"
                        for item in rollout_state.active_reservations
                    )
                )
            rollout_state_at_source = advance_rollout_state(
                rollout_state,
                source_through_sequence,
            )
            _, state_before_close = apply_rollout_event(
                account=rollout_account,
                state=rollout_state_at_source,
                event=window_close.model_copy(
                    update={"sequence": source_through_sequence + 1}
                ),
            )
            assert state_before_close is not None
            rollout_close: AgentEvent = RolloutBudgetAccountClosedEvent(
                id=f"rollout_budget_account_closed:{contract.rollout_account_id}",
                **event_fields,
                account_id=contract.rollout_account_id,
                final_state_fingerprint=state_before_close.state_fingerprint,
                charged_milliunits=state_before_close.charged_milliunits,
                model_call_count=state_before_close.model_call_count,
                tool_call_count=state_before_close.tool_call_count,
                active_reservation_count=0,
                run_end_event_id=run_end.id,
            )
        else:
            subaccount = run_start.child_rollout_subaccount
            child_state = self._run_long_horizon.store.child_rollout_state(
                run_start.run_id
            )
            if child_state is None or child_state.subaccount != subaccount:
                raise RuntimeError("child rollout state is unavailable at close")
            aggregate = child_settlement_aggregate(child_state)
            rollout_close = ChildRolloutSubaccountClosedEvent(
                id=(
                    "child_rollout_subaccount_closed:"
                    f"{subaccount.subaccount_fingerprint}"
                ),
                **event_fields,
                subaccount_fingerprint=subaccount.subaccount_fingerprint,
                settlement_aggregate=aggregate,
                run_end_event_id=run_end.id,
            )
        return (window_close, rollout_close, run_end)

    def _latch_context_input_after_terminalization(
        self, state: RunActivationWorkingState
    ) -> None:
        finalization = self._require_run_finalization_owner(state)
        if finalization.context_input_latch_after_terminalization:
            finalization.context_input_latch_after_terminalization = False
            self._run_ledger.latch_context_input_reconciliation_required()

    def _run_result(self, state: RunActivationWorkingState) -> AgentRunResult:
        finalization = (
            self._require_run_finalization_owner(state)
            if self.run_execution_registry is not None
            and self.run_execution_registry.get(state.run_id) is not None
            else None
        )
        receipt = (
            finalization.terminal_receipt
            if finalization is not None and state.finalized
            else None
        )
        output = getattr(receipt, "output", None)
        status = state.status
        stop_reason = state.stop_reason
        final_text = _final_text(state.messages)
        usage = state.token_usage.model_copy(deep=True)
        if output is not None:
            status = {
                "finished": LoopStatus.FINISHED,
                "failed": LoopStatus.FAILED,
                "aborted": LoopStatus.ABORTED,
            }[output.status]
            stop_reason = output.stop_reason
            final_text = output.final_text or ""
            usage = Usage(
                input_tokens=output.usage.input_tokens,
                output_tokens=output.usage.output_tokens,
                total_tokens=output.usage.total_tokens,
            )
        return AgentRunResult(
            status=status,
            stop_reason=stop_reason,
            run_id=state.run_id,
            messages=[message.model_copy(deep=True) for message in state.messages],
            final_text=final_text,
            token_usage=usage,
            tool_call_count=state.tool_call_count,
            pending_interaction_kind=state.pending_interaction_kind,
            finalized=state.finalized,
            error_message=state.error_message,
        )

    async def _run_memory_hook(
        self, state: RunActivationWorkingState, hook_name: str, call
    ):
        try:
            return True, await call(), None
        except Exception as exc:
            event = await self._mark_memory_hook_failed(state, hook_name, exc)
            return False, None, event

    async def _call_turn_start_hook(
        self, state: RunActivationWorkingState, user_input: str
    ):
        view = self._memory_hook_view(state)
        hook = getattr(self.memory_hooks, "on_turn_start", None)
        if hook is not None and _is_overridden_hook(
            self.memory_hooks, "on_turn_start", NoopMemoryHooks
        ):
            return await hook(view, user_input)
        return await self.memory_hooks.on_session_start(view, user_input)

    async def _call_turn_end_hook(self, state: RunActivationWorkingState):
        view = self._memory_hook_view(state)
        hook = getattr(self.memory_hooks, "on_turn_end", None)
        if hook is not None and _is_overridden_hook(
            self.memory_hooks, "on_turn_end", NoopMemoryHooks
        ):
            return await hook(view)
        return await self.memory_hooks.on_session_end(view)

    def _memory_hook_view(self, state: RunActivationWorkingState) -> MemoryHookRunView:
        model_step_index = (
            state.model_tool_progress.current_model_call_index
            or state.model_tool_progress.model_call_index + 1
        )
        return build_memory_hook_run_view(
            runtime_session_id=state.session_id,
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            status=state.status.value,
            messages=state.messages,
            usage=state.token_usage,
            current_projection=state.memory_projection,
            model_step_key=f"{state.run_id}:{model_step_index}",
        )

    async def _run_memory_hook_and_emit_events(
        self,
        state: RunActivationWorkingState,
        hook_name: str,
        call,
    ) -> tuple[bool, list[AgentEvent]]:
        ok, produced_events, error_event = await self._run_memory_hook(
            state, hook_name, call
        )
        if not ok:
            assert error_event is not None
            return False, [error_event]
        emitted_events: list[AgentEvent] = []
        try:
            for event in produced_events or ():
                emitted_events.append(await self._run_ledger.emit(event))
        except Exception as exc:
            emitted_events.append(
                await self._mark_memory_hook_failed(state, hook_name, exc)
            )
            return False, emitted_events
        return True, emitted_events

    async def _mark_memory_hook_failed(
        self, state: RunActivationWorkingState, hook_name: str, exc: Exception
    ) -> AgentEvent:
        message = f"memory hook {hook_name} failed: {type(exc).__name__}: {exc}"
        state.status = LoopStatus.FAILED
        state.stop_reason = RunStopReason.MEMORY_HOOK_ERROR
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self._run_ledger.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="memory_hook_error",
                metadata={"hook": hook_name},
            ),
        )

    async def _project_memory(
        self, state: RunActivationWorkingState
    ) -> AsyncIterator[AgentEvent]:
        projection_id = f"projection:{state.turn_id}"
        context = self._event_context(state)
        yield await self._run_ledger.emit(
            ProjectionRequestedEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
            ),
        )
        baseline = None
        view = self._memory_hook_view(state)
        try:
            baseline = self.memory_hooks.baseline_projection(
                view,
                token_budget=self.budget.projection_token_budget,
            )
            projection = await asyncio.wait_for(
                self.memory_hooks.project(
                    view,
                    token_budget=self.budget.projection_token_budget,
                ),
                timeout=self.budget.recall_hard_timeout_ms / 1000,
            )
        except TimeoutError:
            state.memory_projection = baseline
            if baseline is not None:
                yield await self._run_ledger.emit(
                    ProjectionReadyEvent(
                        **context.event_fields(),
                        projection_id=projection_id,
                        role=self.model_role.value,
                        scope=state.current_scope or "session",
                        token_budget=self.budget.projection_token_budget,
                        projection_kind=_memory_projection_kind(baseline),
                        included_memory_ids=_projection_ids(baseline),
                        recalled_memory_entries=_typed_recalled_memory_entries(
                            baseline
                        ),
                        summary=_projection_summary(baseline),
                        metadata={
                            "degraded": True,
                            "warnings": ["semantic_recall_timeout"],
                            "fallback": "baseline_projection",
                        },
                    ),
                )
                return
            yield await self._run_ledger.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error="recall_timeout",
                ),
            )
            return
        except Exception as exc:
            state.memory_projection = None
            yield await self._run_ledger.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
            return
        state.memory_projection = projection
        yield await self._run_ledger.emit(
            ProjectionReadyEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
                projection_kind=_memory_projection_kind(projection),
                included_memory_ids=_projection_ids(projection),
                recalled_memory_entries=_typed_recalled_memory_entries(projection),
                summary=_projection_summary(projection),
            ),
        )

    async def _execute_tool_blocks(
        self,
        state: RunActivationWorkingState,
        tool_blocks: list[ToolCallBlock],
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []
        for block in tool_blocks:
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                stored_events = await self._run_ledger.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                        failure_stage="malformed_arguments",
                    ),
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    _tool_result_message_from_events(
                        stored_events, block.name, result_block
                    )
                )

        if not parsed_calls:
            return

        duplicate_ids = _duplicate_tool_call_ids(parsed_calls)
        if duplicate_ids:
            unique_calls = [
                call for call in parsed_calls if call.id not in duplicate_ids
            ]
            for duplicate_id in sorted(duplicate_ids):
                call = next(call for call in parsed_calls if call.id == duplicate_id)
                stored_events = await self._run_ledger.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=f"Duplicate tool_call_id in assistant reply: {call.id}",
                        arguments=call.arguments,
                        failure_stage="policy_denied",
                    ),
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, call.id)
                _remember_tool_result_event_span(state, stored_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    _tool_result_message_from_events(
                        stored_events, call.name, result_block
                    )
                )
            parsed_calls = unique_calls
            if not parsed_calls:
                return

        exposure = self._require_capability_exposure(state)
        executable_calls: list[ToolCall] = []
        async for event_or_calls in self._stream_capability_access_filtered_calls(
            state,
            parsed_calls,
            exposure=exposure,
        ):
            if isinstance(event_or_calls, tuple):
                executable_calls = event_or_calls[0]
            else:
                yield event_or_calls

        if not executable_calls:
            return

        if any(call.name in PLAN_WORKFLOW_TOOL_NAMES for call in executable_calls):
            async for event in self._handle_workflow_tool_batch(
                state, executable_calls
            ):
                yield event
            return

        permission_executable_calls: list[ToolCall] = []
        local_permission_decisions: dict[str, PermissionDecision] = {}
        for call in executable_calls:
            local_permission_decision = self._permission_gate_for_state(
                state
            ).evaluate_local_capability_call(
                call,
                exposure=exposure,
            )
            if local_permission_decision.kind is PermissionDecisionKind.DENY:
                async for event in self._emit_permission_gate_denial(
                    state,
                    call,
                    exposure=exposure,
                    decision=local_permission_decision,
                ):
                    yield event
                continue
            local_permission_decisions[call.id] = local_permission_decision
            permission_executable_calls.append(call)
        executable_calls = permission_executable_calls
        if not executable_calls:
            return

        decision = await self._permission_gate_for_state(state).evaluate(
            executable_calls, exposure=exposure
        )
        if decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
            for call in executable_calls:
                local_decision = local_permission_decisions.get(call.id)
                if (
                    local_decision is not None
                    and local_decision.kind is PermissionDecisionKind.WAIT_FOR_USER
                ):
                    fact_decision = local_decision
                    reason_code_override = "permission_wait_for_user"
                elif any(
                    item.kind is PermissionDecisionKind.WAIT_FOR_USER
                    for item in local_permission_decisions.values()
                ):
                    fact_decision = PermissionDecision(
                        kind=PermissionDecisionKind.WAIT_FOR_USER,
                        reason=decision.reason,
                    )
                    reason_code_override = "permission_wait_for_user_batch_suspension"
                else:
                    fact_decision = decision
                    reason_code_override = (
                        "permission_wait_for_user"
                        if _call_matches_suggested_rule(call, decision.suggested_rules)
                        or len(executable_calls) == 1
                        else "permission_wait_for_user_batch_suspension"
                    )
                fact = self._capability_gate_decision_fact(
                    state,
                    call,
                    exposure=exposure,
                    decision=fact_decision,
                    reason_code_override=reason_code_override,
                )
                async for event in self._emit_capability_gate_decision(state, fact):
                    yield event
            blocks = [
                ToolCallBlock(
                    id=call.id,
                    name=call.name,
                    input=json.dumps(call.arguments),
                    state=ToolCallState.ASKING,
                    suggested_rules=(
                        local_permission_decisions[call.id].suggested_rules
                        if local_permission_decisions.get(call.id) is not None
                        and local_permission_decisions[call.id].kind
                        is PermissionDecisionKind.WAIT_FOR_USER
                        else decision.suggested_rules
                    ),
                )
                for call in executable_calls
            ]
            state.pending_tool_calls = blocks
            state.status = LoopStatus.WAITING_USER
            state.stop_reason = RunStopReason.WAITING_USER
            state.transition(LoopTransition.WAIT_FOR_USER)
            event = await self._run_ledger.emit(
                RequireUserConfirmEvent(
                    **self._event_context(state).event_fields(), tool_calls=blocks
                ),
            )
            state.pending_interaction_payload = {}
            state.pending_interaction_source_event_reference = (
                event_reference_from_stored(
                    event,
                    runtime_session_id=self._run_identity.runtime_session_id,
                )
            )
            state.pending_interaction_source_event_candidate = (
                freeze_event_write_candidate(
                    event.model_copy(update={"sequence": None})
                )
            )
            yield event
            return
        if decision.kind is PermissionDecisionKind.DENY:
            for call in executable_calls:
                async for event in self._emit_permission_gate_denial(
                    state,
                    call,
                    exposure=exposure,
                    decision=decision,
                ):
                    yield event
            return

        async for event in self._stream_parsed_tool_calls(state, executable_calls):
            yield event

    async def _emit_workflow_gate_decisions(
        self,
        state: RunActivationWorkingState,
        parsed_calls: list[ToolCall],
        *,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent]:
        workflow_index = next(
            index
            for index, call in enumerate(parsed_calls)
            if call.name in PLAN_WORKFLOW_TOOL_NAMES
        )
        workflow_call = parsed_calls[workflow_index]
        for index, call in enumerate(parsed_calls):
            if index == workflow_index:
                continue
            suppress_fact = self._capability_gate_decision_fact(
                state,
                call,
                exposure=exposure,
                decision=_suppressed_by_workflow_control_decision(workflow_call),
                result_state=ToolResultState.DENIED,
            )
            async for event in self._emit_capability_gate_decision(
                state, suppress_fact
            ):
                yield event

    async def _handle_workflow_tool_batch(
        self,
        state: RunActivationWorkingState,
        parsed_calls: list[ToolCall],
    ) -> AsyncIterator[AgentEvent]:
        workflow_index = next(
            index
            for index, call in enumerate(parsed_calls)
            if call.name in PLAN_WORKFLOW_TOOL_NAMES
        )
        workflow_call = parsed_calls[workflow_index]
        exposure = self._require_capability_exposure(state)
        (
            stored_admissions,
            executable_workflow_calls,
            reservations,
        ) = await self._commit_tool_admissions(
            state,
            [workflow_call],
            exposure=exposure,
        )
        for event in stored_admissions:
            yield event
        async for event in self._emit_workflow_gate_decisions(
            state,
            parsed_calls,
            exposure=exposure,
        ):
            yield event
        if not executable_workflow_calls:
            self._record_tool_result_events(
                state,
                stored_events=list(stored_admissions),
                tool_call_id=workflow_call.id,
                tool_call_name=workflow_call.name,
            )
            for index, call in enumerate(parsed_calls):
                if index == workflow_index:
                    continue
                async for event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=call.id,
                    tool_call_name=call.name,
                    output=(
                        "not executed because a plan workflow control tool was "
                        "denied by the rollout phase"
                    ),
                    result_state=ToolResultState.DENIED,
                    tool_arguments=call.arguments,
                    failure_stage="workflow_short_circuit",
                ):
                    yield event
            return
        rollout_reservation = reservations[workflow_call.id]
        try:
            if workflow_call.name == "enter_plan":
                async for event in self._execute_enter_plan(
                    state,
                    workflow_call,
                    rollout_reservation=rollout_reservation,
                ):
                    yield event
            elif workflow_call.name == "ask_plan_question":
                async for event in self._execute_ask_plan_question(
                    state,
                    workflow_call,
                    rollout_reservation=rollout_reservation,
                ):
                    yield event
            elif workflow_call.name == "exit_plan":
                async for event in self._execute_exit_plan(
                    state,
                    workflow_call,
                    rollout_reservation=rollout_reservation,
                ):
                    yield event
            else:
                async for event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=workflow_call.id,
                    tool_call_name=workflow_call.name,
                    output=f"unknown workflow tool: {workflow_call.name}",
                    result_state=ToolResultState.ERROR,
                    tool_arguments=workflow_call.arguments,
                    rollout_reservation=rollout_reservation,
                ):
                    yield event
        except Exception as exc:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=workflow_call.id,
                tool_call_name=workflow_call.name,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
                result_state=ToolResultState.ERROR,
                tool_arguments=workflow_call.arguments,
                rollout_reservation=rollout_reservation,
            ):
                yield event

        for index, call in enumerate(parsed_calls):
            if index == workflow_index:
                continue
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output=(
                    "not executed because a plan workflow control tool suspended or changed workflow state; "
                    "retry after the workflow step completes"
                ),
                result_state=ToolResultState.DENIED,
                tool_arguments=call.arguments,
                failure_stage="workflow_short_circuit",
            ):
                yield event

    async def _execute_enter_plan(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        rollout_reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        plan_state = self._plan_state(state)
        if plan_state.active:
            output = json.dumps({"status": "already_active"}, ensure_ascii=False)
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output=output,
                result_state=ToolResultState.SUCCESS,
                tool_arguments=call.arguments,
                rollout_reservation=rollout_reservation,
            ):
                yield event
            return
        reason = _optional_str(call.arguments.get("reason"))
        previous_mode = self.permission_mode
        previous_policy = self.permission_policy
        if previous_mode is None:
            raise ValueError(
                "enter_plan requires a preset session default permission mode"
            )
        plan_state.begin(
            source="agent",
            previous_mode=previous_mode,
            previous_policy=previous_policy,
            reason=reason,
            pending_entry_audit=False,
        )
        stored_entered = await self._run_ledger.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="agent",
                previous_permission_mode=previous_mode.value,
                previous_permission_policy=previous_policy.to_dict(),
                reason=reason,
            ),
        )
        plan_state.apply_durable_event(stored_entered)
        yield stored_entered
        output = json.dumps(
            {"status": "entered", "permission_mode": PermissionMode.READ_ONLY.value},
            ensure_ascii=False,
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=output,
            result_state=ToolResultState.SUCCESS,
            tool_arguments=call.arguments,
            rollout_reservation=rollout_reservation,
        ):
            yield event
        state.status = LoopStatus.FINISHED
        state.stop_reason = RunStopReason.FINAL
        state.transition(LoopTransition.FINISH)

    async def _execute_ask_plan_question(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        rollout_reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="ask_plan_question can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
                tool_arguments=call.arguments,
                failure_stage="workflow_state_denied",
                rollout_reservation=rollout_reservation,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(
                state,
                call,
                kind="interaction",
                rollout_reservation=rollout_reservation,
            ):
                yield event
            return
        question = _required_str(call.arguments.get("question"), "question")
        options = normalize_plan_question_options(call.arguments.get("options") or ())
        option_payload = [option.model_dump() for option in options]
        allow_free_text = bool(call.arguments.get("allow_free_text", True))
        reason = _optional_str(call.arguments.get("reason"))
        question_id = f"plan_question:{uuid4().hex}"
        interaction_id = f"plan_interaction:{uuid4().hex}"
        stored_question = await self._run_ledger.emit(
            PlanQuestionAskedEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                tool_call_id=call.id,
                question=question,
                options=option_payload,
                allow_free_text=allow_free_text,
                reason=reason,
            ),
        )
        yield stored_question
        state.pending_tool_calls = []
        state.pending_interaction_kind = "plan"
        state.pending_interaction_payload = {
            "interaction_id": interaction_id,
            "kind": "question",
            "tool_call_id": call.id,
            "question_id": question_id,
            "question": question,
            "options": option_payload,
            "allow_free_text": allow_free_text,
            "rollout_reservation_id": rollout_reservation.reservation_id,
            "rollout_reservation_fingerprint": (
                rollout_reservation.semantic_fingerprint
            ),
        }
        state.pending_interaction_source_event_reference = event_reference_from_stored(
            stored_question,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        state.pending_interaction_source_event_candidate = freeze_event_write_candidate(
            stored_question.model_copy(update={"sequence": None})
        )
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _execute_exit_plan(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        rollout_reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="exit_plan can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
                tool_arguments=call.arguments,
                failure_stage="workflow_state_denied",
                rollout_reservation=rollout_reservation,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(
                state,
                call,
                kind="interaction",
                rollout_reservation=rollout_reservation,
            ):
                yield event
            return
        plan_text = _required_str(call.arguments.get("plan"), "plan")
        summary = _optional_str(call.arguments.get("summary"))
        state.plan_progress.revision_required = False
        state.plan_progress.revision_feedback = ""
        exit_request_id = f"plan_exit:{uuid4().hex}"
        interaction_id = f"plan_interaction:{uuid4().hex}"
        stored_exit_request = await self._run_ledger.emit(
            PlanExitRequestedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=call.id,
                plan_text=plan_text,
                summary=summary,
            ),
        )
        yield stored_exit_request
        state.pending_tool_calls = []
        state.pending_interaction_kind = "plan"
        state.pending_interaction_payload = {
            "interaction_id": interaction_id,
            "kind": "exit",
            "tool_call_id": call.id,
            "exit_request_id": exit_request_id,
            "plan_text": plan_text,
            "summary": summary,
            "rollout_reservation_id": rollout_reservation.reservation_id,
            "rollout_reservation_fingerprint": (
                rollout_reservation.semantic_fingerprint
            ),
        }
        state.pending_interaction_source_event_reference = event_reference_from_stored(
            stored_exit_request,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        state.pending_interaction_source_event_candidate = freeze_event_write_candidate(
            stored_exit_request.model_copy(update={"sequence": None})
        )
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)

    def _mcp_terminal_source(
        self,
        state: RunActivationWorkingState,
        *,
        payload: dict[str, Any],
    ) -> McpInputRequiredTerminalSourceFact:
        suspension_ref = payload.get("source_suspension_event_reference")
        if not isinstance(suspension_ref, ContextEventReferenceFact):
            raise RuntimeError("MCP terminal transition lost its suspension reference")
        working_set = self._require_run_working_set(state)
        resolution_ref = working_set.latest_mcp_input_required_resolution_ref
        if not isinstance(resolution_ref, ContextEventReferenceFact):
            raise RuntimeError("MCP terminal transition lost its resolution reference")
        return build_frozen_fact(
            McpInputRequiredTerminalSourceFact,
            schema_version="mcp_input_required_terminal_source.v1",
            source_suspension_event_reference=suspension_ref,
            source_resolution_submitted_event_reference=resolution_ref,
        )

    def _attach_mcp_terminal_disposition(
        self,
        state: RunActivationWorkingState,
        *,
        prepared_candidates: tuple[AgentEvent, ...],
        source: McpInputRequiredTerminalSourceFact | None,
        disposition_kind: Literal["expired", "binding_changed"] | None,
        closure_reason: Literal[
            "suspension_publication_unavailable",
            "resume_boundary_publication_unavailable",
            "resume_failed_publication_unavailable",
            "session_reopen_lease_unavailable",
            "child_pending_unsupported",
            "live_pending_lease_unavailable",
        ]
        | None = None,
        source_binding: McpBindingIdentityFact | None = None,
        effective_binding: McpBindingIdentityFact | None = None,
    ) -> tuple[AgentEvent, ...]:
        if disposition_kind is not None and closure_reason is not None:
            raise ValueError("MCP disposition and closure are mutually exclusive")
        if source is None:
            if disposition_kind is not None or closure_reason is not None:
                raise ValueError("MCP terminal companion requires a terminal source")
            return prepared_candidates
        terminal_events = tuple(
            event
            for event in prepared_candidates
            if isinstance(event, ToolResultEndEvent)
        )
        if len(terminal_events) != 1:
            raise ValueError("MCP terminal batch requires one prepared ToolResultEnd")
        terminal = terminal_events[0]
        if disposition_kind is None and closure_reason is None:
            return prepared_candidates
        resolution_ref = source.source_resolution_submitted_event_reference
        terminal_identity = stable_event_identity(
            terminal,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        event_fields = self._event_context(state).event_fields()
        if closure_reason is not None:
            working_set = self._require_run_working_set(state)
            resume_failed_ref = (
                working_set.latest_mcp_resume_failure_event_ref
                if closure_reason
                in {
                    "resume_failed_publication_unavailable",
                    "session_reopen_lease_unavailable",
                }
                else None
            )
            companion = McpInputRequiredInteractionClosedEvent(
                id=stable_runtime_event_id(
                    "mcp-input-required-interaction-closed-event:v1",
                    source.source_suspension_event_reference.event_id,
                    resolution_ref.event_id if resolution_ref is not None else None,
                    (
                        resume_failed_ref.event_id
                        if resume_failed_ref is not None
                        else None
                    ),
                    closure_reason,
                    terminal.id,
                ),
                **event_fields,
                source_suspension_event_reference=(
                    source.source_suspension_event_reference
                ),
                source_resolution_submitted_event_reference=resolution_ref,
                source_resume_failed_event_reference=resume_failed_ref,
                closure_reason=closure_reason,
                terminal_tool_result_event_identity=terminal_identity,
            )
            output = list(prepared_candidates)
            output.insert(output.index(terminal) + 1, companion)
            return tuple(output)
        if resolution_ref is None:
            raise ValueError("MCP terminal disposition requires a resolution reference")
        if disposition_kind == "expired":
            companion: AgentEvent = McpInputRequiredExpiredEvent(
                id=stable_runtime_event_id(
                    "mcp-input-required-expired-event:v1",
                    resolution_ref.event_id,
                    terminal.id,
                ),
                **event_fields,
                resolution_submitted_event_reference=resolution_ref,
                terminal_tool_result_event_identity=terminal_identity,
            )
        else:
            if source_binding is None or effective_binding is None:
                raise ValueError(
                    "MCP binding-changed disposition requires both binding identities"
                )
            companion = McpInputRequiredBindingChangedEvent(
                id=stable_runtime_event_id(
                    "mcp-input-required-binding-changed-event:v1",
                    resolution_ref.event_id,
                    terminal.id,
                ),
                **event_fields,
                resolution_submitted_event_reference=resolution_ref,
                terminal_tool_result_event_identity=terminal_identity,
                source_binding=source_binding,
                effective_binding=effective_binding,
            )
        output = list(prepared_candidates)
        output.insert(output.index(terminal) + 1, companion)
        return tuple(output)

    def _install_mcp_publication_latched_termination(
        self,
        state: RunActivationWorkingState,
        *,
        committed_events: tuple[AgentEvent, ...],
        reason: Literal[
            "mcp_active_interaction_publication_unavailable",
            "mcp_terminal_disposition_publication_unavailable",
            "mcp_closure_publication_unavailable",
        ],
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> None:
        references = tuple(
            event_reference_from_stored(
                event,
                runtime_session_id=self._run_identity.runtime_session_id,
            )
            for event in sorted(
                (
                    event
                    for event in committed_events
                    if isinstance(
                        event,
                        (
                            ToolExecutionSuspendedEvent,
                            McpInputRequiredResumeFailedEvent,
                            McpInputRequiredExpiredEvent,
                            McpInputRequiredBindingChangedEvent,
                            McpInputRequiredInteractionClosedEvent,
                            ToolResultEndEvent,
                        ),
                    )
                ),
                key=lambda event: event.sequence or 0,
            )
        )
        if not references or any(reference.sequence <= 0 for reference in references):
            raise RuntimeError(
                "MCP publication-latched termination lacks stored source authority"
            )
        termination = build_frozen_fact(
            PublicationLatchedRunTerminationFact,
            schema_version="publication_latched_run_termination.v1",
            reason=reason,
            source_event_references=references,
            source_events_accumulator=ordered_fingerprint_accumulator(
                "publication-latched-run-termination-sources:v1",
                tuple(item.payload_fingerprint for item in references),
            ),
        )
        finalization = self._require_run_finalization_owner(state)
        finalization.publication_latched_termination = termination
        finalization.publication_deadline_budget = deadline_budget

    def _install_mandatory_audit_publication_latched_termination(
        self,
        state: RunActivationWorkingState,
        *,
        committed_event: AgentEvent,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> None:
        if (
            committed_event.sequence is None
            or not self._run_ledger.publication_reconciliation_required
        ):
            raise RuntimeError(
                "mandatory audit publication termination lacks committed authority"
            )
        reference = event_reference_from_stored(
            committed_event,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        termination = build_frozen_fact(
            PublicationLatchedRunTerminationFact,
            schema_version="publication_latched_run_termination.v1",
            reason="mandatory_runtime_audit_publication_unavailable",
            source_event_references=(reference,),
            source_events_accumulator=ordered_fingerprint_accumulator(
                "publication-latched-run-termination-sources:v1",
                (reference.payload_fingerprint,),
            ),
        )
        finalization = self._require_run_finalization_owner(state)
        finalization.publication_latched_termination = termination
        finalization.publication_deadline_budget = deadline_budget
        state.status = LoopStatus.ABORTED
        state.stop_reason = RunStopReason.ABORTED
        state.abort_kind = AbortKind.HOST_TEARDOWN
        state.error_message = None
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}

    async def _emit_tool_result_and_record(
        self,
        state: RunActivationWorkingState,
        *,
        tool_call_id: str,
        tool_call_name: str,
        output: str,
        result_state: ToolResultState,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        tool_arguments: dict[str, Any] | None = None,
        failure_stage: str | None = None,
        execution_result: ToolExecutionResult | None = None,
        rollout_reservation: RolloutReservationFact | None = None,
        mcp_input_required_terminal_source: (
            McpInputRequiredTerminalSourceFact | None
        ) = None,
        mcp_disposition_kind: Literal["expired", "binding_changed"] | None = None,
        mcp_closure_reason: Literal[
            "suspension_publication_unavailable",
            "resume_boundary_publication_unavailable",
            "resume_failed_publication_unavailable",
            "session_reopen_lease_unavailable",
            "child_pending_unsupported",
            "live_pending_lease_unavailable",
        ]
        | None = None,
        mcp_source_binding: McpBindingIdentityFact | None = None,
        mcp_effective_binding: McpBindingIdentityFact | None = None,
        mcp_terminal_reason: McpPendingTerminalReason | None = None,
        deadline_budget: RuntimeEventOperationDeadlineBudget | None = None,
    ) -> AsyncIterator[AgentEvent]:
        prior_result_events = self._run_tools.tool_result_boundary_events(
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            start_event_id=_tool_timing_start_event_id(tool_observation_timing_seed),
        )
        prior_starts = [
            event
            for event in prior_result_events
            if isinstance(event, ToolResultStartEvent)
        ]
        prior_ends = [
            event
            for event in prior_result_events
            if isinstance(event, ToolResultEndEvent)
        ]
        if len(prior_starts) > 1 or len(prior_ends) > 1:
            raise RuntimeError("tool-result ledger contains duplicate boundaries")
        existing_start = prior_starts[0] if prior_starts and not prior_ends else None
        exposure = self._require_capability_exposure(state)
        descriptor = exposure.descriptors_by_name.get(tool_call_name)
        semantics = None
        semantics_factory = None
        if descriptor is not None:
            arguments = dict(tool_arguments or {})
            frozen_arguments = freeze_json(arguments)
            if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                raise AssertionError("tool arguments must freeze as an object")
            attribution = self._descriptor_render_attribution(state, descriptor)
            if (
                failure_stage is not None
                and result_state is not ToolResultState.INTERRUPTED
            ):
                timing_variant = (
                    descriptor.result_render_contract.pre_execution_denial_variant_code
                )

                def semantics_factory(timing):
                    return build_pre_execution_denial_semantics(
                        descriptor=descriptor,
                        descriptor_attribution=attribution,
                        requested_arguments=frozen_arguments,
                        message=output,
                        result_state=ToolResultStateFact(result_state.value),
                        reason_code=failure_stage,
                        failure_stage=_pre_execution_failure_stage(failure_stage),
                        capture_policy=self.tool_executor.essential_capture_policy,
                        registry=self.tool_executor.semantics_registry,
                        observation_timing=timing,
                    )
            else:
                runtime_result = execution_result or ToolExecutionResult(
                    call_id=tool_call_id,
                    tool_name=tool_call_name,
                    status=result_state,
                    output=output,
                )
                if (
                    runtime_result.call_id != tool_call_id
                    or runtime_result.tool_name != tool_call_name
                    or runtime_result.status is not result_state
                    or runtime_result.output != output
                ):
                    raise ValueError("typed synthetic tool result identity mismatch")
                call = ToolCall(
                    id=tool_call_id,
                    name=tool_call_name,
                    arguments=arguments,
                )
                timing_variant = (
                    runtime_result.semantics_input.semantics_input_kind
                    if runtime_result.semantics_input is not None
                    else ToolResultRenderVariantCode.GENERIC_RESULT
                )

                def semantics_factory(timing):
                    return build_execution_semantics(
                        descriptor=descriptor,
                        descriptor_attribution=attribution,
                        call=call,
                        result=runtime_result,
                        observation_timing=timing,
                        capture_policy=self.tool_executor.essential_capture_policy,
                        registry=self.tool_executor.semantics_registry,
                    )

            tool_observation_timing_seed = {
                **(tool_observation_timing_seed or {}),
                "tool_origin": tool_origin_for_descriptor_variant(
                    descriptor,
                    timing_variant,
                ),
            }
        else:
            semantics = build_unknown_result_semantics(
                result_state=ToolResultStateFact(result_state.value)
            )
        candidates = tuple(
            build_tool_result_error_events(
                self._event_context(state),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                message=output,
                state=result_state,
                tool_observation_timing_seed=tool_observation_timing_seed,
                existing_start=existing_start,
                semantics=semantics,
                semantics_factory=semantics_factory,
                mcp_input_required_terminal_source=(mcp_input_required_terminal_source),
            )
        )
        terminal_event = next(
            event
            for event in candidates
            if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
        )
        settlement = (
            self._tool_rollout_settlement_event(
                state,
                terminal_event=terminal_event,
                reservation=rollout_reservation,
            )
            if rollout_reservation is not None
            else None
        )
        write_candidates: tuple[AgentEvent, ...] = candidates
        if settlement is not None:
            write_candidates = (*write_candidates, settlement)
        track_mcp_terminal = (
            state.run_id,
            tool_call_id,
        ) in self._mcp_terminal_commit_outcomes
        if track_mcp_terminal:
            self._mark_mcp_terminal_commit_attempt(state, tool_call_id)
        mcp_pending_handle = self._mcp_terminal_pending_handles.get(
            (state.run_id, tool_call_id)
        )
        if track_mcp_terminal and mcp_pending_handle is None:
            raise RuntimeError("MCP terminal candidate lost its pending handle owner")
        terminal_registry = self._run_tools.tool_execution_terminal_registry
        if (
            rollout_reservation is not None
            or mcp_input_required_terminal_source is not None
        ):
            write_candidates = (
                await self._run_tools.tool_terminal_projection_service.prepare_batch(
                    write_candidates,
                    deadline_monotonic=(
                        deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else None
                    ),
                )
            )
            write_candidates = self._attach_mcp_terminal_disposition(
                state,
                prepared_candidates=write_candidates,
                source=mcp_input_required_terminal_source,
                disposition_kind=mcp_disposition_kind,
                closure_reason=mcp_closure_reason,
                source_binding=mcp_source_binding,
                effective_binding=mcp_effective_binding,
            )
        candidate_owner = None
        prepared_mcp_settlement = None
        if rollout_reservation is not None:
            candidate_owner = terminal_registry.freeze_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
                candidates=write_candidates,
                physical_owner_identity_fingerprint=(
                    mcp_pending_handle.identity.identity_fingerprint
                    if mcp_pending_handle is not None
                    else None
                ),
            )
            if mcp_pending_handle is not None:
                mcp_port = self._run_tools.mcp_tool_execution_port
                if mcp_port is None:
                    raise RuntimeError("MCP terminal candidate lost its execution port")
                prepared_mcp_settlement = mcp_port.prepare_terminal_settlement(
                    pending_handle=mcp_pending_handle,
                    reason=(
                        mcp_terminal_reason
                        or _mcp_terminal_reason_from_projection(
                            disposition_kind=mcp_disposition_kind,
                            closure_reason=mcp_closure_reason,
                            result_state=result_state,
                        )
                    ),
                    candidate_owner_identity=candidate_owner,
                    terminal_event_id=terminal_event.id,
                )
                terminal_registry.bind_transaction_companion(
                    owner_identity=candidate_owner,
                    transaction_companion=(
                        prepared_mcp_settlement.transaction_companion
                    ),
                )
        publication_maintenance_lease = None
        mcp_handoff_confirmed = False
        if (
            mcp_closure_reason is not None
            and self._run_ledger.publication_reconciliation_required
        ):
            if deadline_budget is None:
                raise RuntimeError(
                    "publication-latched MCP closure requires a frozen deadline budget"
                )
            publication_maintenance_lease = (
                self._run_ledger.issue_publication_terminal_maintenance_lease(
                    owner_kind="mcp_interaction_closure_bundle",
                    ordered_events=write_candidates,
                    transaction_companion=(
                        prepared_mcp_settlement.transaction_companion
                        if prepared_mcp_settlement is not None
                        else None
                    ),
                    deadline_budget=deadline_budget,
                )
            )
        try:
            if rollout_reservation is None:
                result = await self._run_ledger.write_events_with_deadline(
                    write_candidates,
                    deadline_monotonic=(
                        deadline_budget.terminal_deadline_monotonic
                        if (
                            deadline_budget is not None
                            and mcp_closure_reason is not None
                        )
                        else deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else self._run_ledger.new_write_deadline_monotonic()
                    ),
                    publication_terminal_maintenance_lease=(
                        publication_maintenance_lease
                    ),
                )
                stored_events = list(result.committed_events)
            else:
                result = await self._run_tools.event_commit_port().commit_terminal_batch_and_settlement(
                    terminal_candidates=tuple(
                        event for event in write_candidates if event.id != settlement.id
                    ),
                    settlement_candidate=settlement,
                    expected_reservation_fingerprint=(
                        rollout_reservation.semantic_fingerprint
                    ),
                    deadline_monotonic=(
                        deadline_budget.terminal_deadline_monotonic
                        if (
                            deadline_budget is not None
                            and mcp_closure_reason is not None
                        )
                        else deadline_budget.ordinary_deadline_monotonic
                        if deadline_budget is not None
                        else None
                    ),
                    publication_terminal_maintenance_lease=(
                        publication_maintenance_lease
                    ),
                )
                stored_events = list(result.committed_events)
                if result.reconciliation_required:
                    self._run_ledger.latch_event_commit_outcome_unknown()
            if candidate_owner is not None and prepared_mcp_settlement is not None:
                receipt = terminal_registry.confirm_stable_candidate_write(
                    owner_identity=candidate_owner,
                    outcome=EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=(
                            deadline_budget.terminal_deadline_monotonic
                            if deadline_budget is not None
                            else time.monotonic()
                        ),
                        result=result,
                    ),
                )
                transition = (
                    self._run_tools.mcp_tool_execution_port.confirm_terminal_commit(
                        settlement=prepared_mcp_settlement,
                        commit_receipt=receipt,
                    )
                )
                terminal_registry.accept_physical_owner_handoff(
                    transition.handoff_receipt
                )
                mcp_handoff_confirmed = True
            if result.publication_errors or result.publication_status == "unavailable":
                raise EventPublicationAfterCommitError(result)
        except EventPublicationAfterCommitError as exc:
            if (
                candidate_owner is not None
                and prepared_mcp_settlement is not None
                and not mcp_handoff_confirmed
            ):
                receipt = terminal_registry.confirm_stable_candidate_write(
                    owner_identity=candidate_owner,
                    outcome=EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=(
                            deadline_budget.terminal_deadline_monotonic
                            if deadline_budget is not None
                            else time.monotonic()
                        ),
                        result=exc.result,
                    ),
                )
                mcp_port = self._run_tools.mcp_tool_execution_port
                if mcp_port is None:
                    self._run_ledger.latch_event_commit_outcome_unknown()
                    raise RuntimeError(
                        "MCP terminal publication failure lost its execution port"
                    ) from exc
                transition = mcp_port.confirm_terminal_commit(
                    settlement=prepared_mcp_settlement,
                    commit_receipt=receipt,
                )
                terminal_registry.accept_physical_owner_handoff(
                    transition.handoff_receipt
                )
                mcp_handoff_confirmed = True
            if rollout_reservation is not None and prepared_mcp_settlement is None:
                terminal_registry.complete_terminal(
                    run_id=state.run_id,
                    reservation=rollout_reservation,
                )
            if track_mcp_terminal:
                self._mark_mcp_terminal_commit_full(state, tool_call_id)
            stored_events = list(exc.result.committed_events)
            self._record_tool_result_events(
                state,
                stored_events=(
                    self._committed_tool_result_events(
                        state,
                        tool_call_id=tool_call_id,
                        start_event_id=_tool_timing_start_event_id(
                            tool_observation_timing_seed
                        ),
                    )
                    or stored_events
                ),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
            raise
        except BaseException as error:
            outcome = self._run_ledger.resolved_write_outcome(error)
            if (
                candidate_owner is not None
                and prepared_mcp_settlement is not None
                and not mcp_handoff_confirmed
            ):
                receipt = terminal_registry.confirm_stable_candidate_write(
                    owner_identity=candidate_owner,
                    outcome=outcome,
                )
                transition = (
                    self._run_tools.mcp_tool_execution_port.confirm_terminal_commit(
                        settlement=prepared_mcp_settlement,
                        commit_receipt=receipt,
                    )
                )
                terminal_registry.accept_physical_owner_handoff(
                    transition.handoff_receipt
                )
            if outcome.status == "unknown":
                if track_mcp_terminal:
                    self._mark_mcp_terminal_commit_untrusted(state, tool_call_id)
                if rollout_reservation is not None and prepared_mcp_settlement is None:
                    terminal_registry.mark_commit_outcome_unknown(
                        run_id=state.run_id,
                        reservation=rollout_reservation,
                    )
                raise
            if outcome.status == "none":
                # The complete stable batch is absent, so the caller may safely
                # restore its pre-write process-local state and retry.
                if track_mcp_terminal:
                    self._mark_mcp_terminal_commit_none(state, tool_call_id)
                raise
            if track_mcp_terminal:
                self._mark_mcp_terminal_commit_full(state, tool_call_id)
            if rollout_reservation is not None and prepared_mcp_settlement is None:
                terminal_registry.complete_terminal(
                    run_id=state.run_id,
                    reservation=rollout_reservation,
                )
            stored_events = list(outcome.committed_events)
            self._record_tool_result_events(
                state,
                stored_events=(
                    self._committed_tool_result_events(
                        state,
                        tool_call_id=tool_call_id,
                        start_event_id=_tool_timing_start_event_id(
                            tool_observation_timing_seed
                        ),
                    )
                    or stored_events
                ),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
            raise
        if track_mcp_terminal:
            self._mark_mcp_terminal_commit_full(state, tool_call_id)
        if rollout_reservation is not None and prepared_mcp_settlement is None:
            terminal_registry.complete_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
            )
        for event in stored_events:
            yield event
        self._record_tool_result_events(
            state,
            stored_events=(
                self._committed_tool_result_events(
                    state,
                    tool_call_id=tool_call_id,
                    start_event_id=_tool_timing_start_event_id(
                        tool_observation_timing_seed
                    ),
                )
                or stored_events
            ),
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
        )

    def _pending_tool_rollout_reservation(
        self,
        payload: dict[str, Any],
        *,
        run_id: str,
    ) -> RolloutReservationFact:
        reservation_id = _required_str(
            payload.get("rollout_reservation_id"),
            "pending tool rollout reservation id",
        )
        reservation_fingerprint = _required_str(
            payload.get("rollout_reservation_fingerprint"),
            "pending tool rollout reservation fingerprint",
        )
        tool_call_id = _required_str(
            payload.get("tool_call_id"),
            "pending tool rollout call id",
        )
        matches = tuple(
            reservation
            for state in self._run_long_horizon.store.rollout_states()
            for reservation in state.active_reservations
            if reservation.reservation_id == reservation_id
        )
        binding = self._run_long_horizon.resolve_rollout_binding(
            run_id=run_id,
        )
        if binding.child_state is not None:
            matches = (
                *matches,
                *(
                    reservation
                    for reservation in binding.child_state.active_reservations
                    if reservation.reservation_id == reservation_id
                ),
            )
        if len(matches) != 1:
            raise RuntimeError(
                "pending tool interaction lost its active rollout reservation"
            )
        reservation = matches[0]
        if (
            reservation.semantic_fingerprint != reservation_fingerprint
            or reservation.owner_kind != "tool_call"
            or reservation.owner_id != tool_call_id
        ):
            raise RuntimeError("pending tool rollout reservation identity mismatch")
        terminal_registry = self._run_tools.tool_execution_terminal_registry
        if (
            terminal_registry.owner_for_call(
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            is None
        ):
            terminal_registry.restore_suspended(
                run_id=run_id,
                reservation=reservation,
            )
        return reservation

    def _tool_rollout_settlement_event(
        self,
        state: RunActivationWorkingState,
        *,
        terminal_event: ToolResultEndEvent | ToolResultEndCandidate,
        reservation: RolloutReservationFact,
    ) -> RolloutBudgetReservationSettledEvent:
        if (
            reservation.owner_kind != "tool_call"
            or reservation.owner_id != terminal_event.tool_call_id
        ):
            raise RuntimeError("tool rollout settlement owner mismatch")
        return RolloutBudgetReservationSettledEvent(
            id=f"rollout_budget_reservation_settled:{reservation.reservation_id}",
            **self._event_context(state).event_fields(),
            reservation_id=reservation.reservation_id,
            charged_milliunits=reservation.reserved_milliunits,
            usage_status="tool_terminal",
            usage_charge=None,
            source_model_call_end_event_id=None,
            source_tool_result_event_id=terminal_event.id,
            child_usage_handoff=None,
        )

    def _mcp_terminal_commit_key(
        self,
        state: RunActivationWorkingState,
        tool_call_id: str,
    ) -> tuple[str, str]:
        key = (state.run_id, tool_call_id)
        if key not in self._mcp_terminal_commit_outcomes:
            raise RuntimeError("MCP terminal commit owner is not active")
        return key

    def _mark_mcp_terminal_commit_attempt(
        self,
        state: RunActivationWorkingState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "attempting"

    def _mark_mcp_terminal_commit_none(
        self,
        state: RunActivationWorkingState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "none"

    def _mark_mcp_terminal_commit_full(
        self,
        state: RunActivationWorkingState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "full"

    def _mark_mcp_terminal_commit_untrusted(
        self,
        state: RunActivationWorkingState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "untrusted"

    def _resolve_mcp_terminal_commit_failure(
        self,
        state: RunActivationWorkingState,
        *,
        tool_call_id: str,
        candidates: tuple[AgentEvent, ...],
        error: BaseException,
    ) -> None:
        if isinstance(error, EventPublicationAfterCommitError):
            self._mark_mcp_terminal_commit_full(state, tool_call_id)
            return
        del candidates
        outcome = self._run_ledger.resolved_write_outcome(error)
        if outcome.status == "unknown":
            self._mark_mcp_terminal_commit_untrusted(state, tool_call_id)
            return
        if outcome.status == "none":
            self._mark_mcp_terminal_commit_none(state, tool_call_id)
            return
        self._mark_mcp_terminal_commit_full(state, tool_call_id)

    def _committed_tool_result_events(
        self,
        state: RunActivationWorkingState,
        *,
        tool_call_id: str,
        start_event_id: str | None = None,
    ) -> list[AgentEvent]:
        events = self._run_tools.completed_tool_result_events(
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            start_event_id=start_event_id,
        )
        if not any(isinstance(event, ToolResultEndEvent) for event in events):
            return []
        return events

    def _record_tool_result_events(
        self,
        state: RunActivationWorkingState,
        *,
        stored_events: list[AgentEvent],
        tool_call_id: str,
        tool_call_name: str,
    ) -> None:
        if any(result.id == tool_call_id for result in state.tool_results):
            return
        result_block = _tool_result_from_event_slice(stored_events, tool_call_id)
        _remember_tool_result_event_span(state, stored_events, tool_call_id)
        state.tool_results.append(result_block)
        state.messages.append(
            _tool_result_message_from_events(
                stored_events,
                tool_call_name,
                result_block,
            )
        )

    def _plan_state(self, state: RunActivationWorkingState) -> PlanWorkflowState:
        plan_state = state.plan_progress.workflow_state
        if isinstance(plan_state, PlanWorkflowState):
            return plan_state
        plan_state = PlanWorkflowState()
        state.plan_progress.workflow_state = plan_state
        return plan_state

    def _plan_revision_required(self, state: RunActivationWorkingState) -> bool:
        return state.plan_progress.revision_required and self._plan_state(state).active

    def _consume_plan_interaction_budget(
        self, state: RunActivationWorkingState
    ) -> bool:
        consumed = state.plan_progress.interactions
        if consumed >= state.budget.max_plan_interactions_per_run:
            state.status = LoopStatus.FAILED
            state.stop_reason = RunStopReason.PLAN_INTERACTION_BUDGET
            state.error_message = "plan interaction budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.plan_progress.interactions = consumed + 1
        return True

    async def _emit_plan_budget_error_result(
        self,
        state: RunActivationWorkingState,
        call: ToolCall,
        *,
        kind: str,
        rollout_reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        message = f"plan {kind} budget exceeded"
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=message,
            result_state=ToolResultState.ERROR,
            tool_arguments=call.arguments,
            failure_stage="workflow_budget_exceeded",
            rollout_reservation=rollout_reservation,
        ):
            yield event
        yield await self._run_ledger.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
        )

    async def _mark_plan_budget_exceeded(
        self, state: RunActivationWorkingState, *, kind: str
    ) -> AgentEvent:
        message = f"plan {kind} budget exceeded"
        state.status = LoopStatus.FAILED
        state.stop_reason = RunStopReason.PLAN_INTERACTION_BUDGET
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self._run_ledger.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
        )

    def _policy_from_plan_state(
        self, plan_state: PlanWorkflowState
    ) -> EffectivePermissionPolicy:
        payload = plan_state.pre_plan_permission_policy or {}
        if not payload or plan_state.pre_plan_permission_mode is None:
            raise ValueError(
                "plan workflow is missing preset previous permission facts"
            )
        validate_preset_policy_payload(
            plan_state.pre_plan_permission_mode,
            dict(payload),
            context="PlanWorkflowState.pre_plan",
        )
        return EffectivePermissionPolicy(
            profile=PermissionProfile(str(payload["profile"])),
            approval=ApprovalPolicy(str(payload["approval_policy"])),
            terminal=TerminalAccess(str(payload["terminal_access"])),
            execution_boundary="host",
            network_isolated=bool(payload.get("network_isolated", False)),
        )

    async def _stream_confirmed_tool_blocks(
        self,
        state: RunActivationWorkingState,
        decisions_by_id,
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []

        async def flush_parsed_calls() -> AsyncIterator[AgentEvent]:
            nonlocal parsed_calls
            if not parsed_calls:
                return
            calls = parsed_calls
            parsed_calls = []
            exposure = self._require_capability_exposure(state)
            executable_calls: list[ToolCall] = []
            async for event_or_calls in self._stream_capability_access_filtered_calls(
                state,
                calls,
                exposure=exposure,
            ):
                if isinstance(event_or_calls, tuple):
                    executable_calls = event_or_calls[0]
                else:
                    yield event_or_calls
            if not executable_calls:
                return
            if any(call.name in PLAN_WORKFLOW_TOOL_NAMES for call in executable_calls):
                async for event in self._handle_workflow_tool_batch(
                    state, executable_calls
                ):
                    yield event
                return
            async for event in self._stream_parsed_tool_calls(state, executable_calls):
                yield event

        for block in state.pending_tool_calls:
            decision = decisions_by_id[block.id]
            if not decision.confirmed:
                async for event in flush_parsed_calls():
                    yield event
                stored_events = await self._run_ledger.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message="tool call denied by user approval",
                        result_state=ToolResultState.DENIED,
                        arguments=_tool_block_arguments_for_semantics(block),
                        failure_stage="permission_denied",
                    ),
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    _tool_result_message_from_events(
                        stored_events, block.name, result_block
                    )
                )
                continue
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                async for event in flush_parsed_calls():
                    yield event
                stored_events = await self._run_ledger.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                        failure_stage="malformed_arguments",
                    ),
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    _tool_result_message_from_events(
                        stored_events, block.name, result_block
                    )
                )
        async for event in flush_parsed_calls():
            yield event

    async def _stream_parsed_tool_calls(
        self,
        state: RunActivationWorkingState,
        parsed_calls: list[ToolCall],
    ) -> AsyncIterator[AgentEvent]:
        exposure = self._require_capability_exposure(state)
        for logical_batch in _tool_batches(
            parsed_calls,
            self.tool_executor,
            exposure=exposure,
        ):
            remaining = list(logical_batch)
            while remaining:
                capacity = self._run_tools.physical_dispatch_capacity(
                    PhysicalOperationKind.TOOL_CALL
                )
                if capacity <= 0:
                    await self._run_tools.ensure_physical_operation_headroom(
                        PhysicalOperationKind.TOOL_CALL
                    )
                    capacity = self._run_tools.physical_dispatch_capacity(
                        PhysicalOperationKind.TOOL_CALL
                    )
                if capacity <= 0:
                    raise RuntimeError(
                        "tool execution is blocked by physical ledger headroom"
                    )
                batch = remaining[:capacity]
                del remaining[:capacity]
                async for event in self._stream_physically_admitted_tool_batch(
                    state,
                    batch,
                    exposure=exposure,
                ):
                    yield event
                if state.status is LoopStatus.WAITING_USER:
                    return

    async def _stream_physically_admitted_tool_batch(
        self,
        state: RunActivationWorkingState,
        batch: list[ToolCall],
        *,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent]:
        (
            stored_admissions,
            executable_batch,
            reservations,
        ) = await self._commit_tool_admissions(
            state,
            batch,
            exposure=exposure,
        )
        for event in stored_admissions:
            yield event
        batch_events: list[AgentEvent] = [
            event
            for event in stored_admissions
            if isinstance(
                event,
                (
                    ToolResultStartEvent,
                    ToolResultTextDeltaEvent,
                    ToolResultDataDeltaEvent,
                    ToolResultEndEvent,
                ),
            )
        ]
        if executable_batch:
            async for event in self._stream_tool_batch_events(
                state,
                executable_batch,
                batch_events,
                exposure=exposure,
                reservations=reservations,
            ):
                yield event
        if state.status is LoopStatus.WAITING_USER:
            return
        for call in batch:
            result_block = _tool_result_from_event_slice(batch_events, call.id)
            _remember_tool_result_event_span(state, batch_events, call.id)
            state.tool_results.append(result_block)
            state.messages.append(
                _tool_result_message_from_events(batch_events, call.name, result_block)
            )
            if call.id in reservations:
                state.tool_call_count += 1

    async def _commit_tool_admissions(
        self,
        state: RunActivationWorkingState,
        calls: list[ToolCall],
        *,
        exposure: CapabilityExposurePlan,
    ) -> tuple[
        tuple[AgentEvent, ...],
        list[ToolCall],
        dict[str, RolloutReservationFact],
    ]:
        if not calls:
            raise ValueError("tool admission batch cannot be empty")
        prelude_events: list[AgentEvent] = []
        for _attempt in range(len(RolloutPhase) + 1):
            binding = self._run_long_horizon.resolve_rollout_binding(
                run_id=state.run_id,
            )
            if binding.child_state is not None:
                plan = None
                break
            plan = plan_root_tool_admission(
                account=binding.account,
                state=binding.parent_state,
                attempted_tool_call_count=len(calls),
            )
            if plan.action == "transition":
                transition = await self._run_ledger.emit(
                    build_rollout_phase_transition_event(
                        event_context=self._event_context(state),
                        account=binding.account,
                        state=binding.parent_state,
                        plan=plan,
                    ),
                )
                prelude_events.append(transition)
                continue
            if plan.action == "blocked":
                raise RuntimeError(
                    "tool admission is blocked by unresolved rollout reservations"
                )
            break
        else:
            raise RuntimeError("tool admission phase transition did not converge")

        account_id = binding.account.account_id
        rollout_state = binding.parent_state
        rollout_account = binding.account
        phase = rollout_state.phase
        bucket = (
            RolloutBudgetBucket.EXPLORATION
            if binding.child_state is not None
            else plan.budget_bucket
            if plan is not None and plan.action == "admit"
            else None
        )
        source_sequence = (
            binding.child_state.through_sequence
            if binding.child_state is not None
            else rollout_state.through_sequence
        )
        allow_facts: dict[str, CapabilityGateDecisionFact] = {}
        deny_reasons: dict[str, tuple[str, str]] = {}
        allowed_classes = set(allowed_action_classes_for_phase(phase))
        reserved_by_call: dict[str, int] = {}
        for call in calls:
            fact = self._capability_gate_decision_fact(
                state,
                call,
                exposure=exposure,
                decision=PermissionDecision.allow(),
            )
            classification = fact.action_classification
            descriptor = exposure.descriptors_by_name.get(call.name)
            if classification is None or descriptor is None:
                raise RuntimeError(
                    "known executable tool lacks rollout action semantics"
                )
            if phase in {
                RolloutPhase.EXHAUSTED,
                RolloutPhase.EMERGENCY_HARD_STOP,
            }:
                code = (
                    "rollout_emergency_hard_stop"
                    if phase is RolloutPhase.EMERGENCY_HARD_STOP
                    else "rollout_phase_tool_denied"
                )
                deny_reasons[call.id] = (
                    code,
                    f"tool execution is unavailable in rollout phase {phase.value}",
                )
                continue
            if (
                phase not in descriptor.long_horizon_policy.allowed_in_phases
                or classification.action_class not in allowed_classes
            ):
                deny_reasons[call.id] = (
                    "rollout_phase_tool_denied",
                    "tool action class is not allowed in the current rollout phase",
                )
                continue
            reserved_milliunits = (
                classification.rollout_cost_units
                * rollout_account.policy.tool_cost_unit_weight_milli
            )
            if reserved_milliunits <= 0:
                raise RuntimeError(
                    "production tool action must reserve positive rollout cost"
                )
            allow_facts[call.id] = fact
            reserved_by_call[call.id] = reserved_milliunits

        requested_milliunits = sum(reserved_by_call.values())
        if binding.child_state is not None:
            available_milliunits = binding.child_state.remaining_milliunits
        elif bucket is not None:
            available_milliunits = rollout_bucket_remaining(
                account=rollout_account,
                state=rollout_state,
                bucket=bucket,
            )
        else:
            available_milliunits = 0
        if requested_milliunits > available_milliunits:
            for call_id in tuple(allow_facts):
                deny_reasons[call_id] = (
                    "rollout_tool_budget_unavailable",
                    "tool batch exceeds the remaining rollout tool budget",
                )
                allow_facts.pop(call_id)
                reserved_by_call.pop(call_id)

        gate_items: list[
            tuple[
                CapabilityGateDecisionEvent,
                RolloutBudgetReservationCreatedEvent | None,
                tuple[AgentEvent, ...],
            ]
        ] = []
        reservations: dict[str, RolloutReservationFact] = {}
        for call in calls:
            denial = deny_reasons.get(call.id)
            if denial is not None:
                reason_code, reason_message = denial
                fact = self._capability_gate_decision_fact(
                    state,
                    call,
                    exposure=exposure,
                    decision=PermissionDecision(
                        kind=PermissionDecisionKind.DENY,
                        reason=reason_message,
                    ),
                    result_state=ToolResultState.DENIED,
                    reason_code_override=reason_code,
                )
                terminal_candidates = tuple(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=reason_message,
                        result_state=ToolResultState.DENIED,
                        arguments=call.arguments,
                        failure_stage="policy_denied",
                        reason_code=reason_code,
                    )
                )
                gate_items.append(
                    (
                        self._capability_gate_decision_event(state, fact),
                        None,
                        terminal_candidates,
                    )
                )
                continue
            fact = allow_facts[call.id]
            reserved_milliunits = reserved_by_call[call.id]
            if bucket is None:
                raise RuntimeError("admitted tool call lacks a rollout budget bucket")
            reservation_payload = {
                "reservation_id": (
                    "rollout_reservation:tool:"
                    f"{state.run_id}:{state.reply_id}:{call.id}"
                ),
                "account_id": account_id,
                "owner_kind": "tool_call",
                "owner_id": call.id,
                "phase_at_reservation": phase,
                "budget_bucket": bucket,
                "reserved_milliunits": reserved_milliunits,
                "model_call_reservation_quote": None,
                "source_sequence": source_sequence,
            }
            reservation = RolloutReservationFact(
                **reservation_payload,
                semantic_fingerprint=context_fingerprint(
                    "rollout-reservation:v1", reservation_payload
                ),
            )
            gate_event = self._capability_gate_decision_event(state, fact)
            reservation_event = RolloutBudgetReservationCreatedEvent(
                id=(
                    "rollout_budget_reservation_created:tool:"
                    f"{state.run_id}:{state.reply_id}:{call.id}"
                ),
                **self._event_context(state).event_fields(),
                reservation=reservation,
            )
            gate_items.append((gate_event, reservation_event, ()))
            reservations[call.id] = reservation
        result = await self._run_tools.event_commit_port().commit_gate_batch(
            gate_items=gate_items,
            expected_account_state_fingerprint=rollout_state.state_fingerprint,
            account_id=account_id,
        )
        if result.reconciliation_required:
            raise RuntimeError(
                "tool admission committed without a healthy reducer fold"
            )
        if reservations:
            try:
                self._run_tools.tool_execution_terminal_registry.install_admitted_batch(
                    run_id=state.run_id,
                    reservations=tuple(reservations.values()),
                )
            except BaseException:
                # Admission is already durable.  Losing its sole process owner is
                # an unknown execution state, so no physical tool may start.
                self._run_ledger.latch_event_commit_outcome_unknown()
                raise
        executable_calls = [call for call in calls if call.id in reservations]
        return (
            (*prelude_events, *result.committed_events),
            executable_calls,
            reservations,
        )

    async def _commit_tool_terminal(
        self,
        state: RunActivationWorkingState,
        *,
        terminal_event: ToolResultEndCandidate,
        reservation: RolloutReservationFact,
        prepared_monitor_registration=None,
        prepared_notification_reservation=None,
        prepared_monitor_cancellation=None,
    ) -> tuple[AgentEvent, ...]:
        await self._run_tools.terminal_monitor_coordinator.ensure_terminal_receipt_observation(
            terminal_event.terminal_process_observation_receipt
        )
        settlement = self._tool_rollout_settlement_event(
            state,
            terminal_event=terminal_event,
            reservation=reservation,
        )
        registration_events = (
            ()
            if prepared_monitor_registration is None
            else (prepared_monitor_registration.registered_event,)
        )
        source_candidates = (
            *(
                ()
                if prepared_monitor_cancellation is None
                else prepared_monitor_cancellation.stable_candidates
            ),
            terminal_event,
            *registration_events,
            settlement,
        )
        prepared_candidates = (
            await self._run_tools.tool_terminal_projection_service.prepare_batch(
                source_candidates
            )
        )
        committed_terminal = next(
            item
            for item in prepared_candidates
            if isinstance(item, ToolResultEndEvent) and item.id == terminal_event.id
        )
        receipt_candidates = self._tool_receipt_notification_candidates(
            tool_result_end=committed_terminal,
        )
        candidates = tuple(
            item
            for candidate in prepared_candidates
            for item in (
                (*receipt_candidates, candidate)
                if candidate.id == settlement.id
                else (candidate,)
            )
        )
        if prepared_notification_reservation is not None:
            committed_registration = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, TerminalProcessMonitorRegisteredEvent)
                ),
                None,
            )
            notification_event = self._run_tools.terminal_notification_account_coordinator.freeze_created_event(
                prepared=prepared_notification_reservation,
                cause_events=tuple(
                    item
                    for item in (committed_terminal, committed_registration)
                    if item is not None
                ),
                registration_event=committed_registration,
            )
            candidates = tuple(
                item
                for candidate in candidates
                for item in (
                    (notification_event, candidate)
                    if candidate.id == settlement.id
                    else (candidate,)
                )
            )
        terminal_registry = self._run_tools.tool_execution_terminal_registry
        terminal_registry.freeze_terminal(
            run_id=state.run_id,
            reservation=reservation,
            candidates=candidates,
        )
        try:
            result = await self._run_tools.event_commit_port().commit_terminal_batch_and_settlement(
                terminal_candidates=tuple(
                    event for event in candidates if event.id != settlement.id
                ),
                settlement_candidate=settlement,
                expected_reservation_fingerprint=reservation.semantic_fingerprint,
            )
        except BaseException:
            if self._run_ledger.reconciliation_required:
                terminal_registry.mark_commit_outcome_unknown(
                    run_id=state.run_id,
                    reservation=reservation,
                )
            raise
        if result.reconciliation_required:
            terminal_registry.mark_commit_outcome_unknown(
                run_id=state.run_id,
                reservation=reservation,
            )
            reducer_details = "; ".join(
                f"{item.reducer_id}: {item.error_type}: {item.message}"
                for item in result.reducer_errors
            )
            raise RuntimeError(
                "tool terminal committed without a healthy reducer fold"
                + (f" ({reducer_details})" if reducer_details else "")
            )
        terminal_registry.complete_terminal(
            run_id=state.run_id,
            reservation=reservation,
        )
        return result.committed_events

    def _tool_receipt_notification_candidates(
        self,
        *,
        tool_result_end: ToolResultEndEvent,
    ) -> tuple[AgentEvent, ...]:
        receipt_applications = (
            self._run_tools.terminal_monitor_coordinator.prepare_receipt_applications(
                tool_result_end
            )
        )
        dominated = (
            self._run_tools.terminal_notification_store.receipt_dominated_notifications(
                tool_result_end
            )
        )
        if not dominated:
            return receipt_applications
        source_references = tuple(
            sorted(
                (
                    event_reference_from_stored(
                        item.source_event,
                        runtime_session_id=self._run_identity.runtime_session_id,
                    )
                    for item in dominated
                ),
                key=lambda item: (item.sequence, item.event_id),
            )
        )
        disposition = TerminalProcessObservationDeliveryDispositionEvent(
            id=context_fingerprint(
                "terminal-notification-tool-receipt-disposition-id:v1",
                (
                    tool_result_end.id,
                    tuple(item.event_id for item in source_references),
                ),
            ).replace("sha256:", "terminal_notification_disposition:"),
            run_id=tool_result_end.run_id,
            turn_id=tool_result_end.turn_id,
            reply_id=tool_result_end.reply_id,
            observation_source_references=source_references,
            outcome="explicitly_observed",
            tool_result_end_event_identity=stable_event_identity(
                tool_result_end,
                runtime_session_id=self._run_identity.runtime_session_id,
            ),
        )
        terminal_process_ids = tuple(
            sorted(
                {
                    _terminal_notification_process_id(item.source_event)
                    for item in dominated
                    if _terminal_notification_is_terminal(item.source_event)
                }
            )
        )
        releases = (
            self._run_tools.terminal_notification_account_coordinator.freeze_released_events(
                reservation_ids=tuple(
                    f"terminal_completion_head:{process_id}"
                    for process_id in terminal_process_ids
                ),
                cause_events=(disposition,),
            )
            if terminal_process_ids
            else ()
        )
        return *receipt_applications, disposition, *releases

    async def _stream_tool_batch_events(
        self,
        state: RunActivationWorkingState,
        batch: list[ToolCall],
        batch_events: list[AgentEvent],
        *,
        exposure: CapabilityExposurePlan,
        reservations: dict[str, RolloutReservationFact],
    ) -> AsyncIterator[AgentEvent]:
        tap = _ToolBatchTap({call.id for call in batch})
        self._run_tools.publisher.subscribe(tap)
        executor = ToolExecutor(
            registry=self.tool_executor.registry,
            record_event=self._run_tools.make_thread_recorder(),
            artifact_service=self.tool_executor.artifact_service,
            artifact_policies=self.tool_executor.artifact_policies,
            runtime_session_id=self._run_identity.runtime_session_id,
            semantics_registry=self.tool_executor.semantics_registry,
            essential_capture_policy=(self.tool_executor.essential_capture_policy),
        )

        async def execute_call(
            call: ToolCall,
        ) -> ToolExecutionResult | ToolExecutionSuspended:
            descriptor = exposure.descriptors_by_name.get(call.name)
            resources = state.execution_resources
            borrow_authority = resources.capability_execution_borrow_authority
            borrow_kind = resources.capability_execution_borrow_kind
            is_async = executor.is_async(call)

            def acquire_borrow() -> None:
                if borrow_authority is None:
                    return
                if borrow_kind == "child":
                    borrow_authority.borrow_child_tool_call()
                else:
                    borrow_authority.borrow_parent_tool_call()

            def release_borrow() -> None:
                if borrow_authority is None:
                    return
                if borrow_kind == "child":
                    borrow_authority.release_child_tool_call()
                else:
                    borrow_authority.release_parent_tool_call()

            acquire_borrow()
            if is_async:
                try:
                    return await executor.execute_async(
                        call,
                        event_context=self._event_context(state),
                        descriptor=descriptor,
                        descriptor_attribution=self._descriptor_render_attribution(
                            state, descriptor
                        ),
                        context_id=state.model_tool_progress.current_context_id,
                        model_call_index=(
                            state.model_tool_progress.current_model_call_index
                        ),
                        runtime_context=self._tool_runtime_context(
                            state,
                            context_id=state.model_tool_progress.current_context_id,
                            model_call_index=(
                                state.model_tool_progress.current_model_call_index
                            ),
                        ),
                    )
                finally:
                    release_borrow()

            return await _await_sync_tool_thread(
                lambda: executor.execute(
                    call,
                    event_context=self._event_context(state),
                    descriptor=descriptor,
                    descriptor_attribution=self._descriptor_render_attribution(
                        state, descriptor
                    ),
                    context_id=state.model_tool_progress.current_context_id,
                    model_call_index=(
                        state.model_tool_progress.current_model_call_index
                    ),
                    runtime_context=self._tool_runtime_context(
                        state,
                        context_id=state.model_tool_progress.current_context_id,
                        model_call_index=(
                            state.model_tool_progress.current_model_call_index
                        ),
                    ),
                ),
                release_borrow=release_borrow,
            )

        tasks_by_call = {
            asyncio.create_task(execute_call(call)): call for call in batch
        }
        pending = set(tasks_by_call)
        completed_tool_calls: set[str] = set()
        terminal_settlements: dict[str, RolloutBudgetReservationSettledEvent] = {}

        try:
            while (
                pending
                or len(completed_tool_calls) < len(batch)
                or not tap.queue.empty()
            ):
                while not tap.queue.empty():
                    event = tap.queue.get_nowait()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
                    if isinstance(event, ToolResultEndEvent):
                        settlement = terminal_settlements.pop(event.tool_call_id)
                        yield settlement
                if pending:
                    done, pending = await asyncio.wait(
                        pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        outcome = task.result()
                        if isinstance(outcome, ToolExecutionSuspended):
                            async for event in self._suspend_tool_execution(
                                state,
                                outcome,
                                reservation=reservations[tasks_by_call[task].id],
                            ):
                                yield event
                            for pending_task in pending:
                                pending_task.cancel()
                            if pending:
                                await asyncio.gather(
                                    *pending,
                                    return_exceptions=True,
                                )
                            pending = set()
                            return
                        call = tasks_by_call[task]
                        prepared_terminal = outcome.prepared_terminal_result
                        if prepared_terminal is None:
                            raise RuntimeError(
                                "production tool execution did not prepare a terminal fact"
                            )
                        terminal_event = build_tool_result_terminal_event(
                            event_context=self._event_context(state),
                            prepared=prepared_terminal,
                        )
                        reservation = reservations[call.id]
                        terminal_events = await self._commit_tool_terminal(
                            state,
                            terminal_event=terminal_event,
                            reservation=reservation,
                            prepared_monitor_registration=(
                                prepared_terminal.prepared_terminal_monitor_registration
                            ),
                            prepared_notification_reservation=(
                                prepared_terminal.prepared_terminal_notification_reservation
                            ),
                            prepared_monitor_cancellation=(
                                prepared_terminal.prepared_terminal_monitor_cancellation
                            ),
                        )
                        settlement = next(
                            event
                            for event in terminal_events
                            if isinstance(event, RolloutBudgetReservationSettledEvent)
                        )
                        terminal_settlements[call.id] = settlement
                    continue
                if len(completed_tool_calls) < len(batch):
                    event = await tap.queue.get()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
                    if isinstance(event, ToolResultEndEvent):
                        settlement = terminal_settlements.pop(event.tool_call_id)
                        yield settlement
        finally:
            self._run_tools.publisher.unsubscribe(tap)
            pending_tasks = tuple(pending)
            for task in pending_tasks:
                if not task.done():
                    task.cancel()
            if pending_tasks:
                pending_outcomes = await asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                )
                for task, outcome in zip(pending_tasks, pending_outcomes):
                    call = tasks_by_call[task]
                    reservation = reservations.get(call.id)
                    if reservation is None:
                        # Private unit-level callers may exercise physical
                        # borrow behavior without durable admission.  The
                        # production path always supplies one reservation per
                        # call and is guarded before any tool starts.
                        continue
                    if isinstance(outcome, ToolExecutionSuspended):
                        async for _event in self._suspend_tool_execution(
                            state,
                            outcome,
                            reservation=reservation,
                        ):
                            pass
                        continue
                    if not isinstance(outcome, ToolExecutionResult):
                        continue
                    prepared_terminal = outcome.prepared_terminal_result
                    if prepared_terminal is None:
                        raise RuntimeError(
                            "cancelled tool execution did not prepare a terminal fact"
                        )
                    await self._commit_tool_terminal(
                        state,
                        terminal_event=build_tool_result_terminal_event(
                            event_context=self._event_context(state),
                            prepared=prepared_terminal,
                        ),
                        reservation=reservation,
                        prepared_monitor_registration=(
                            prepared_terminal.prepared_terminal_monitor_registration
                        ),
                        prepared_notification_reservation=(
                            prepared_terminal.prepared_terminal_notification_reservation
                        ),
                        prepared_monitor_cancellation=(
                            prepared_terminal.prepared_terminal_monitor_cancellation
                        ),
                    )

    async def _suspend_tool_execution(
        self,
        state: RunActivationWorkingState,
        suspended: ToolExecutionSuspended,
        *,
        reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        deadline_budget = build_runtime_event_deadline_budget(
            admitted_at_monotonic=time.monotonic(),
            total_timeout_seconds=30.0,
            terminal_reserve_seconds=10.0,
        )
        pending_handle = suspended.mcp_pending_handle
        view = pending_handle.suspension_commit_view
        interaction = view.interaction
        if (
            interaction.tool_call_id != suspended.tool_call_id
            or interaction.tool_name != suspended.tool_name
        ):
            raise ValueError("prepared MCP suspension tool identity drifted")
        predecessor = (
            state.execution_resources.latest_mcp_input_required_resolution_reference
        )
        if interaction.round_count == 1:
            predecessor = None
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("MCP suspension requires committed run authority")
        suspension_fact = build_frozen_fact(
            McpInputRequiredSuspensionFact,
            schema_version="mcp_input_required_suspension.v2",
            interaction=interaction,
            binding_identity=view.binding_identity,
            pending_lease_reservation=view.pending_lease_reservation,
            request_envelope=view.request_envelope,
            durable_continuation=view.durable_continuation,
            rollout_reservation_id=reservation.reservation_id,
            rollout_reservation_fingerprint=reservation.semantic_fingerprint,
            source_mcp_installation_id=(
                working_set.frozen_execution_surface.identity.mcp_installation_id
            ),
            predecessor_resolution_submitted_event_reference=predecessor,
        )
        payload: dict[str, object] = {
            "interaction_id": interaction.interaction_id,
            "kind": "mcp_input_required",
            "tool_call_id": suspended.tool_call_id,
            "tool_name": suspended.tool_name,
            "server_id": interaction.server_id,
            "round_count": interaction.round_count,
            "mcp_pending_handle": pending_handle,
            "suspension_fact": suspension_fact,
            "deadline_monotonic": view.deadline_monotonic,
            "tool_observation_timing_seed": (
                thaw_json(suspended.tool_observation_timing_seed)
                if suspended.tool_observation_timing_seed is not None
                else {}
            ),
            "rollout_reservation_id": reservation.reservation_id,
            "rollout_reservation_fingerprint": reservation.semantic_fingerprint,
        }
        original_pending_tool_calls = list(state.pending_tool_calls)
        original_pending_kind = state.pending_interaction_kind
        original_pending_payload = dict(state.pending_interaction_payload)
        original_pending_source_reference = (
            state.pending_interaction_source_event_reference
        )
        original_pending_source_candidate = (
            state.pending_interaction_source_event_candidate
        )
        original_status = state.status
        original_stop_reason = state.stop_reason
        original_transition = state.last_transition
        state.pending_tool_calls = []
        state.pending_interaction_kind = suspended.interaction_kind
        state.pending_interaction_payload = payload
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)
        suspension_event = ToolExecutionSuspendedEvent(
            id=view.suspension_event_id,
            **self._event_context(state).event_fields(),
            interaction_kind="mcp_input_required",
            tool_call_id=suspended.tool_call_id,
            tool_name=suspended.tool_name,
            suspension=suspension_fact,
        )
        mcp_port = self._run_tools.mcp_tool_execution_port
        if mcp_port is None:
            raise RuntimeError("MCP suspension lost its execution port owner")
        terminal_registry = self._run_tools.tool_execution_terminal_registry
        candidate_owner = terminal_registry.freeze_suspension(
            run_id=state.run_id,
            reservation=reservation,
            candidates=(suspension_event,),
            physical_owner_identity_fingerprint=(
                pending_handle.identity.identity_fingerprint
            ),
            transaction_companion=view.transaction_companion,
        )
        mcp_port.bind_suspension_candidate(
            pending_handle=pending_handle,
            candidate_owner_identity=candidate_owner,
        )
        suspension_publication_unavailable = False
        suspension_reconciliation_required = False
        try:
            commit_result = await self._run_tools.event_commit_port().commit_suspension(
                suspension_candidate=suspension_event,
                reservation_id=reservation.reservation_id,
                expected_reservation_fingerprint=reservation.semantic_fingerprint,
                deadline_monotonic=deadline_budget.ordinary_deadline_monotonic,
                transaction_companion=view.transaction_companion,
            )
            stored = commit_result.committed_events[0]
            write_outcome = EventBatchCommitOutcome(
                status="full",
                deadline_monotonic=deadline_budget.ordinary_deadline_monotonic,
                result=commit_result,
            )
            suspension_reconciliation_required = commit_result.reconciliation_required
            suspension_publication_unavailable = (
                commit_result.publication_status == "unavailable"
                or bool(commit_result.publication_errors)
            )
        except EventPublicationAfterCommitError as exc:
            # The suspension fact is already canonical.  Preserve/confirm its
            # process-local lease owner and surface the committed event rather
            # than turning a hook failure into an unrecoverable pending record.
            stored = next(
                event
                for event in exc.result.committed_events
                if event.id == suspension_event.id
            )
            suspension_publication_unavailable = True
            write_outcome = EventBatchCommitOutcome(
                status="full",
                deadline_monotonic=deadline_budget.ordinary_deadline_monotonic,
                result=exc.result,
            )
        except BaseException as suspension_error:
            outcome = self._run_ledger.resolved_write_outcome(suspension_error)
            receipt = terminal_registry.confirm_stable_candidate_write(
                owner_identity=candidate_owner,
                outcome=outcome,
            )
            transition = mcp_port.confirm_suspension_commit(
                pending_handle=pending_handle,
                commit_receipt=receipt,
            )
            terminal_registry.accept_physical_owner_handoff(transition.handoff_receipt)
            if outcome.status == "unknown":
                raise
            if outcome.status == "none":
                state.pending_tool_calls = original_pending_tool_calls
                state.pending_interaction_kind = original_pending_kind
                state.pending_interaction_payload = original_pending_payload
                state.pending_interaction_source_event_reference = (
                    original_pending_source_reference
                )
                state.pending_interaction_source_event_candidate = (
                    original_pending_source_candidate
                )
                state.status = original_status
                state.stop_reason = original_stop_reason
                state.last_transition = original_transition
                async for _event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=suspended.tool_call_id,
                    tool_call_name=suspended.tool_name,
                    output=(
                        "Tool suspension could not be durably recorded; "
                        "the admitted call was terminated fail-closed."
                    ),
                    result_state=ToolResultState.ERROR,
                    tool_observation_timing_seed=(
                        dict(payload.get("tool_observation_timing_seed") or {}) or None
                    ),
                    tool_arguments={},
                    rollout_reservation=reservation,
                ):
                    pass
                raise suspension_error
            stored = next(
                event
                for event in outcome.committed_events
                if event.id == suspension_event.id
            )
            raise
        receipt = terminal_registry.confirm_stable_candidate_write(
            owner_identity=candidate_owner,
            outcome=write_outcome,
        )
        transition = mcp_port.confirm_suspension_commit(
            pending_handle=pending_handle,
            commit_receipt=receipt,
        )
        terminal_registry.accept_physical_owner_handoff(transition.handoff_receipt)
        if suspension_reconciliation_required:
            raise RuntimeError(
                "tool suspension committed without a healthy reducer fold"
            )
        payload["source_suspension_event_reference"] = event_reference_from_stored(
            stored,
            runtime_session_id=self._run_identity.runtime_session_id,
        )
        state.pending_interaction_source_event_reference = payload[
            "source_suspension_event_reference"
        ]
        state.pending_interaction_source_event_candidate = freeze_event_write_candidate(
            stored.model_copy(update={"sequence": None})
        )
        yield stored
        if suspension_publication_unavailable:
            finalization = self._require_run_finalization_owner(state)
            finalization.mcp_publication_closure_reason = (
                "suspension_publication_unavailable"
            )
            finalization.publication_deadline_budget = deadline_budget
            closure_events: tuple[AgentEvent, ...] = ()
            terminal_key = (state.run_id, suspended.tool_call_id)
            self._mcp_terminal_commit_outcomes[terminal_key] = "not_attempted"
            self._mcp_terminal_pending_handles[terminal_key] = pending_handle
            try:
                async for event in self._terminalize_pending_mcp_for_abort(
                    state,
                    reason=AbortKind.HOST_TEARDOWN,
                ):
                    closure_events = (*closure_events, event)
                    yield event
            finally:
                self._mcp_terminal_commit_outcomes.pop(terminal_key, None)
                self._mcp_terminal_pending_handles.pop(terminal_key, None)
            if finalization.publication_latched_termination is None:
                self._install_mcp_publication_latched_termination(
                    state,
                    committed_events=(stored, *closure_events),
                    reason="mcp_active_interaction_publication_unavailable",
                    deadline_budget=deadline_budget,
                )
            state.status = LoopStatus.ABORTED
            state.stop_reason = RunStopReason.ABORTED
            state.error_message = None
            state.pending_tool_calls = []
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.abort_kind = AbortKind.HOST_TEARDOWN
            return

    def _recover_or_fail_model(self, state: RunActivationWorkingState) -> bool:
        state.consecutive_model_failures += 1
        state.in_run_recovery = InRunRecoveryState(
            cause=InRunRecoveryCause.MODEL_FAILURE,
            consecutive_failures=state.consecutive_model_failures,
        )
        if (
            state.consecutive_model_failures
            > self.budget.max_consecutive_model_failures
        ):
            state.status = LoopStatus.FAILED
            state.stop_reason = RunStopReason.MODEL_ERROR
            state.error_message = "model error budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.transition(LoopTransition.CONTINUE_AFTER_RECOVERY)
        return True

    def _event_context(self, state: RunActivationWorkingState) -> EventContext:
        return EventContext(
            run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id
        )


def _next_model_call_index(state: RunActivationWorkingState) -> int:
    value = state.model_tool_progress.model_call_index + 1
    state.model_tool_progress.model_call_index = value
    return value


def _pending_interaction_authority_fingerprint(
    agent: AgentRuntime,
    state: RunActivationWorkingState,
) -> str | None:
    registry = agent.run_execution_registry
    if registry is None:
        return None
    owner = registry.get(state.run_id)
    if owner is None:
        return None
    authority = getattr(owner.suspension_slot, "authority", None)
    identity = getattr(authority, "identity", None)
    fingerprint = getattr(identity, "interaction_fingerprint", None)
    return fingerprint if isinstance(fingerprint, str) else None


def _context_budget_pressure_is_recoverable(exc: ContextBudgetExceeded) -> bool:
    del exc
    # L1 removes aggregate render pressure. L2 installs the deterministic
    # projection rewrite owner; until then an overall hard-budget failure is
    # not recoverable by the legacy cross-run compactor.
    return False


def _compiled_section_included(compiled_context, section_id: str) -> bool:
    return any(
        section.id == section_id and section.included
        for section in compiled_context.sections
    )


def _compiled_source_included(compiled_context, source_id: str) -> bool:
    return any(
        section.source_id == source_id and section.included
        for section in compiled_context.sections
    )


def _tool_call_in_flight(state: RunActivationWorkingState) -> bool:
    authority = state.execution_resources.capability_execution_borrow_authority
    tracker = getattr(authority, "tracker", None)
    if tracker is None:
        return False
    return bool(
        tracker.active_parent_tool_call_borrows
        or tracker.active_child_tool_call_borrows
    )


def _active_projection_rewrite_refs(
    *,
    prepared_context_input,
    window_id: str,
    projection_generation: int,
):
    refs = []
    for frozen in prepared_context_input.authority_slice.events:
        if frozen.event_type != EventType.CONTEXT_PROJECTION_REWRITE_PAGE:
            continue
        event = decode_raw_stored_event_envelope(frozen, DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, ContextProjectionRewritePageEvent):
            raise RuntimeError("projection rewrite event decoder mismatch")
        if (
            event.window_id == window_id
            and event.to_projection_generation <= projection_generation
        ):
            refs.append(
                frozen.to_reference(
                    prepared_context_input.authority_slice.runtime_session_id
                )
            )
    return tuple(refs)


def _resolve_prepared_long_horizon_context_facts(*, prepared_context_input):
    """Expose the reducer snapshot frozen with the compile authority."""

    return (
        prepared_context_input.active_window,
        prepared_context_input.projection_state,
        prepared_context_input.rollout_state,
    )


def _empty_context_budget_report(resolved_call) -> ContextBudgetReportEvent:
    target = resolved_call.target
    return ContextBudgetReportEvent(
        target_fingerprint=target.fact.target_fingerprint,
        resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
        measurement_stage="tool_result_render",
        total_context_tokens=target.limits.total_context_tokens,
        max_input_tokens=target.limits.max_input_tokens,
        max_output_tokens=target.limits.max_output_tokens,
        effective_output_tokens=target.context_budget.effective_output_tokens,
        safety_margin_tokens=target.context_budget.safety_margin_tokens,
        input_budget_tokens=target.context_budget.input_budget_tokens,
        estimator=target.token_estimator.fact,
    )


def _context_manifest_input_failure(
    *,
    snapshot,
    manifest,
    candidate,
    error: BaseException,
) -> ContextCompileInputFailureFact:
    if isinstance(error, ContextInputManifestConfirmedAbsent):
        outcome = "confirmed_absent"
        reason = ContextInputFailureReasonCode.MANIFEST_CONFIRMED_ABSENT
    elif isinstance(error, ContextInputManifestWriteConflict):
        outcome = "conflict"
        reason = ContextInputFailureReasonCode.MANIFEST_CONFLICT
    elif isinstance(error, ContextInputManifestWriteDeadlineExceeded):
        outcome = "deadline_exceeded"
        reason = ContextInputFailureReasonCode.MANIFEST_DEADLINE_EXCEEDED
    else:
        outcome = "outcome_unknown"
        reason = ContextInputFailureReasonCode.MANIFEST_OUTCOME_UNKNOWN
    fact = snapshot.invocation.fact
    available = tuple(
        sorted(
            (
                (
                    "prepared_candidate_set",
                    snapshot.prepared_candidates.candidate_set_fingerprint,
                ),
                ("snapshot_fact", fact.snapshot_fact_fingerprint),
                (
                    "tool_result_render_input",
                    snapshot.prepared_tool_results.render_input_fingerprint,
                ),
                (
                    "transcript",
                    snapshot.normalized_transcript.transcript.transcript_fingerprint,
                ),
            )
        )
    )
    return ContextCompileInputFailureFact(
        failure_stage="input_manifest_write",
        context_id=fact.identity.context_id,
        resolved_model_call_id=fact.resolved_model_call.resolved_model_call_id,
        model_call_index=fact.identity.model_call_index,
        compile_attempt_index=fact.identity.compile_attempt_index,
        context_retry_index=fact.identity.context_retry_index,
        snapshot_id=fact.identity.snapshot_id,
        source_through_sequence=fact.identity.source_through_sequence,
        available_component_fingerprints=available,
        input_aggregate_fingerprint=manifest.input_aggregate_fingerprint,
        manifest_candidate_artifact_id=candidate.artifact_id,
        manifest_candidate_content_fingerprint=candidate.content_fingerprint,
        manifest_candidate_metadata_fingerprint=candidate.metadata_fingerprint,
        manifest_write_outcome=outcome,
        reason_code=reason,
    )


def _context_pre_manifest_input_failure(
    *,
    error: ContextInputPreparationError,
    context_id: str,
    resolved_model_call_id: str,
    model_call_index: int,
    compile_attempt_index: int,
    context_retry_index: int,
) -> ContextCompileInputFailureFact:
    return ContextCompileInputFailureFact(
        failure_stage=error.failure_stage,
        context_id=context_id,
        resolved_model_call_id=resolved_model_call_id,
        model_call_index=model_call_index,
        compile_attempt_index=compile_attempt_index,
        context_retry_index=context_retry_index,
        snapshot_id=error.snapshot_id,
        source_through_sequence=error.source_through_sequence,
        available_component_fingerprints=(error.available_component_fingerprints),
        input_aggregate_fingerprint=None,
        manifest_candidate_artifact_id=None,
        manifest_candidate_content_fingerprint=None,
        manifest_candidate_metadata_fingerprint=None,
        manifest_write_outcome="not_attempted",
        reason_code=error.reason_code,
    )


def _context_manifest_preparation_error(
    prepared_context_input,
    *,
    cause: Exception,
) -> ContextInputPreparationError:
    fact = prepared_context_input.invocation.fact
    available = tuple(
        sorted(
            (
                (
                    "prepared_candidate_set",
                    prepared_context_input.prepared_candidates.candidate_set_fingerprint,
                ),
                ("snapshot_fact", fact.snapshot_fact_fingerprint),
                (
                    "tool_result_render_input",
                    prepared_context_input.prepared_tool_results.render_input_fingerprint,
                ),
                (
                    "transcript",
                    prepared_context_input.normalized_transcript.transcript.transcript_fingerprint,
                ),
            )
        )
    )
    return ContextInputPreparationError(
        failure_stage="candidate_materialization",
        reason_code=ContextInputFailureReasonCode.CANDIDATE_INVALID,
        snapshot_id=fact.identity.snapshot_id,
        source_through_sequence=fact.identity.source_through_sequence,
        available_component_fingerprints=available,
        cause=cause,
    )


def _context_finalization_preparation_error(
    prepared_context_input,
    *,
    failure_stage: str,
    reason_code: ContextInputFailureReasonCode,
    cause: Exception,
) -> ContextInputPreparationError:
    build_input = prepared_context_input.snapshot_build_input
    available = tuple(
        sorted(
            (
                (
                    "prepared_candidate_set",
                    prepared_context_input.prepared_candidates.candidate_set_fingerprint,
                ),
                (
                    "snapshot_draft",
                    context_fingerprint("context-snapshot-draft:v1", build_input),
                ),
                (
                    "tool_result_render_input",
                    prepared_context_input.prepared_tool_results.render_input_fingerprint,
                ),
                (
                    "transcript",
                    prepared_context_input.normalized_transcript.transcript.transcript_fingerprint,
                ),
            )
        )
    )
    return ContextInputPreparationError(
        failure_stage=failure_stage,
        reason_code=reason_code,
        snapshot_id=build_input.identity.snapshot_id,
        source_through_sequence=build_input.identity.source_through_sequence,
        available_component_fingerprints=available,
        cause=cause,
    )


def _context_stage_preparation_error(
    *,
    prepared_context_input,
    failure_stage: ContextCompileFailureStage,
    reason_code: ContextInputFailureReasonCode,
    cause: Exception,
) -> ContextInputPreparationError:
    if prepared_context_input is None:
        return ContextInputPreparationError(
            failure_stage=failure_stage.value,
            reason_code=reason_code,
            snapshot_id=None,
            source_through_sequence=None,
            available_component_fingerprints=(),
            cause=cause,
        )
    return _context_finalization_preparation_error(
        prepared_context_input,
        failure_stage=failure_stage.value,
        reason_code=reason_code,
        cause=cause,
    )


def _long_horizon_preparation_error(
    *,
    prepared_context_input,
    reason_code: ContextInputFailureReasonCode,
    message: str,
) -> ContextInputPreparationError:
    cause = RuntimeError(message)
    if prepared_context_input is None:
        return ContextInputPreparationError(
            failure_stage="long_horizon_preparation",
            reason_code=reason_code,
            snapshot_id=None,
            source_through_sequence=None,
            available_component_fingerprints=(),
            cause=cause,
        )
    return _context_finalization_preparation_error(
        prepared_context_input,
        failure_stage="long_horizon_preparation",
        reason_code=reason_code,
        cause=cause,
    )


def _context_budget_input_failure(
    *,
    prepared_context_input,
    context_id: str,
    resolved_model_call_id: str,
    model_call_index: int,
    compile_attempt_index: int,
    context_retry_index: int,
) -> ContextCompileInputFailureFact:
    build_input = prepared_context_input.snapshot_build_input
    error = _context_finalization_preparation_error(
        prepared_context_input,
        failure_stage="context_budget",
        reason_code=ContextInputFailureReasonCode.CONTEXT_BUDGET_EXCEEDED,
        cause=RuntimeError("resolved context input exceeds its model budget"),
    )
    return _context_pre_manifest_input_failure(
        error=error,
        context_id=context_id,
        resolved_model_call_id=resolved_model_call_id,
        model_call_index=model_call_index,
        compile_attempt_index=compile_attempt_index,
        context_retry_index=context_retry_index,
    ).model_copy(update={"snapshot_id": build_input.identity.snapshot_id})


def _validate_prepared_context_input(
    *, prepared_context_input, compiled_context
) -> None:
    """Fail closed when the prepared immutable input disagrees with compiled output."""

    fact = prepared_context_input.invocation.fact
    if fact.identity.context_id != compiled_context.context_id:
        raise RuntimeError("immutable context snapshot context ID drift")
    if fact.resolved_model_call != compiled_context.resolved_model_call:
        raise RuntimeError("immutable context snapshot resolved-call drift")
    compiled_names = tuple(sorted(item.name for item in compiled_context.tool_specs))
    frozen_names = tuple(item.model_tool_name for item in fact.tool_specs)
    if compiled_names != frozen_names:
        raise RuntimeError("immutable context snapshot tool-spec drift")
    compiled_descriptor_ids = tuple(
        sorted(
            item.descriptor_id
            for item in compiled_context.tool_specs
            if item.descriptor_id is not None
        )
    )
    frozen_descriptor_ids = tuple(
        sorted(item.descriptor_id for item in fact.tool_specs)
    )
    if compiled_descriptor_ids != frozen_descriptor_ids:
        raise RuntimeError("immutable context snapshot descriptor attribution drift")
    normalized = prepared_context_input.normalized_transcript
    if normalized.transcript.current_user_anchor != (
        fact.current_user_message.message_id
    ):
        raise RuntimeError("normalized transcript current-user anchor drift")
    old_result_ids = tuple(
        str(decision.get("tool_call_id"))
        for decision in compiled_context.tool_result_render_decisions
        if isinstance(decision, dict) and decision.get("tool_call_id")
    )
    normalized_result_ids = tuple(
        unit.tool_call_id for unit in normalized.tool_result_units
    )
    if old_result_ids != normalized_result_ids:
        raise RuntimeError(
            "normalized transcript tool-result ordering drift: "
            f"old={old_result_ids!r} normalized={normalized_result_ids!r}"
        )
    if (
        prepared_context_input.prepared_tool_results.resolved_policy.basis
        != fact.compile_policy.tool_result_basis
    ):
        raise RuntimeError("prepared tool-result policy drift")
    compiled_section_ids = {section.id for section in compiled_context.sections}
    missing_candidate_sections = tuple(
        entry.candidate.source_instance_id
        for entry in prepared_context_input.prepared_candidates.entries
        if entry.candidate.source_instance_id not in compiled_section_ids
    )
    if missing_candidate_sections:
        raise RuntimeError(
            "typed context candidates are absent from old compiler sections: "
            f"{missing_candidate_sections!r}"
        )


def _bind_compiled_context_to_provider_input(
    *,
    compiled_context: CompiledContext,
    provider_input_start_bundle,
    resolved_call: ResolvedModelCall,
) -> CompiledContext:
    """Make the canonical resident carrier the final budget/payload truth."""

    context = provider_input_start_bundle.carrier.to_llm_context(
        replace(
            compiled_context.llm_context,
            resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
            target_fingerprint=resolved_call.target.fact.target_fingerprint,
        )
    )
    estimate = resolved_call.target.token_estimator.estimate_context(context)
    scopes = tuple(
        "transcript"
        if unit.attribution.semantic.unit_kind == "transcript_message"
        else "non_transcript"
        for unit in provider_input_start_bundle.resident.units
        if unit.attribution.semantic.unit_kind != "tool_catalog"
        and not (
            unit.attribution.semantic.unit_kind == "context_source"
            and getattr(unit.canonical_provider_fragment, "role", None) == "system"
        )
    )
    return bind_compiled_context_to_provider_carrier(
        compiled_context=compiled_context,
        provider_context=context,
        token_estimate=estimate,
        message_budget_scopes=scopes,
    )


def _run_start_for_id(long_horizon_store, *, run_id: str) -> RunStartEvent:
    start = long_horizon_store.run_start(run_id)
    if start is None:
        raise RuntimeError("run terminalization requires exactly one RunStart")
    return start


def _tool_timing_start_event_id(seed: dict[str, Any] | None) -> str | None:
    if not seed:
        return None
    value = seed.get("start_event_id")
    return value if isinstance(value, str) and value else None


def _pending_tool_result_start_event_id(payload: dict[str, Any]) -> str | None:
    seed = payload.get("tool_observation_timing_seed")
    return _tool_timing_start_event_id(seed if isinstance(seed, dict) else None)


def _is_exact_run_terminal_batch(
    stored: tuple[AgentEvent, ...],
    candidates: tuple[AgentEvent, ...],
) -> bool:
    return (
        len(stored) == len(candidates)
        and tuple(event.id for event in stored)
        == tuple(event.id for event in candidates)
        and isinstance(stored[-1], RunEndEvent)
    )


def _context_window_terminal_reason(
    *,
    terminalization_kind: RunTerminalizationKind,
    status: LoopStatus,
) -> ContextWindowCloseReason:
    if terminalization_kind is RunTerminalizationKind.NORMAL:
        return ContextWindowCloseReason.RUN_FINISHED
    if terminalization_kind is RunTerminalizationKind.USER_STOP:
        return ContextWindowCloseReason.USER_STOP
    if terminalization_kind is RunTerminalizationKind.HOST_TEARDOWN:
        return ContextWindowCloseReason.HOST_TEARDOWN
    if terminalization_kind is RunTerminalizationKind.RECOVERED_INTERRUPTED:
        return ContextWindowCloseReason.RECOVERED_INTERRUPTED
    if status is not LoopStatus.FAILED:
        raise RuntimeError("execution failure close requires failed loop status")
    return ContextWindowCloseReason.RUN_FAILED


def _tool_block_arguments_for_semantics(block: ToolCallBlock) -> dict[str, Any]:
    try:
        value = json.loads(block.input or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _is_overridden_hook(instance: object, name: str, base: type) -> bool:
    method = getattr(type(instance), name, None)
    return method is not None and method is not getattr(base, name, None)


def _pre_execution_failure_stage(
    reason_code: str,
) -> Literal[
    "malformed_arguments",
    "exposure_denied",
    "permission_denied",
    "policy_denied",
    "adapter_initialization",
]:
    if reason_code in {
        "malformed_arguments",
        "exposure_denied",
        "permission_denied",
        "adapter_initialization",
    }:
        return reason_code  # type: ignore[return-value]
    return "policy_denied"


def _optional_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _remove_plan_runtime_instructions(state: RunActivationWorkingState) -> None:
    state.messages = [
        message
        for message in state.messages
        if message.metadata.get("runtime_instruction")
        not in {"plan_entry", "plan_active", "plan_revision_required"}
    ]


def _tool_result_message_from_events(
    events: list[AgentEvent],
    tool_name: str,
    result_block,
) -> Msg:
    start = next(
        (
            event
            for event in events
            if isinstance(event, ToolResultStartEvent)
            and event.tool_call_id == result_block.id
        ),
        None,
    )
    end = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, ToolResultEndEvent)
            and event.tool_call_id == result_block.id
        ),
        None,
    )
    metadata: dict[str, object] = {}
    if start is not None or end is not None:
        metadata["source_timing"] = {
            "observed_at": end.created_at
            if end is not None
            else (start.created_at if start is not None else None),
            "source_started_at": start.created_at if start is not None else None,
            "source_ended_at": end.created_at if end is not None else None,
            "freshness": "current_tool_observation",
            "clock_source": "event_created_at",
        }
    if end is not None:
        timing = end.observation_timing.to_message_projection_payload()
        metadata["tool_observation_timing_by_call_id"] = {result_block.id: timing}
        metadata["tool_observation_timing"] = timing
    return Msg(
        role="tool_result",
        name=tool_name,
        id=f"tool-result-message:{result_block.id}",
        content=[result_block],
        metadata=metadata,
        created_at=start.created_at if start is not None else None,
        finished_at=end.created_at if end is not None else None,
    )


def _plan_exit_resolution_output(resolution: PlanExitResolution) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": resolution.decision,
        "user_feedback": resolution.user_feedback,
    }
    if resolution.decision == "revise":
        payload["next_required_action"] = (
            "Revise the plan according to user_feedback and call exit_plan again immediately. "
            "Do not answer with prose only. Ask another plan question only if a new material "
            "ambiguity genuinely blocks the revised plan."
        )
    return payload


def _memory_projection_kind(
    projection: dict[str, Any] | None,
) -> Literal["memory"]:
    return "memory"


def _typed_recalled_memory_entries(
    projection: dict[str, Any] | None,
) -> tuple[RecalledMemoryProjectionEntryFact, ...]:
    if projection is None:
        return ()
    raw_entries = projection.get("typed_recalled_entries")
    if not isinstance(raw_entries, list):
        raise ValueError("memory projection lacks typed recalled entries")
    entries: list[RecalledMemoryProjectionEntryFact] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError("typed recalled memory entry must be an object")
        text = raw.get("model_visible_text")
        memory_ids = raw.get("memory_ids")
        if not isinstance(text, str) or not isinstance(memory_ids, list):
            raise ValueError("typed recalled memory entry is malformed")
        ordered_ids = tuple(sorted({str(item) for item in memory_ids}))
        text_sha = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
        payload = {
            "entry_index": index,
            "memory_ids": ordered_ids,
            "model_visible_text": text,
            "text_utf8_sha256": text_sha,
        }
        entries.append(
            RecalledMemoryProjectionEntryFact(
                **payload,
                entry_semantic_fingerprint=sha256_fingerprint(
                    "recalled-memory-projection-entry:v1", payload
                ),
            )
        )
    return tuple(entries)


def _accepted_plan_artifact_id(run_id: str, exit_request_id: str) -> str:
    return f"artifact:plan:{_sanitize_artifact_part(run_id)}:{_sanitize_artifact_part(exit_request_id)}:accepted"


def _sanitize_artifact_part(value: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
        or "unknown"
    )


def _terminal_notification_process_id(
    event: TerminalProcessCompletedEvent
    | TerminalProcessMonitorObservationCommittedEvent,
) -> str:
    if isinstance(event, TerminalProcessCompletedEvent):
        cursor = event.completion_semantic.terminal_output_cursor
    else:
        output = event.observation.output_authority
        cursor = getattr(output, "end_cursor", None) or getattr(
            output, "terminal_cursor", None
        )
        if cursor is None:
            raise RuntimeError("terminal notification lacks an output cursor")
    return cursor.stream_identity.process_id


def _terminal_notification_is_terminal(
    event: TerminalProcessCompletedEvent
    | TerminalProcessMonitorObservationCommittedEvent,
) -> bool:
    return isinstance(event, TerminalProcessCompletedEvent) or (
        event.observation.observation_kind == "process_completed"
    )
