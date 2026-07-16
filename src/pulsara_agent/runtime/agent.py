"""Claude Code-like main loop built on RuntimeSession."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Literal
from uuid import uuid4

from pulsara_agent.capability.call_classifier import DefaultCapabilityCallClassifier
from pulsara_agent.capability.descriptor import CapabilityAvailability
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.provider import (
    CapabilityDescriptorSnapshotOutput,
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.render import (
    render_active_skill_prompt,
    render_catalog_prompt,
)
from pulsara_agent.capability.runtime import (
    CapabilityRuntime,
)
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.capability import (
    CapabilityExecutionSurfaceIdentityFact,
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    CapabilityGateDecisionEvent,
    ChildRolloutSubaccountClosedEvent,
    ConfirmResult,
    ContextCompiledEvent,
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
    CustomEvent,
    EventContext,
    EventType,
    ModelCallRejectedEvent,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    ProjectionRequestedEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    ToolResultEndEvent,
    ToolResultDataDeltaEvent,
    ToolExecutionSuspendedEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.event.events import utc_now
from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.control import (
    ModelCallControlResolutionError,
    RunModelCallControlOwner,
)
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import (
    Msg,
    SystemMsg,
    ToolCallBlock,
    ToolCallState,
    ToolResultState,
)
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.long_horizon import ToolActionClassificationFact
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.runtime.context_engine.types import ContextBudgetExceeded
from pulsara_agent.runtime.tool_action import (
    ToolActionClassifierRegistry,
    default_tool_action_classifier_registry,
)
from pulsara_agent.runtime.approval import ApprovalResolution
from pulsara_agent.runtime.compaction.inline import (
    MidTurnCompactionResult,
    NoopRuntimeContextCompactor,
    RuntimeContextCompactorProtocol,
)
from pulsara_agent.runtime.hooks import (
    MemoryHooks,
    NoopMemoryHooks,
    ToolResultPersistenceHook,
)
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
from pulsara_agent.runtime.execution_handles import BoundaryExecutionHandles
from pulsara_agent.runtime.context_input.candidate import (
    DEFAULT_SYSTEM_PROMPT,
    ContextCandidateCollectionInput,
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
    prepare_live_context_snapshot,
    prepare_live_transcript_projection,
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
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    SubagentRunEntryFact,
    text_sha256,
)
from pulsara_agent.primitives.run_boundary import RunExecutionActivationFact
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutUsageHandoffFact,
    ContextWindowCloseReason,
    RolloutBudgetBucket,
    RolloutPhase,
    RolloutReservationFact,
    RolloutReservationReferenceFact,
    build_child_rollout_usage_handoff,
    calculate_model_call_reservation,
    default_long_horizon_context_policy,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.subagent import (
    ChildNativeTerminalReferenceFact,
    build_child_result_render_policy,
    validate_child_render_policy_against_budget,
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
    PLAN_ACTIVE_INSTRUCTION,
    McpInputRequiredInteractionResolution,
    PlanExitResolution,
    PlanInteractionResolution,
    PlanQuestionResolution,
    PlanWorkflowState,
    normalize_plan_question_options,
    plan_workflow_state_fact,
)
from pulsara_agent.runtime.mcp.types import (
    MAX_MCP_INPUT_REQUIRED_ROUNDS,
    McpBindingIdentity,
    McpInputRequestDTO,
    McpInputRequiredResolution,
    redact_mcp_error_message,
)
from pulsara_agent.runtime.recovery import (
    AbortKind,
    InRunRecoveryCause,
    InRunRecoveryState,
)
from pulsara_agent.runtime.session import (
    EventPublicationAfterCommitError,
    RuntimeSession,
)
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CapabilityResolveBasis,
    CommittedRunEntry,
    PreparedSubagentRunEntry,
    RunWorkingSet,
)
from pulsara_agent.runtime.long_horizon.rollout import apply_rollout_event
from pulsara_agent.runtime.long_horizon.coordinator import (
    allowed_action_classes_for_phase,
    build_rollout_phase_transition_event,
    plan_root_model_admission,
    plan_root_tool_admission,
    rollout_bucket_remaining,
)
from pulsara_agent.runtime.long_horizon.window_compaction_service import (
    ContextWindowCompactionService,
    WindowCompactionRequest,
)
from pulsara_agent.runtime.long_horizon.accounting import (
    child_settlement_aggregate,
    resolve_run_rollout_binding,
)
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
    materialize_observation_rollup,
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
from pulsara_agent.runtime.tool_execution import (
    RuntimeSessionToolExecutionEventCommitPort,
    build_tool_result_terminal_event,
)
from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate
from pulsara_agent.runtime.long_horizon.run_contract import (
    build_child_rollout_subaccount,
    prepare_child_long_horizon_run,
    prepare_child_rollout_reservation,
)
from pulsara_agent.runtime.state import (
    LoopBudget,
    LoopState,
    LoopStatus,
    LoopTransition,
)
from pulsara_agent.runtime.subagent import (
    HydratedSubagentRunView,
    InMemoryEventLogLocator,
    PostgresEventLogLocator,
    SubagentRuntime,
    SubagentRuntimeError,
)
from pulsara_agent.runtime.subagent.run_entry import SubagentRunEntryDriver
from pulsara_agent.runtime.tool_taxonomy import PLAN_WORKFLOW_TOOL_NAMES
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
from pulsara_agent.tools import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolExecutor,
    ToolRuntimeContext,
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
_TERMINAL_CAPABILITY_CONTEXT_TOOL_NAMES = frozenset({"terminal", "terminal_process"})
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
class _ProfileFilteredExecutionSurfaceProvider:
    provider: Any
    allowed_tool_names: frozenset[str]
    allowed_descriptor_ids: frozenset[str]

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "provider_id", "profile-filtered"))

    def snapshot_descriptors(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
    ) -> CapabilityDescriptorSnapshotOutput:
        snapshot = self.provider.snapshot_descriptors
        output = snapshot(context)
        return CapabilityDescriptorSnapshotOutput(
            descriptors=tuple(
                descriptor
                for descriptor in output.descriptors
                if descriptor.name in self.allowed_tool_names
                or descriptor.id in self.allowed_descriptor_ids
            ),
            diagnostics=output.diagnostics,
        )


@dataclass(frozen=True, slots=True)
class _ProfileFilteredProjectionProvider:
    provider: Any
    allowed_skill_names: frozenset[str]

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "provider_id", "profile-filtered"))

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface: CapabilityExecutionSurfaceIdentityFact,
    ) -> CapabilityProjectionOutput:
        resolve_projection = self.provider.resolve_projection
        output = resolve_projection(context, execution_surface=execution_surface)
        catalog_entries = tuple(
            entry
            for entry in output.catalog_entries
            if entry.name in self.allowed_skill_names
        )
        active_injections = tuple(
            injection
            for injection in output.active_injections
            if injection.name in self.allowed_skill_names
        )
        catalog_rendered = render_catalog_prompt(catalog_entries)
        active_rendered = render_active_skill_prompt(active_injections)
        return CapabilityProjectionOutput(
            catalog_entries=catalog_entries,
            active_injections=active_injections,
            diagnostics=(
                *output.diagnostics,
                *catalog_rendered.diagnostics,
                *active_rendered.diagnostics,
            ),
            catalog_prompt=catalog_rendered.text,
            active_skill_prompt=active_rendered.text,
            catalog_rendered=catalog_rendered,
            active_skill_rendered=active_rendered,
        )


def _subagent_event_log_backend(runtime_session: RuntimeSession):
    parent_log = runtime_session.event_log
    if isinstance(parent_log, InMemoryEventLog):
        locator = InMemoryEventLogLocator()

        def factory(runtime_session_id: str) -> EventLog:
            event_log = InMemoryEventLog()
            locator.register(runtime_session_id, event_log)
            return event_log

        return factory, locator
    if isinstance(parent_log, PostgresEventLog):
        locator = PostgresEventLogLocator(
            dsn=parent_log.dsn,
            workspace_root=runtime_session.workspace_root,
        )
        return locator.event_log_for_runtime_session, locator
    raise TypeError(
        "SubagentRuntime requires a supported EventLog backend "
        f"(got {type(parent_log).__name__})"
    )


def _profile_filtered_capability_runtime(
    parent: CapabilityRuntime, profile: Any
) -> CapabilityRuntime:
    allowed_tool_names = frozenset(getattr(profile, "allowed_tool_names", ()) or ())
    allowed_descriptor_ids = frozenset(
        getattr(profile, "allowed_descriptor_ids", ()) or ()
    )
    allowed_skill_names = frozenset(getattr(profile, "allowed_skill_names", ()) or ())
    if (
        not allowed_tool_names
        and not allowed_descriptor_ids
        and not allowed_skill_names
    ):
        return CapabilityRuntime(providers=())
    filtered: list[Any] = []
    for provider in parent.providers:
        if hasattr(provider, "snapshot_descriptors"):
            filtered.append(
                _ProfileFilteredExecutionSurfaceProvider(
                    provider=provider,
                    allowed_tool_names=allowed_tool_names,
                    allowed_descriptor_ids=allowed_descriptor_ids,
                )
            )
        if hasattr(provider, "resolve_projection"):
            filtered.append(
                _ProfileFilteredProjectionProvider(
                    provider=provider,
                    allowed_skill_names=allowed_skill_names,
                )
            )
    return CapabilityRuntime(providers=tuple(filtered))


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


def _mcp_input_requests_from_payload(value: object) -> tuple[McpInputRequestDTO, ...]:
    if not isinstance(value, list):
        return ()
    requests: list[McpInputRequestDTO] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        method = str(item.get("method") or "")
        params = item.get("params")
        if not key or not method:
            continue
        requests.append(
            McpInputRequestDTO(
                key=key,
                method=method,
                params=dict(params) if isinstance(params, dict) else {},
            )
        )
    return tuple(requests)


def _mcp_resume_binding_changed(payload: dict[str, Any], tool: object) -> bool:
    raw_identity = payload.get("mcp_binding_identity")
    current_identity = getattr(tool, "binding_identity", None)
    if not isinstance(raw_identity, dict) or not isinstance(
        current_identity,
        McpBindingIdentity,
    ):
        return True
    try:
        pending_identity = McpBindingIdentity(
            server_id=str(raw_identity["server_id"]),
            slot_id=str(raw_identity["slot_id"]),
            snapshot_id=str(raw_identity["snapshot_id"]),
            discovery_generation=int(raw_identity["discovery_generation"]),
        )
    except (KeyError, TypeError, ValueError):
        return True
    return pending_identity != current_identity


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
    state: LoopState
    messages: list[Msg]
    final_text: str
    error_message: str | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        llm_runtime: LLMRuntime,
        memory_hooks: MemoryHooks | None = None,
        tool_result_persistence_hook: ToolResultPersistenceHook | None = None,
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
    ) -> None:
        if capability_runtime is None:
            raise ValueError("AgentRuntime requires an explicit CapabilityRuntime")
        self.runtime_session = runtime_session
        self.llm_runtime = llm_runtime
        self.memory_hooks = memory_hooks or NoopMemoryHooks()
        self.tool_result_persistence_hook = tool_result_persistence_hook
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
        self._is_subagent_child = isinstance(
            runtime_session.default_event_metadata.get("subagent"), dict
        )
        self._subagent_parent_features_enabled = (
            enable_subagents and not self._is_subagent_child
        )
        self.subagent_runtime = subagent_runtime
        if self._subagent_parent_features_enabled and self.subagent_runtime is None:
            existing_subagent_runtime = runtime_session.subagent_runtime
            if isinstance(existing_subagent_runtime, SubagentRuntime):
                self.subagent_runtime = existing_subagent_runtime
                self.subagent_runtime.bind_child_runner(self._run_child_agent)
            else:
                child_event_log_factory, event_log_locator = (
                    _subagent_event_log_backend(runtime_session)
                )
                self.subagent_runtime = SubagentRuntime(
                    parent_runtime_session=runtime_session,
                    child_event_log_factory=child_event_log_factory,
                    event_log_locator=event_log_locator,
                    child_runner=self._run_child_agent,
                )
        if self._subagent_parent_features_enabled and self.subagent_runtime is not None:
            self.subagent_runtime.bind_rollout_admission(
                self._prepare_child_rollout_admission_events
            )
            self.subagent_runtime.bind_rollout_terminal_augmenter(
                self._prepare_child_rollout_terminal_events
            )
        self.runtime_session.subagent_runtime = self.subagent_runtime
        self._subagent_dangling_repair_done = False
        self._mcp_terminal_commit_outcomes: dict[
            tuple[str, str],
            Literal["not_attempted", "attempting", "none", "full", "untrusted"],
        ] = {}
        self.tool_action_classifier_registry: ToolActionClassifierRegistry = (
            default_tool_action_classifier_registry()
        )
        self.rollout_budget_feasibility_report: (
            ProductionRolloutBudgetFeasibilityReport | None
        ) = None
        self.observation_rollup_renderer_registry = (
            default_observation_rollup_renderer_registry()
        )
        existing_window_compactor = runtime_session.window_compaction_service
        if existing_window_compactor is None:
            existing_window_compactor = ContextWindowCompactionService(
                runtime_session=runtime_session,
                llm_runtime=llm_runtime,
            )
            runtime_session.window_compaction_service = existing_window_compactor
        elif not isinstance(
            existing_window_compactor, ContextWindowCompactionService
        ):
            raise TypeError(
                "RuntimeSession carries an incompatible window compaction service"
            )
        self.window_compaction_service = existing_window_compactor
        self.tool_executor = runtime_session.create_tool_executor(
            memory_proposal_sink=getattr(
                self.memory_hooks, "memory_proposal_sink", None
            ),
            memory_recall_service=getattr(self.memory_hooks, "recall", None),
            memory_query=getattr(self.memory_hooks, "memory_query", None),
            graph_id=getattr(self.memory_hooks, "graph_id", None),
            memory_read_scopes=getattr(self.memory_hooks, "read_scopes", None),
            permission_state=self._permission_state,
        )

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

    def refresh_capability_runtime(self, capability_runtime: CapabilityRuntime) -> None:
        """Replace per-turn capability facts and rebuild the executor registry.

        MCP descriptors and execution bindings are session-owned and may change
        after a reconnect/backoff sync.  Rebuilding here keeps the model-facing
        exposure plan and the executable ToolRegistry on the same snapshot.
        """
        if capability_runtime is None:
            raise ValueError("AgentRuntime requires an explicit CapabilityRuntime")
        self.capability_runtime = capability_runtime
        self.tool_executor = self.runtime_session.create_tool_executor(
            memory_proposal_sink=getattr(
                self.memory_hooks, "memory_proposal_sink", None
            ),
            memory_recall_service=getattr(self.memory_hooks, "recall", None),
            memory_query=getattr(self.memory_hooks, "memory_query", None),
            graph_id=getattr(self.memory_hooks, "graph_id", None),
            memory_read_scopes=getattr(self.memory_hooks, "read_scopes", None),
            permission_state=self._permission_state,
        )

    @property
    def permission_policy(self) -> EffectivePermissionPolicy:
        return self._permission_state.policy

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
        state: LoopState,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_approval(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_approval(
        self,
        state: LoopState,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_approval_resolution(state, resolution):
            yield event

    async def resume_after_plan_interaction(
        self,
        state: LoopState,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_plan_interaction(state, resolution):
            pass
        return self._run_result(state)

    async def resume_after_mcp_input_required(
        self,
        state: LoopState,
        resolution: McpInputRequiredInteractionResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_mcp_input_required(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_plan_interaction(
        self,
        state: LoopState,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_plan_interaction_resolution(state, resolution):
            yield event

    async def stream_after_mcp_input_required(
        self,
        state: LoopState,
        resolution: McpInputRequiredInteractionResolution,
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
        if commit_outcome_key in self._mcp_terminal_commit_outcomes:
            raise RuntimeError("MCP terminal result commit is already active")
        self._mcp_terminal_commit_outcomes[commit_outcome_key] = "not_attempted"
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
            if commit_outcome == "full":
                committed_result_events = self._committed_tool_result_events(
                    state,
                    tool_call_id=tool_call_id,
                    start_event_id=_pending_tool_result_start_event_id(
                        original_pending_payload
                    ),
                )
                if not committed_result_events:
                    self.runtime_session.latch_event_commit_outcome_unknown()
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
                try:
                    self._record_tool_result_events(
                        state,
                        stored_events=committed_result_events,
                        tool_call_id=tool_call_id,
                        tool_call_name=tool_name,
                    )
                finally:
                    await self._complete_mcp_pending_lease(resolution.interaction_id)
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

    async def abort_run(
        self,
        state: LoopState,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult:
        async for _event in self.stream_abort_run(state, reason=reason):
            pass
        return self._run_result(state)

    async def fail_committed_run(
        self,
        state: LoopState,
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

    async def retry_run_terminalization(self, state: LoopState) -> AgentRunResult:
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
        state: LoopState,
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
            async for event in self._terminalize_pending_mcp_for_abort(
                state,
                reason=reason,
            ):
                yield event
        elif state.pending_interaction_kind == "plan":
            async for event in self._terminalize_pending_plan_for_abort(
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

    async def _terminalize_pending_mcp_for_abort(
        self,
        state: LoopState,
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
        original_request = dict(payload.get("original_request") or {})
        timing_seed = dict(payload.get("tool_observation_timing_seed") or {})
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
                tool_arguments=dict(original_request.get("arguments") or {}),
                tool_observation_timing_seed=(
                    {**timing_seed, "resumed_at": utc_now()} if timing_seed else None
                ),
                rollout_reservation=reservation,
            ):
                yield event
        except EventPublicationAfterCommitError as exc:
            for event in exc.result.committed_events:
                yield event

    async def _terminalize_pending_plan_for_abort(
        self,
        state: LoopState,
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
            "ask_plan_question"
            if payload.get("kind") == "question"
            else "exit_plan"
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
        self.runtime_session.close()

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
            self.runtime_session.runtime_session_id
        }:
            raise RuntimeError("child rollout admission parent attribution mismatch")
        parent_run_id = ordered[0].parent_run_id
        parent_start = self.runtime_session.long_horizon_state_store.run_start(
            parent_run_id
        )
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
        account = self.runtime_session.long_horizon_state_store.rollout_account(
            account_id
        )
        state = self.runtime_session.long_horizon_state_store.rollout_state(account_id)
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
                    source_sequence=(
                        self.runtime_session.long_horizon_state_store.through_sequence
                    ),
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
                self.runtime_session,
                run_id=terminal.run_id,
            )
            account_id = parent_start.long_horizon.rollout_account_id
            account_state = self.runtime_session.long_horizon_state_store.rollout_state(
                account_id
            )
            if account_state is None:
                admission = self.runtime_session.event_log.get_by_id(
                    "subagent_rollout_budget_resolved:"
                    f"{terminal.subagent_run_id}"
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
            usage_status: Literal[
                "child_terminal_handoff", "child_not_started_zero"
            ]
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
                charged_milliunits = (
                    handoff.settlement_aggregate.charged_milliunits
                )

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
        child_log = self.subagent_runtime.event_log_locator.event_log_for_runtime_session(
            child_terminal_reference.child_runtime_session_id
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
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
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

    async def _run_child_agent(
        self,
        subagent_runtime: SubagentRuntime,
        run_view: HydratedSubagentRunView,
    ) -> None:
        run = run_view.fact
        capability_profile = run.capability_profile_value
        child_session = subagent_runtime.child_runtime_session(run.subagent_run_id)
        if capability_profile.permission_mode is None:
            raise ValueError(
                "child subagent run requires a preset child_profile permission mode"
            )
        child_permission_mode = parse_permission_mode(
            capability_profile.permission_mode
        )
        child_agent = AgentRuntime(
            runtime_session=child_session,
            llm_runtime=self.llm_runtime,
            memory_hooks=NoopMemoryHooks(),
            tool_result_persistence_hook=self.tool_result_persistence_hook,
            model_role=self.model_role,
            options=self.options,
            budget=self.budget,
            system_prompt=self.system_prompt,
            capability_runtime=_profile_filtered_capability_runtime(
                self.capability_runtime,
                capability_profile,
            ),
            memory_domain=None,
            workspace_kind=self.workspace_kind,
            permission_policy=preset_to_policy(child_permission_mode),
            context_compactor=NoopRuntimeContextCompactor(),
            subagent_runtime=subagent_runtime,
            enable_subagents=False,
        )
        child_agent.rollout_budget_feasibility_report = (
            self.rollout_budget_feasibility_report
        )
        # A child profile is an execution-surface boundary, not merely a
        # model-visible projection.  Keep descriptor and binding sets exact so
        # disallowed parent tools cannot remain as unowned executable bindings.
        child_agent.tool_executor.registry = (
            child_agent.tool_executor.registry.restricted_to(
                frozenset(capability_profile.allowed_tool_names)
            )
        )
        if not run_view.task_text_complete or run_view.task_text is None:
            raise ValueError(
                "child subagent run requires a fully hydrated task artifact"
            )
        if run.task_artifact_id is None:
            raise ValueError("child subagent run requires a durable task artifact")
        child_state = child_agent.new_state()
        child_target = child_agent.resolve_run_model_target()
        child_summarizer_target = child_agent.llm_runtime.resolve_target(
            role=ModelRole.FLASH
        )
        child_agent.require_prevalidated_rollout_pair(
            execution_profile_kind="subagent_child",
            execution_profile_id=run.profile_id or "general_worker",
            primary_target=child_target,
            summarizer_target=child_summarizer_target,
        )
        parent_run_start = self.runtime_session.long_horizon_state_store.run_start(
            run.parent_run_id
        )
        if parent_run_start is None:
            raise RuntimeError("child rollout contract requires one parent RunStart")
        resolved_budget_event = self.runtime_session.event_log.get_by_id(
            f"subagent_rollout_budget_resolved:{run.subagent_run_id}"
        )
        account_state = self.runtime_session.long_horizon_state_store.rollout_state(
            parent_run_start.long_horizon.rollout_account_id
        )
        reservations = (
            tuple(
                item
                for item in account_state.active_reservations
                if item.owner_kind == "subagent_run"
                and item.owner_id == run.subagent_run_id
            )
            if account_state is not None
            else ()
        )
        if not isinstance(
            resolved_budget_event, SubagentRolloutBudgetResolvedEvent
        ) or len(reservations) != 1:
            raise RuntimeError("child start lost its atomic rollout admission facts")
        reservation = reservations[0]
        stored_reservation = self.runtime_session.event_log.get_by_id(
            f"rollout_budget_reservation_created:{reservation.reservation_id}"
        )
        if not isinstance(stored_reservation, RolloutBudgetReservationCreatedEvent):
            raise RuntimeError("child start lost its rollout reservation fact")
        if (
            resolved_budget_event.budget_snapshot_event_id
            != run.provenance.created_event_id
            or stored_reservation.sequence is None
            or stored_reservation.reservation.reserved_milliunits
            != resolved_budget_event.resolved_budget.max_rollout_milliunits_per_child
            or resolved_budget_event.resolved_budget.child_primary_target_fingerprint
            != child_target.fact.target_fingerprint
            or resolved_budget_event.resolved_budget.child_summarizer_target_fingerprint
            != child_summarizer_target.fact.target_fingerprint
        ):
            raise RuntimeError("child rollout admission identity mismatch")
        reservation_reference = RolloutReservationReferenceFact(
            owner_runtime_session_id=self.runtime_session.runtime_session_id,
            reservation_id=stored_reservation.reservation.reservation_id,
            reservation_event_id=stored_reservation.id,
            reservation_sequence=stored_reservation.sequence,
            reservation_fingerprint=(
                stored_reservation.reservation.semantic_fingerprint
            ),
        )
        child_permission = child_agent._capture_run_permission_snapshot(child_state)
        child_run_start_id = f"run_start:subagent:{uuid4().hex}"
        child_long_horizon = prepare_child_long_horizon_run(
            child_runtime_session_id=child_session.runtime_session_id,
            child_run_id=child_state.run_id,
            run_start_event_id=child_run_start_id,
            primary_target=child_target.fact,
            summarizer_target=child_summarizer_target.fact,
            graph_reducer_contract=(
                child_session.subagent_graph_checkpoint_service.reducer_binding.contract
            ),
            account_id=parent_run_start.long_horizon.rollout_account_id,
            account_owner_runtime_session_id=(
                parent_run_start.long_horizon.rollout_account_owner_runtime_session_id
            ),
            account_owner_run_id=(
                parent_run_start.long_horizon.rollout_account_owner_run_id
            ),
            inherited_rollout_reservation=reservation_reference,
        )
        child_rollout_subaccount = build_child_rollout_subaccount(
            child_runtime_session_id=child_session.runtime_session_id,
            child_run_id=child_state.run_id,
            resolved_budget=resolved_budget_event.resolved_budget,
            reservation_reference=reservation_reference,
            root_account_id=parent_run_start.long_horizon.rollout_account_id,
        )
        task_observed_at = run.created_at.isoformat()
        render_policy = build_child_result_render_policy(
            renderer_version="subagent-result:v1",
            max_summary_chars=run.budget_snapshot.max_result_summary_chars_per_child,
            max_artifact_refs=run.budget_snapshot.max_result_artifact_refs_per_child,
        )
        validate_child_render_policy_against_budget(render_policy, run.budget_snapshot)
        frozen_surface = child_agent.capability_runtime.freeze_execution_surface(
            CapabilityExecutionSurfaceSnapshotContext(
                workspace_root=child_session.workspace_root,
                workspace_kind=self.workspace_kind,
                available_tool_names=frozenset(
                    child_agent.tool_executor.registry.names()
                ),
                mcp_installation_id=child_session.mcp_installation_id,
            ),
            tool_registry=child_agent.tool_executor.registry,
            archive=child_session.archive,
            runtime_session_id=child_session.runtime_session_id,
            owner_id=child_run_start_id,
        )
        child_execution_handles = BoundaryExecutionHandles(
            handle_id=f"child_execution_handles:{uuid4().hex}",
            handle_generation=1,
            owner_id=run.subagent_run_id,
            state="run_owned",
            mcp_installation=child_session.mcp_installation_id,
            capability_runtime=child_agent.capability_runtime,
            tool_registry=child_agent.tool_executor.registry,
            frozen_execution_surface=frozen_surface,
        )
        subagent_runtime.attach_child_execution_handles(
            run.subagent_run_id,
            child_execution_handles,
        )
        child_state.scratchpad["capability_execution_borrow_authority"] = (
            child_execution_handles.borrow_authority
        )
        child_state.scratchpad["capability_execution_borrow_kind"] = "child"
        exposure_owner = CapabilityExposureOwnerFact(
            owner_kind="subagent_run_start",
            owner_id=child_run_start_id,
            host_boundary_kind=None,
            runtime_session_id=child_session.runtime_session_id,
            run_id=child_state.run_id,
        )
        capability_basis = build_capability_resolve_basis(
            basis_id=f"capability_basis:subagent:{uuid4().hex}",
            basis_kind="initial",
            source_basis_id=None,
            source_basis_fingerprint=None,
            owner=exposure_owner,
            workspace_identity_fingerprint=sha256_fingerprint(
                "subagent-workspace-identity:v1",
                [str(child_session.workspace_root), self.workspace_kind],
            ),
            memory_domain_id="memory_domain:subagent-disabled",
            permission_snapshot_id=child_permission.snapshot_id,
            plan_active=False,
            active_skill_names=(),
            user_intent_fingerprint=sha256_fingerprint(
                "subagent-task-intent:v1", run_view.task_text
            ),
            prior_transcript_fingerprint=sha256_fingerprint(
                "subagent-prior-transcript:v1", []
            ),
            mcp_installation_id=child_session.mcp_installation_id,
            execution_surface_identity=frozen_surface.identity,
        )
        current_user = CurrentUserMessageFact(
            message_id=f"user-message:{child_state.run_id}",
            source_kind=(
                "subagent_task"
                if run.task_id is not None
                else "subagent_primitive_objective"
            ),
            text=run_view.task_text,
            observed_at_utc=task_observed_at,
            content_sha256=text_sha256(run_view.task_text),
            source_artifact_id=run.task_artifact_id,
        )
        child_entry = SubagentRunEntryFact(
            subagent_run_id=run.subagent_run_id,
            subagent_task_id=run.task_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            parent_run_id=run.parent_run_id,
            spawn_edge_id=run.edge_id,
            capability_profile_fingerprint=sha256_fingerprint(
                "subagent-capability-profile:v1",
                capability_profile.to_event_value(),
            ),
            task_artifact_id=run.task_artifact_id,
            task_observed_at_utc=task_observed_at,
            child_result_render_policy=render_policy,
            permission_snapshot_id=child_permission.snapshot_id,
            model_target_fingerprint=child_target.fact.target_fingerprint,
            mcp_installation_id=child_session.mcp_installation_id,
            mcp_installation_owner_runtime_session_id=(
                child_session.mcp_installation_owner_runtime_session_id
            ),
        )
        child_state.run_model_target = child_target
        child_state.permission_snapshot = child_permission
        child_state.scratchpad.update(
            {
                "run_start_event_id": child_run_start_id,
                "current_user_message_fact": current_user,
                "terminal_run_end_event_id": f"run_end:subagent:{uuid4().hex}",
                "subagent_run_entry_fact": child_entry,
                "capability_resolve_basis_fact": capability_basis,
                "capability_resolve_basis": CapabilityResolveBasis(
                    fact=capability_basis,
                    user_input=run_view.task_text,
                    prior_messages=(),
                    active_skill_names=frozenset(),
                    workspace_root=child_session.workspace_root,
                    memory_domain_id="memory_domain:subagent-disabled",
                ),
                "frozen_capability_execution_surface": frozen_surface,
            }
        )
        prepared_child_entry = PreparedSubagentRunEntry(
            entry_fact=child_entry,
            current_user_message=current_user,
            run_model_target=child_target,
            permission_snapshot=child_permission,
            mcp_installation_fact=McpInstallationReferenceFact(
                installation_id=child_session.mcp_installation_id,
                owner_runtime_session_id=(
                    child_session.mcp_installation_owner_runtime_session_id
                ),
                config_epoch=0,
                event_safe_config_set_fingerprint=sha256_fingerprint(
                    "subagent-mcp-installation-reference:v1",
                    [
                        child_session.mcp_installation_id,
                        child_session.mcp_installation_owner_runtime_session_id,
                    ],
                ),
                server_snapshot_semantic_fingerprints=(),
                binding_identities=(),
            ),
            capability_basis=child_state.scratchpad["capability_resolve_basis"],
            frozen_execution_surface=frozen_surface,
            run_start_event_id=child_run_start_id,
            terminal_run_end_event_id=child_state.scratchpad[
                "terminal_run_end_event_id"
            ],
            long_horizon=child_long_horizon,
            child_rollout_subaccount=child_rollout_subaccount,
        )
        entry_bundle = await SubagentRunEntryDriver().prepare_and_commit(
            child_agent=child_agent,
            state=child_state,
            prepared=prepared_child_entry,
            prior_messages=[],
        )
        working_set = child_state.run_working_set
        if working_set is None:
            raise RuntimeError("committed child run is missing its working set")
        activation_payload = {
            "schema_version": "run_execution_activation.v1",
            "activation_owner_kind": "subagent_run_start",
            "activation_owner_id": entry_bundle.committed.run_start_event.id,
            "segment_generation": 1,
        }
        working_set.run_execution_activation = RunExecutionActivationFact(
            **activation_payload,
            activation_fingerprint=sha256_fingerprint(
                "run-execution-activation:v1", activation_payload
            ),
        )
        working_set.process_segment_id = f"child_segment:{run.subagent_run_id}:1"
        working_set.model_call_control_owner = RunModelCallControlOwner(
            run_id=child_state.run_id,
            activation=working_set.run_execution_activation,
            segment_id=working_set.process_segment_id,
            segment_generation=1,
        )
        try:
            result = await child_agent.run_committed_entry(
                entry_bundle.draft,
                entry_bundle.committed,
            )
        finally:
            await working_set.model_call_control_owner.retire()
            working_set.model_call_control_owner = None
        if result.status is LoopStatus.FINISHED:
            submitted = subagent_runtime.submitted_result(run.subagent_run_id)
            if submitted is not None:
                await subagent_runtime.complete_submitted_result(
                    run.subagent_run_id,
                    token_usage=result.state.token_usage.model_dump(),
                    tool_call_count=result.state.tool_call_count,
                    child_run_id=result.state.run_id,
                )
                return
            await subagent_runtime.complete_native_result(
                run.subagent_run_id,
                child_run_id=result.state.run_id,
            )
            return
        if result.status is LoopStatus.WAITING_USER:
            await child_agent.fail_committed_run(
                result.state,
                stop_reason=RunStopReason.SUBAGENT_PENDING_UNSUPPORTED,
                error_message=(
                    "Child agent entered a pending interaction that V1 cannot route."
                ),
            )
            await subagent_runtime.fail_from_native_child_terminal(
                run.subagent_run_id,
                child_run_id=result.state.run_id,
                reason_code="subagent_pending_unsupported",
                reason_message="Child agent entered a pending interaction that V1 subagent runtime cannot route.",
                diagnostics=[
                    {
                        "status": result.status.value,
                        "stop_reason": result.stop_reason,
                        "pending_interaction_kind": result.state.pending_interaction_kind,
                    }
                ],
            )
            return
        await subagent_runtime.fail_from_native_child_terminal(
            run.subagent_run_id,
            child_run_id=result.state.run_id,
            reason_code=f"subagent_{result.status.value}",
            reason_message=(
                f"Child agent ended with status {result.status.value} without a usable result."
            ),
            diagnostics=[
                {
                    "status": result.status.value,
                    "stop_reason": result.stop_reason,
                    "child_error_present": result.error_message is not None,
                }
            ],
        )

    def new_state(self) -> LoopState:
        return LoopState(
            session_id=self.runtime_session.runtime_session_id, budget=self.budget
        )

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
    def _require_run_model_target(state: LoopState) -> ResolvedModelTarget:
        if state.run_model_target is None:
            raise RuntimeError("active run is missing its ResolvedModelTarget")
        return state.run_model_target

    def _capture_run_permission_snapshot(
        self, state: LoopState
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
            runtime_session_id=self.runtime_session.runtime_session_id,
            run_id=state.run_id,
            permission_mode=mode,
            permission_snapshot_source=source,
        )
        state.permission_snapshot = snapshot
        return snapshot

    def _require_run_permission_snapshot(
        self, state: LoopState
    ) -> RunPermissionSnapshot:
        if state.permission_snapshot is None:
            raise RuntimeError(
                "missing RunPermissionSnapshot for active run; RunStartEvent permission fields are required"
            )
        return state.permission_snapshot

    def _run_permission_policy(self, state: LoopState) -> EffectivePermissionPolicy:
        return preset_to_policy(
            self._require_run_permission_snapshot(state).permission_mode
        )

    def _run_permission_mode(self, state: LoopState) -> PermissionMode:
        return self._require_run_permission_snapshot(state).permission_mode

    def _permission_gate_for_state(self, state: LoopState) -> PolicyPermissionGate:
        return PolicyPermissionGate(
            self._require_run_permission_snapshot(state).to_permission_state(),
            inner=self.permission_gate.inner,
        )

    def _tool_runtime_context(
        self,
        state: LoopState,
        *,
        context_id: str | None = None,
        model_call_index: int | None = None,
    ) -> ToolRuntimeContext:
        snapshot = self._require_run_permission_snapshot(state)
        return ToolRuntimeContext(
            runtime_session_id=self.runtime_session.runtime_session_id,
            event_context=self._event_context(state),
            context_id=context_id,
            model_call_index=model_call_index,
            permission_snapshot_id=snapshot.snapshot_id,
            permission_mode=snapshot.permission_mode.value,
            permission_policy=dict(snapshot.permission_policy),
        )

    def _tool_permission_kwargs(self, state: LoopState) -> dict[str, object]:
        snapshot = self._require_run_permission_snapshot(state)
        return {
            "permission_snapshot_id": snapshot.snapshot_id,
            "permission_mode": snapshot.permission_mode.value,
            "permission_policy": dict(snapshot.permission_policy),
        }

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
                    state.scratchpad.get("pending_run_end_candidate"), RunEndEvent
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
                workspace_root=self.runtime_session.workspace_root,
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
            archive=self.runtime_session.archive,
            runtime_session_id=self.runtime_session.runtime_session_id,
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
            stored_exposure = await self.runtime_session.emit(
                exposure_event,
                state=state,
            )
        except BaseException as exc:
            if isinstance(exc, EventPublicationAfterCommitError):
                confirmed = tuple(exc.result.committed_events)
            else:
                outcome = self.runtime_session.resolved_event_write_outcome(exc)
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
                    runtime_session_id=self.runtime_session.runtime_session_id,
                ),
            )
            raise
        if not isinstance(stored_exposure, CapabilityExposureResolvedEvent):
            raise RuntimeError("capability exposure commit returned wrong event type")
        self._require_run_working_set(state).install_initial_exposure(
            plan=exposure,
            fact=resolved_exposure.fact,
            event_ref=event_reference_from_stored(
                stored_exposure,
                runtime_session_id=self.runtime_session.runtime_session_id,
            ),
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

    def _require_capability_exposure(self, state: LoopState) -> CapabilityExposurePlan:
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
                self.runtime_session.tool_result_render_cache.put(
                    cache_write.cache_key,
                    cache_write.hint,
                )
            except Exception as exc:
                self.runtime_session.record_context_input_cache_diagnostic(
                    cache_kind="tool_result_render",
                    operation="write",
                    error=exc,
                )
        for cache_write in prepared_context_input.candidate_cache_writes:
            try:
                self.runtime_session.context_candidate_lifecycle_cache.put(
                    cache_write.key,
                    cache_write.candidate,
                )
            except Exception as exc:
                self.runtime_session.record_context_input_cache_diagnostic(
                    cache_kind="candidate_lifecycle",
                    operation="write",
                    error=exc,
                )

    async def _ingest_new_tool_result_projections(
        self,
        *,
        state: LoopState,
        resolved_call: ResolvedModelCall,
    ) -> tuple[AgentEvent, ...]:
        """Commit every newly terminal observation before context compilation."""

        projection_input = await prepare_live_transcript_projection(
            runtime_session=self.runtime_session,
            working_set=self._require_run_working_set(state),
            budget=self.budget,
        )
        rendered = render_prepared_tool_result_units(
            prepared=projection_input.prepared_tool_results,
            transcript=projection_input.normalized_transcript.transcript,
            token_estimator=resolved_call.target.token_estimator,
        )
        store = self.runtime_session.long_horizon_state_store
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
        stored = tuple(await self.runtime_session.emit_many(plan.events, state=state))
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
        state: LoopState,
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
        units = {
            unit.unit_id: unit
            for unit in normalized_transcript.tool_result_units
        }
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
            cached = self.runtime_session.prepared_observation_rollup_cache.get(
                cache_key
            )
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
            prepared_unit = await materialize_observation_rollup(
                runtime_session=self.runtime_session,
                run_id=state.run_id,
                prepared=prepared,
                carrier=carrier,
                artifact_mode="read_confirm",
            )
            self.runtime_session.prepared_observation_rollup_cache.put(
                cache_key, prepared_unit
            )
            prepared_units.append(prepared_unit)
        return tuple(prepared_units)

    def _descriptor_render_attribution(
        self,
        state: LoopState,
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
        state: LoopState,
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
        )

    @staticmethod
    def _require_run_working_set(state: LoopState) -> RunWorkingSet:
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("committed run requires a typed RunWorkingSet")
        return working_set

    def _capability_gate_decision_fact(
        self,
        state: LoopState,
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
        subagent_context = self.runtime_session.default_event_metadata.get("subagent")
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
        state: LoopState,
        fact: CapabilityGateDecisionFact,
    ) -> AsyncIterator[AgentEvent]:
        yield await self.runtime_session.emit(
            self._capability_gate_decision_event(state, fact),
            state=state,
        )

    def _capability_gate_decision_event(
        self,
        state: LoopState,
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
        state: LoopState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        rollout_reservation: RolloutReservationFact | None = None,
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
        state: LoopState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
        rollout_reservation: RolloutReservationFact | None = None,
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
        state: LoopState,
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
            )
        )
        run_start = _run_start_for_id(
            self.runtime_session,
            run_id=state.run_id,
        )
        account_id = run_start.long_horizon.rollout_account_id
        rollout_state = self.runtime_session.long_horizon_state_store.rollout_state(
            account_id
        )
        settlement = (
            self._tool_rollout_settlement_event(
                state,
                terminal_event=next(
                    event
                    for event in terminal_candidates
                    if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
                ),
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
        terminal_registry = self.runtime_session.tool_execution_terminal_registry
        if rollout_reservation is not None:
            write_candidates = (
                await self.runtime_session.tool_terminal_projection_service.prepare_batch(
                    write_candidates
                )
            )
            terminal_registry.freeze_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
                candidates=write_candidates,
            )
        try:
            if rollout_reservation is not None:
                result = await RuntimeSessionToolExecutionEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=state,
                ).commit_terminal_batch_and_settlement(
                    terminal_candidates=tuple(
                        event
                        for event in write_candidates
                        if event.id != settlement.id
                    ),
                    settlement_candidate=settlement,
                    expected_reservation_fingerprint=(
                        rollout_reservation.semantic_fingerprint
                    ),
                )
            elif rollout_state is None:
                result = await self.runtime_session.write_events(
                    write_candidates,
                    expected_last_sequence=(
                        self.runtime_session.long_horizon_state_store.through_sequence
                    ),
                    state=state,
                )
            else:
                result = await RuntimeSessionToolExecutionEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=state,
                ).commit_gate_and_denial(
                    gate_candidate=gate_event,
                    denied_terminal_candidates=terminal_candidates,
                    expected_account_state_fingerprint=rollout_state.state_fingerprint,
                    account_id=account_id,
                )
        except BaseException as exc:
            if (
                rollout_reservation is not None
                and self.runtime_session.reconciliation_required
            ):
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
        if track_mcp_terminal:
            self._mark_mcp_terminal_commit_full(state, call.id)
        if result.reconciliation_required:
            if rollout_reservation is not None:
                terminal_registry.mark_commit_outcome_unknown(
                    run_id=state.run_id,
                    reservation=rollout_reservation,
                )
            raise RuntimeError("tool denial committed without a healthy reducer fold")
        if rollout_reservation is not None:
            terminal_registry.complete_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
            )
        return result.committed_events

    async def _stream_capability_access_filtered_calls(
        self,
        state: LoopState,
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
        self, state: LoopState
    ) -> AsyncIterator[AgentEvent]:
        payload = state.scratchpad.get("plan_entry_audit")
        if not isinstance(payload, dict):
            return
        if state.scratchpad.get("plan_entry_audit_emitted"):
            return
        event = await self.runtime_session.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="user",
                previous_permission_mode=payload.get("previous_permission_mode"),
                previous_permission_policy=dict(
                    payload.get("previous_permission_policy") or {}
                ),
                reason=str(payload.get("reason") or ""),
            ),
            state=state,
        )
        plan_state = self._plan_state(state)
        plan_state.apply_durable_event(event)
        if state.run_working_set is not None:
            state.run_working_set.plan_snapshot = plan_workflow_state_fact(plan_state)
        state.scratchpad["plan_entry_audit_emitted"] = True
        yield event

    async def _prepare_rollout_phase_for_model_call(
        self,
        *,
        state: LoopState,
        resolved_call: ResolvedModelCall,
    ) -> tuple[AgentEvent | None, str | None]:
        for _attempt in range(3):
            binding = resolve_run_rollout_binding(
                self.runtime_session,
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
                    resolved_model_call_id=(
                        resolved_call.fact.resolved_model_call_id
                    ),
                    policy=binding.account.policy,
                )
                if (
                    quote.reserved_milliunits
                    > binding.child_state.remaining_milliunits
                ):
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
                    if binding.parent_state.phase
                    is RolloutPhase.EMERGENCY_HARD_STOP
                    else "rollout_budget_exhausted"
                )
                return None, reason
            candidate = build_rollout_phase_transition_event(
                event_context=self._event_context(state),
                account=binding.account,
                state=binding.parent_state,
                plan=plan,
            )
            stored = await self.runtime_session.emit(candidate, state=state)
            return stored, None
        return None, "rollout_admission_reconciliation_blocked"

    async def _await_reclaimable_rollout_reservations(
        self,
        *,
        state: LoopState,
        budget_bucket: RolloutBudgetBucket | None,
    ) -> bool:
        if budget_bucket is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        cancellation_takeover_at = loop.time() + 0.25
        cancelled_children: set[str] = set()
        while loop.time() < deadline:
            binding = resolve_run_rollout_binding(
                self.runtime_session,
                run_id=state.run_id,
            )
            blockers = tuple(
                item
                for item in binding.parent_state.active_reservations
                if item.budget_bucket is budget_bucket
            )
            if not blockers:
                return binding.parent_state.model_stream_reconciliation_blocker_count == 0
            if loop.time() < cancellation_takeover_at:
                # A terminal owner or batch-repair path may already be folding
                # the exact settlement. Give that owner a bounded opportunity
                # to finish before the rollout coordinator takes over child
                # cancellation; competing terminal owners corrupt attribution.
                await asyncio.sleep(
                    min(0.05, cancellation_takeover_at - loop.time())
                )
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
        state: LoopState,
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
            phase_event, rollout_terminal_reason = (
                await self._prepare_rollout_phase_for_model_call(
                    state=state,
                    resolved_call=resolved_call,
                )
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
                yield await self.runtime_session.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=rollout_terminal_reason,
                        code=rollout_terminal_reason,
                    ),
                    state=state,
                )
                break
            for event in await self._ingest_new_tool_result_projections(
                state=state,
                resolved_call=resolved_call,
            ):
                yield event
            memory_prompt = getattr(
                self.memory_hooks, "memory_context_prompt", lambda: None
            )()
            working_set = self._require_run_working_set(state)
            window_policy = working_set.long_horizon_contract.window_policy
            await self.runtime_session.transcript_projection_checkpoint_service.checkpoint_if_needed(
                context=self._event_context(state),
                run_seed_semantic=working_set.run_transcript_seed_semantic,
                run_seed_reference=working_set.run_transcript_seed_reference,
            )
            compiled_context = None
            compile_attempt_index = 0
            context_retry_index = 0
            safe_point_revision = 0
            while state.status is LoopStatus.RUNNING:
                context_id = f"context:{uuid4().hex}"
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
                    prepared_context_input = await prepare_live_context_snapshot(
                        runtime_session=self.runtime_session,
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
                            self.runtime_session.terminal_sessions.current_cwd(
                                owner_host_session_id=(
                                    self.runtime_session.terminal_owner_host_session_id
                                )
                            )
                        ),
                        raw_suspended_state_token_for_validation=(
                            state.scratchpad.get("suspended_state_token")
                        ),
                        candidate_sources=ContextCandidateCollectionInput(
                            system_prompt=(self.system_prompt or DEFAULT_SYSTEM_PROMPT),
                            memory_hook_prompt=memory_prompt,
                            capability_catalog=exposure.catalog_prompt,
                            capability_active_skill=exposure.active_skill_prompt,
                            plan_workflow=(
                                PLAN_ACTIVE_INSTRUCTION
                                if self._plan_state(state).active
                                else None
                            ),
                        ),
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
                    long_horizon_store = (
                        self.runtime_session.long_horizon_state_store
                    )
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
                    prepared_rollups = await (
                        self._prepare_active_observation_rollups(
                            state=state,
                            resolved_call=resolved_call,
                            normalized_transcript=(
                                prepared_context_input.normalized_transcript
                            ),
                            projection_state=projection_state,
                        )
                    )
                    pre_manifest_failure_stage = (
                        ContextCompileFailureStage.WINDOW_COMPACTION_PLANNING
                    )
                    pre_manifest_failure_reason = (
                        ContextInputFailureReasonCode.WINDOW_COMPACTION_PLANNING_FAILED
                    )
                    current_run_planning = (
                        prepare_current_run_projection_planning_input(
                            run_id=state.run_id,
                            run_start_sequence=(
                                self._require_run_working_set(
                                    state
                                ).run_start_sequence
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
                    if (
                        projection_state.total_projected_tokens
                        > projected_soft_target
                    ):
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
                            protection_facts=(
                                current_run_planning.protection_facts
                            ),
                            target_projected_tokens=projected_post_target,
                            source_through_sequence=(
                                prepared_context_input.authority_slice.through_sequence
                            ),
                            rollup_registry=(
                                self.observation_rollup_renderer_registry
                            ),
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
                                next_safe_point_revision = (
                                    advance_safe_point_revision(
                                        safe_point_revision,
                                        policy=window_policy,
                                    )
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
                                await materialize_observation_rollup(
                                    runtime_session=self.runtime_session,
                                    run_id=state.run_id,
                                    prepared=prepared_rollup,
                                    carrier=carrier,
                                )
                            stored_rewrite = tuple(
                                await self.runtime_session.emit_many(
                                    plan.events,
                                    state=state,
                                )
                            )
                            if tuple(event.id for event in stored_rewrite) != tuple(
                                event.id for event in plan.events
                            ):
                                raise RuntimeError(
                                    "projection rewrite committed unexpected events"
                                )
                            if long_horizon_store.projection_state(
                                active_window.window_id
                            ) != plan.final_state:
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
                    draft_compiled_context = compile_context_from_facts(
                        facts=prepared_context_input.invocation,
                        transcript=prepared_context_input.normalized_transcript.transcript,
                        rendered_tool_results=render_output,
                        prepared_rollups=prepared_rollups,
                        section_candidates=prepared_context_input.prepared_candidates,
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
                                run_contract=(
                                    working_set.long_horizon_contract
                                ),
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
                    long_horizon_attribution = (
                        build_long_horizon_context_attribution(
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
                    try:
                        projection_unreachable_audit = (
                            projection_target_unreachable_audit(
                                projection_unreachable
                            )
                            if projection_unreachable is not None
                            else None
                        )
                        prepared_transcript_projection = (
                            prepare_transcript_projection_input(
                                evidence=(
                                    prepared_context_input.transcript_projection_evidence
                                ),
                                normalized=(
                                    prepared_context_input.normalized_transcript
                                ),
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
                                fallback_source_ref=(
                                    snapshot_fact.run_entry.run_start
                                ),
                                authority_events=(
                                    *tuple(
                                        prepared_context_input.authority_slice.events
                                    ),
                                    *tuple(
                                        event
                                        for event_slice in prepared_context_input.named_slices
                                        for event in event_slice.events
                                    ),
                                    *prepared_context_input.exact_named_authority_events,
                                ),
                            )
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
                            self.runtime_session.context_input_manifest_service.persist(
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
                        yield await self.runtime_session.emit(
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
                            state=state,
                        )
                        yield await self.runtime_session.emit(
                            RunErrorEvent(
                                **self._event_context(state).event_fields(),
                                message=str(exc),
                                code="context_input_manifest_write_failed",
                            ),
                            state=state,
                        )
                        if isinstance(
                            exc,
                            (
                                ContextInputManifestWriteConflict,
                                ContextInputManifestWriteOutcomeUnknown,
                            ),
                        ):
                            state.scratchpad[
                                "context_input_latch_after_terminalization"
                            ] = True
                        break
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
                        or self.runtime_session.reconciliation_required
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
                    yield await self.runtime_session.emit(
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
                        state=state,
                    )
                    yield await self.runtime_session.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=str(exc),
                            code=f"context_input_{exc.reason_code.value}",
                        ),
                        state=state,
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
                    yield await self.runtime_session.emit(
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
                        state=state,
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
                    yield await self.runtime_session.emit(
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
                        state=state,
                    )
                    yield await self.runtime_session.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=str(exc),
                            code="context_budget_exceeded",
                        ),
                        state=state,
                    )
                    break
                except Exception as exc:
                    if self.runtime_session.reconciliation_required:
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
                    yield await self.runtime_session.emit(
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
                        state=state,
                    )
                    yield await self.runtime_session.emit(
                        RunErrorEvent(
                            **self._event_context(state).event_fields(),
                            message=f"{type(exc).__name__}: {exc}",
                            code=diagnostic_code,
                        ),
                        state=state,
                    )
                    break
            if compiled_context is None:
                break
            long_horizon_diagnostics = list(
                long_horizon_context_diagnostics(
                    measurement=long_horizon_budget,
                    target_unreachable=input_manifest.projection_target_unreachable,
                )
            )
            state.scratchpad["current_context_id"] = compiled_context.context_id
            state.scratchpad["current_model_call_index"] = model_call_index
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
                long_horizon_context_budget_decision=(
                    long_horizon_budget.decision
                ),
                long_horizon_projection_pressure_shadow=(
                    long_horizon_budget.pressure_shadow
                ),
                input_audit=input_audit,
                provider_neutral_payload_fingerprint=(
                    provider_neutral_payload_fingerprint(compiled_context.llm_context)
                ),
                canonical_render_decisions_fingerprint=(
                    canonical_render_decisions_fingerprint(
                        compiled_context.tool_result_render_decision_facts
                    )
                ),
            )
            try:
                stored_context_compiled = await self.runtime_session.emit(
                    context_compiled_candidate,
                    state=state,
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
            context = replace(
                compiled_context.llm_context,
                resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
                target_fingerprint=resolved_call.target.fact.target_fingerprint,
            )
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
                if _compiled_section_included(
                    compiled_context, _SUBAGENT_RESULTS_SECTION_ID
                )
                else ()
            )
            delivered_subagent_results = False

            reply_had_run_error = False
            accepted_control_permit = None
            try:
                run_activation = (
                    state.run_working_set.run_execution_activation
                    if state.run_working_set is not None
                    else None
                )
                start_bundle = prepare_model_lifecycle_start_bundle(
                    call=resolved_call,
                    context=context,
                    event_context=self._event_context(state),
                    runtime_session=self.runtime_session,
                    lifecycle_kind="main_assistant_reply",
                    run_execution_activation=run_activation,
                )
                model_stream_handle = self.llm_runtime.start_stream(
                    call=resolved_call,
                    context=context,
                    event_context=self._event_context(state),
                    start_bundle=start_bundle,
                    commit_port=RuntimeSessionModelStreamEventCommitPort(
                        runtime_session=self.runtime_session,
                        state=state,
                    ),
                    execution_registry=(
                        self.runtime_session.model_stream_execution_registry
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
                        control_resolution = await control_owner.resolve_completed_call(
                            result=committed_model_result,
                            model_call_index=model_call_index,
                            event_context=self._event_context(state),
                            runtime_session=self.runtime_session,
                            state=state,
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
                yield await self.runtime_session.emit(
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
                    state=state,
                )
                state.status = LoopStatus.FAILED
                state.stop_reason = RunStopReason.MODEL_ERROR
                state.error_message = str(exc)
                state.transition(LoopTransition.FAIL)
                reply_had_run_error = True
                yield await self.runtime_session.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code=exc.reason_code,
                    ),
                    state=state,
                )
            except ModelCallControlResolutionError:
                # The completed provider result remains owned by its stable
                # disposition candidate.  A later model call or RunEnd would
                # cross that unresolved control fact, so fail closed here.
                raise
            except Exception as exc:
                event = await self.runtime_session.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code=str(getattr(exc, "reason_code", "model_stream_error")),
                    ),
                    state=state,
                )
                reply_had_run_error = True
                yield event

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

            assistant = self.runtime_session.event_log.replay(state.reply_id)
            state.messages.append(assistant)
            _accumulate_usage(state, assistant)
            ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "after_model_reply",
                lambda: self.memory_hooks.after_model_reply(state, assistant),
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
                                str(
                                    state.scratchpad.get("plan_revision_feedback") or ""
                                )
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
            async for event in self._execute_tool_blocks(state, tool_blocks):
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
            async for event in self._continue_after_tool_before_followup(state):
                yield event

        if state.status is LoopStatus.WAITING_USER:
            return
        async for event in self._finalize_run(state):
            yield event

    def _apply_stop_request(self, state: LoopState) -> bool:
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

    async def _stream_approval_resolution(
        self,
        state: LoopState,
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
        event = await self.runtime_session.emit(
            UserConfirmResultEvent(
                **self._event_context(state).event_fields(),
                confirm_results=confirm_results,
            ),
            state=state,
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
        async for event in self._continue_after_tool_before_followup(state):
            yield event
        exposure = self._require_capability_exposure(state)
        async for event in self._stream_model_loop(state, exposure):
            yield event

    async def _stream_plan_interaction_resolution(
        self,
        state: LoopState,
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
        state: LoopState,
        resolution: McpInputRequiredInteractionResolution,
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

        tool_call_id = str(payload["tool_call_id"])
        tool_name = str(payload["tool_name"])
        server_id = str(payload["server_id"])
        original_request = dict(payload.get("original_request") or {})
        request_state = (
            str(payload["request_state"])
            if payload.get("request_state") is not None
            else None
        )
        input_requests = _mcp_input_requests_from_payload(payload.get("input_requests"))
        round_count = int(payload.get("round_count") or 1)
        deadline_monotonic = _optional_float(payload.get("deadline_monotonic"))
        timing_seed = dict(payload.get("tool_observation_timing_seed") or {})
        original_pending_payload = dict(state.pending_interaction_payload)
        rollout_reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )

        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            yield await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="mcp_input_required_expired",
                    value={
                        "interaction_id": resolution.interaction_id,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "server_id": server_id,
                        "round_count": round_count,
                    },
                ),
                state=state,
            )
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
                tool_arguments=dict(original_request.get("arguments") or {}),
                tool_observation_timing_seed={**timing_seed, "resumed_at": utc_now()}
                if timing_seed
                else None,
                rollout_reservation=rollout_reservation,
            ):
                yield event
            async for event in self._after_mcp_resume_terminal_result(
                state,
                interaction_id=resolution.interaction_id,
            ):
                yield event
            return

        yield await self.runtime_session.emit(
            CustomEvent(
                **self._event_context(state).event_fields(),
                name="mcp_input_required_resolved",
                value={
                    "interaction_id": resolution.interaction_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "server_id": server_id,
                    "cancelled": resolution.cancelled,
                    "response_keys": sorted(resolution.responses),
                    "round_count": round_count,
                },
            ),
            state=state,
        )

        gate_call = ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=dict(original_request.get("arguments") or {}),
        )
        try:
            current_tool = self.tool_executor.registry.get(tool_name)
        except KeyError:
            current_tool = None
        if current_tool is not None and _mcp_resume_binding_changed(
            payload,
            current_tool,
        ):
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.status = LoopStatus.RUNNING
            state.stop_reason = None
            yield await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="mcp_input_required_binding_changed",
                    value={
                        "interaction_id": resolution.interaction_id,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "server_id": server_id,
                        "reason_code": "mcp_binding_generation_changed",
                    },
                ),
                state=state,
            )
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
                ):
                    yield event
            else:
                tool = self.tool_executor.registry.get(tool_name)
                resume = getattr(tool, "resume_input_required", None)
                if resume is None:
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
                    ):
                        yield event
                else:
                    try:
                        result = await resume(
                            original_request=original_request,
                            request_state=request_state,
                            resolution=McpInputRequiredResolution(
                                interaction_id=resolution.interaction_id,
                                responses={
                                    key: dict(value)
                                    for key, value in resolution.responses.items()
                                },
                                cancelled=resolution.cancelled,
                                tool_call_id=tool_call_id,
                                input_requests=input_requests,
                                round_count=round_count,
                                deadline_monotonic=deadline_monotonic,
                            ),
                            runtime_context=self._tool_runtime_context(state),
                        )
                    except Exception as exc:
                        state.pending_interaction_kind = "mcp_input_required"
                        state.pending_interaction_payload = original_pending_payload
                        state.status = LoopStatus.WAITING_USER
                        state.stop_reason = RunStopReason.WAITING_USER
                        yield await self.runtime_session.emit(
                            CustomEvent(
                                **self._event_context(state).event_fields(),
                                name="mcp_input_required_resume_failed",
                                value={
                                    "interaction_id": resolution.interaction_id,
                                    "tool_call_id": tool_call_id,
                                    "tool_name": tool_name,
                                    "server_id": server_id,
                                    "error_type": type(exc).__name__,
                                    "message": redact_mcp_error_message(exc),
                                },
                            ),
                            state=state,
                        )
                        return
                    state.pending_interaction_kind = None
                    state.pending_interaction_payload = {}
                    state.status = LoopStatus.RUNNING
                    state.stop_reason = None
                    if isinstance(result, ToolExecutionSuspended):
                        next_round = int(
                            result.payload.get("round_count") or (round_count + 1)
                        )
                        if next_round > MAX_MCP_INPUT_REQUIRED_ROUNDS:
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
                            result,
                            reservation=rollout_reservation,
                        ):
                            yield event
                        return
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
                    ):
                        yield event

        async for event in self._after_mcp_resume_terminal_result(
            state,
            interaction_id=resolution.interaction_id,
        ):
            yield event

    async def _after_mcp_resume_terminal_result(
        self,
        state: LoopState,
        *,
        interaction_id: str,
    ) -> AsyncIterator[AgentEvent]:
        await self._complete_mcp_pending_lease(interaction_id)
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
        state: LoopState,
    ) -> MidTurnCompactionResult:
        model_visible_messages = [
            message.model_copy(deep=True) for message in state.messages
        ]
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = ()
        if state.run_model_target is not None:
            projection = await prepare_live_transcript_projection(
                runtime_session=self.runtime_session,
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
        state: LoopState,
    ) -> AsyncIterator[AgentEvent]:
        state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
        compaction_result = await self._maybe_compact_mid_turn_before_followup(state)
        for event in compaction_result.events:
            yield event
        state.begin_next_turn()

    async def _resolve_plan_question(
        self,
        state: LoopState,
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
        yield await self.runtime_session.emit(
            PlanQuestionAnsweredEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                answer_text=resolution.answer_text,
                selected_option=resolution.selected_option,
            ),
            state=state,
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
        state: LoopState,
        payload: dict,
        resolution: PlanExitResolution,
    ) -> AsyncIterator[AgentEvent]:
        rollout_reservation = self._pending_tool_rollout_reservation(
            payload,
            run_id=state.run_id,
        )
        exit_request_id = str(payload.get("exit_request_id") or "")
        tool_call_id = str(payload["tool_call_id"])
        yield await self.runtime_session.emit(
            PlanExitResolvedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=tool_call_id,
                decision=resolution.decision,
                user_feedback=resolution.user_feedback,
            ),
            state=state,
        )
        if resolution.decision == "revise":
            revisions = int(state.scratchpad.get("plan_exit_revisions", 0)) + 1
            state.scratchpad["plan_exit_revisions"] = revisions
            if revisions > state.budget.max_plan_exit_revisions_per_run:
                yield await self._mark_plan_budget_exceeded(state, kind="exit_revision")
            else:
                state.scratchpad["plan_revision_required"] = True
                state.scratchpad["plan_revision_feedback"] = resolution.user_feedback
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
                self.runtime_session.archive.put_text(
                    accepted_artifact_id,
                    accepted_plan_text,
                    session_id=self.runtime_session.runtime_session_id,
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
            stored_exit = await self.runtime_session.emit(
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
                state=state,
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

    async def _after_tool_results(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        if self._finish_child_run_after_report_result(state):
            return

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

        if self.tool_result_persistence_hook is not None:
            event = await self._run_tool_result_persistence_hook(state)
            if event is not None:
                yield event
        ok, hook_events = await self._run_memory_hook_and_emit_events(
            state,
            "after_tool_results",
            lambda: self.memory_hooks.after_tool_results(state, state.tool_results),
        )
        for event in hook_events:
            yield event
        if not ok:
            return
        ok, should_compact, error_event = await self._run_memory_hook(
            state,
            "should_compact",
            lambda: self.memory_hooks.should_compact(state),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            return
        if should_compact:
            state.compacted = True
            yield await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="compaction_requested",
                    value={},
                ),
                state=state,
            )

    def _finish_child_run_after_report_result(self, state: LoopState) -> bool:
        if not self._is_subagent_child or self.subagent_runtime is None:
            return False
        subagent_context = self.runtime_session.default_event_metadata.get("subagent")
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

    async def _finalize_run(
        self,
        state: LoopState,
        *,
        run_session_end_hook: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        if state.finalized:
            return
        hook_done = bool(state.scratchpad.get("run_finalization_hook_done"))
        if run_session_end_hook and not hook_done:
            _ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "on_turn_end",
                lambda: self._call_turn_end_hook(state),
            )
            for event in hook_events:
                yield event
        state.scratchpad["run_finalization_hook_done"] = True
        terminal_event_id = state.scratchpad.get("terminal_run_end_event_id")
        if not isinstance(terminal_event_id, str):
            raise RuntimeError("run finalization requires stable RunEnd event id")
        if state.status is LoopStatus.FINISHED:
            terminalization_kind = RunTerminalizationKind.NORMAL
        elif state.status is LoopStatus.ABORTED:
            terminalization_kind = (
                RunTerminalizationKind.HOST_TEARDOWN
                if state.abort_kind is AbortKind.HOST_TEARDOWN
                else RunTerminalizationKind.USER_STOP
            )
        else:
            terminalization_kind = RunTerminalizationKind.EXECUTION_FAILURE
        if (
            not self._is_subagent_child
            and self.subagent_runtime is not None
            and not state.scratchpad.get("long_horizon_child_drain_done")
        ):
            await self.subagent_runtime.drain_children_for_parent_run(
                state.run_id,
                timeout_seconds=5.0,
            )
            state.scratchpad["long_horizon_child_drain_done"] = True
        pending = state.scratchpad.get("pending_run_terminal_candidates")
        if isinstance(pending, tuple) and pending:
            candidates = pending
            if not isinstance(candidates[-1], RunEndEvent):
                raise RuntimeError("pending run terminal batch has invalid shape")
            candidate = candidates[-1]
        elif pending is not None:
            raise RuntimeError("pending run terminal candidates have invalid type")
        else:
            candidate = RunEndEvent(
                id=terminal_event_id,
                **self._event_context(state).event_fields(),
                status=state.status.value,
                stop_reason=state.stop_reason,
                terminalization_kind=terminalization_kind,
                abort_kind=state.abort_kind.value
                if state.abort_kind is not None
                else None,
                error_message=state.error_message,
            )
            candidates = self._build_run_terminal_candidates(
                state=state,
                run_end=candidate,
                terminalization_kind=terminalization_kind,
            )
        state.scratchpad["pending_run_terminal_candidates"] = candidates
        state.scratchpad["pending_run_end_candidate"] = candidate
        try:
            stored = tuple(
                await self.runtime_session.emit_many(candidates, state=state)
            )
        except BaseException as exc:
            outcome = self.runtime_session.resolved_event_write_outcome(exc)
            if outcome.status == "unknown":
                raise
            if outcome.status == "none":
                state.scratchpad["run_end_commit_state"] = "pending"
                try:
                    stored_retry = tuple(
                        await self.runtime_session.emit_many(candidates, state=state)
                    )
                except BaseException as retry_error:
                    retry_outcome = self.runtime_session.resolved_event_write_outcome(
                        retry_error
                    )
                    if retry_outcome.status != "full":
                        raise
                    retry_confirmed = tuple(retry_outcome.committed_events)
                    if not _is_exact_run_terminal_batch(
                        retry_confirmed,
                        candidates,
                    ):
                        self.runtime_session.latch_event_commit_outcome_unknown()
                        raise RuntimeError(
                            "run terminal retry confirmation was not exact"
                        ) from retry_error
                    self._mark_run_terminal_committed(state)
                    raise
                if not _is_exact_run_terminal_batch(stored_retry, candidates):
                    raise RuntimeError(
                        "run terminal bounded retry returned wrong batch"
                    )
                self._mark_run_terminal_committed(state)
                for event in stored_retry:
                    yield event
                return
            confirmed = tuple(outcome.committed_events)
            if not _is_exact_run_terminal_batch(confirmed, candidates):
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise RuntimeError("run terminal confirmation was not exact") from exc
            self._mark_run_terminal_committed(state)
            raise
        if not _is_exact_run_terminal_batch(stored, candidates):
            raise RuntimeError("run terminal commit returned wrong batch")
        self._mark_run_terminal_committed(state)
        for event in stored:
            yield event

    def _mark_run_terminal_committed(self, state: LoopState) -> None:
        state.finalized = True
        self._latch_context_input_after_terminalization(state)
        state.scratchpad["run_end_commit_state"] = "committed"
        state.scratchpad.pop("pending_run_end_candidate", None)
        state.scratchpad.pop("pending_run_terminal_candidates", None)

    def _build_run_terminal_candidates(
        self,
        *,
        state: LoopState,
        run_end: RunEndEvent,
        terminalization_kind: RunTerminalizationKind,
    ) -> tuple[AgentEvent, ...]:
        if state.run_working_set is None:
            raise RuntimeError("run terminalization requires committed working set")
        store = self.runtime_session.long_horizon_state_store
        window_state = store.window_state(state.run_id)
        if window_state is None or window_state.active_window_id is None:
            raise RuntimeError("run terminalization requires one active context window")
        window = window_state.windows[window_state.active_window_id]
        projection_state = store.projection_state(window.window_id)
        if projection_state is None:
            raise RuntimeError("run terminalization lost projection state")
        source_through_sequence = self.runtime_session.event_log.next_sequence() - 1
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
            self.runtime_session,
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
            _, state_before_close = apply_rollout_event(
                account=rollout_account,
                state=rollout_state,
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
            child_state = (
                self.runtime_session.long_horizon_state_store.child_rollout_state(
                    run_start.run_id
                )
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

    def _latch_context_input_after_terminalization(self, state: LoopState) -> None:
        if state.scratchpad.pop("context_input_latch_after_terminalization", False):
            self.runtime_session.latch_context_input_reconciliation_required()

    def _run_result(self, state: LoopState) -> AgentRunResult:
        return AgentRunResult(
            status=state.status,
            stop_reason=state.stop_reason,
            state=state,
            messages=list(state.messages),
            final_text=_final_text(state.messages),
            error_message=state.error_message,
        )

    async def _run_memory_hook(self, state: LoopState, hook_name: str, call):
        try:
            return True, await call(), None
        except Exception as exc:
            event = await self._mark_memory_hook_failed(state, hook_name, exc)
            return False, None, event

    async def _call_turn_start_hook(self, state: LoopState, user_input: str):
        hook = getattr(self.memory_hooks, "on_turn_start", None)
        if hook is not None and _is_overridden_hook(
            self.memory_hooks, "on_turn_start", NoopMemoryHooks
        ):
            return await hook(state, user_input)
        return await self.memory_hooks.on_session_start(state, user_input)

    async def _call_turn_end_hook(self, state: LoopState):
        hook = getattr(self.memory_hooks, "on_turn_end", None)
        if hook is not None and _is_overridden_hook(
            self.memory_hooks, "on_turn_end", NoopMemoryHooks
        ):
            return await hook(state)
        return await self.memory_hooks.on_session_end(state)

    async def _run_memory_hook_and_emit_events(
        self,
        state: LoopState,
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
                emitted_events.append(
                    await self.runtime_session.emit(event, state=state)
                )
        except Exception as exc:
            emitted_events.append(
                await self._mark_memory_hook_failed(state, hook_name, exc)
            )
            return False, emitted_events
        return True, emitted_events

    async def _run_tool_result_persistence_hook(
        self, state: LoopState
    ) -> AgentEvent | None:
        assert self.tool_result_persistence_hook is not None
        try:
            await self.tool_result_persistence_hook.after_tool_results(
                state, state.tool_results
            )
            return None
        except Exception as exc:
            return await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="tool_result_persistence_failed",
                    value={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
                state=state,
            )

    async def _mark_memory_hook_failed(
        self, state: LoopState, hook_name: str, exc: Exception
    ) -> AgentEvent:
        message = f"memory hook {hook_name} failed: {type(exc).__name__}: {exc}"
        state.status = LoopStatus.FAILED
        state.stop_reason = RunStopReason.MEMORY_HOOK_ERROR
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="memory_hook_error",
                metadata={"hook": hook_name},
            ),
            state=state,
        )

    async def _project_memory(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        projection_id = f"projection:{state.turn_id}"
        context = self._event_context(state)
        yield await self.runtime_session.emit(
            ProjectionRequestedEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
            ),
            state=state,
        )
        baseline = None
        try:
            baseline = self.memory_hooks.baseline_projection(
                state,
                token_budget=self.budget.projection_token_budget,
            )
            projection = await asyncio.wait_for(
                self.memory_hooks.project(
                    state,
                    token_budget=self.budget.projection_token_budget,
                ),
                timeout=self.budget.recall_hard_timeout_ms / 1000,
            )
        except TimeoutError:
            state.memory_projection = baseline
            if baseline is not None:
                yield await self.runtime_session.emit(
                    ProjectionReadyEvent(
                        **context.event_fields(),
                        projection_id=projection_id,
                        role=self.model_role.value,
                        scope=state.current_scope or "session",
                        token_budget=self.budget.projection_token_budget,
                        projection_kind=_memory_projection_kind(baseline),
                        included_memory_ids=_projection_ids(baseline),
                        summary=_projection_summary(baseline),
                        metadata={
                            "degraded": True,
                            "warnings": ["semantic_recall_timeout"],
                            "fallback": "baseline_projection",
                        },
                    ),
                    state=state,
                )
                return
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error="recall_timeout",
                ),
                state=state,
            )
            return
        except Exception as exc:
            state.memory_projection = None
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                state=state,
            )
            return
        state.memory_projection = projection
        yield await self.runtime_session.emit(
            ProjectionReadyEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
                projection_kind=_memory_projection_kind(projection),
                included_memory_ids=_projection_ids(projection),
                summary=_projection_summary(projection),
            ),
            state=state,
        )

    async def _execute_tool_blocks(
        self,
        state: LoopState,
        tool_blocks: list[ToolCallBlock],
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []
        for block in tool_blocks:
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                stored_events = await self.runtime_session.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                        failure_stage="malformed_arguments",
                    ),
                    state=state,
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
                stored_events = await self.runtime_session.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=f"Duplicate tool_call_id in assistant reply: {call.id}",
                        arguments=call.arguments,
                        failure_stage="policy_denied",
                    ),
                    state=state,
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
            event = await self.runtime_session.emit(
                RequireUserConfirmEvent(
                    **self._event_context(state).event_fields(), tool_calls=blocks
                ),
                state=state,
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
        state: LoopState,
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
        state: LoopState,
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
        state: LoopState,
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
        stored_entered = await self.runtime_session.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="agent",
                previous_permission_mode=previous_mode.value,
                previous_permission_policy=previous_policy.to_dict(),
                reason=reason,
            ),
            state=state,
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
        state: LoopState,
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
        yield await self.runtime_session.emit(
            PlanQuestionAskedEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                tool_call_id=call.id,
                question=question,
                options=option_payload,
                allow_free_text=allow_free_text,
                reason=reason,
            ),
            state=state,
        )
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
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _execute_exit_plan(
        self,
        state: LoopState,
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
        state.scratchpad.pop("plan_revision_required", None)
        state.scratchpad.pop("plan_revision_feedback", None)
        exit_request_id = f"plan_exit:{uuid4().hex}"
        interaction_id = f"plan_interaction:{uuid4().hex}"
        yield await self.runtime_session.emit(
            PlanExitRequestedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=call.id,
                plan_text=plan_text,
                summary=summary,
            ),
            state=state,
        )
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
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _emit_tool_result_and_record(
        self,
        state: LoopState,
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
    ) -> AsyncIterator[AgentEvent]:
        prior_result_events = _tool_result_boundary_events(
            self.runtime_session.event_log,
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            start_event_id=_tool_timing_start_event_id(
                tool_observation_timing_seed
            ),
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
        terminal_registry = self.runtime_session.tool_execution_terminal_registry
        if rollout_reservation is not None:
            write_candidates = (
                await self.runtime_session.tool_terminal_projection_service.prepare_batch(
                    write_candidates
                )
            )
            terminal_registry.freeze_terminal(
                run_id=state.run_id,
                reservation=rollout_reservation,
                candidates=write_candidates,
            )
        try:
            if rollout_reservation is None:
                stored_events = await self.runtime_session.emit_many(
                    candidates,
                    state=state,
                )
            else:
                result = await RuntimeSessionToolExecutionEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=state,
                ).commit_terminal_batch_and_settlement(
                    terminal_candidates=tuple(
                        event
                        for event in write_candidates
                        if event.id != settlement.id
                    ),
                    settlement_candidate=settlement,
                    expected_reservation_fingerprint=(
                        rollout_reservation.semantic_fingerprint
                    ),
                )
                stored_events = list(result.committed_events)
                if result.reconciliation_required:
                    terminal_registry.mark_commit_outcome_unknown(
                        run_id=state.run_id,
                        reservation=rollout_reservation,
                    )
                    raise RuntimeError(
                        "MCP terminal committed without a healthy reducer fold"
                    )
        except EventPublicationAfterCommitError as exc:
            if rollout_reservation is not None:
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
            outcome = self.runtime_session.resolved_event_write_outcome(error)
            if outcome.status == "unknown":
                if track_mcp_terminal:
                    self._mark_mcp_terminal_commit_untrusted(state, tool_call_id)
                if rollout_reservation is not None:
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
            if rollout_reservation is not None:
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
        if rollout_reservation is not None:
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

    async def _complete_mcp_pending_lease(self, interaction_id: str) -> None:
        supervisor = self.runtime_session.mcp_supervisor
        if supervisor is None:
            return
        supervisor.complete_pending_lease(interaction_id)
        await supervisor.close_retiring_slots(
            timeout_seconds=5.0,
            wait_for_borrowers=False,
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
            for state in self.runtime_session.long_horizon_state_store.rollout_states()
            for reservation in state.active_reservations
            if reservation.reservation_id == reservation_id
        )
        binding = resolve_run_rollout_binding(
            self.runtime_session,
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
        terminal_registry = self.runtime_session.tool_execution_terminal_registry
        if terminal_registry.owner_for_call(
            run_id=run_id,
            tool_call_id=tool_call_id,
        ) is None:
            terminal_registry.restore_suspended(
                run_id=run_id,
                reservation=reservation,
            )
        return reservation

    def _tool_rollout_settlement_event(
        self,
        state: LoopState,
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
        state: LoopState,
        tool_call_id: str,
    ) -> tuple[str, str]:
        key = (state.run_id, tool_call_id)
        if key not in self._mcp_terminal_commit_outcomes:
            raise RuntimeError("MCP terminal commit owner is not active")
        return key

    def _mark_mcp_terminal_commit_attempt(
        self,
        state: LoopState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "attempting"

    def _mark_mcp_terminal_commit_none(
        self,
        state: LoopState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "none"

    def _mark_mcp_terminal_commit_full(
        self,
        state: LoopState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "full"

    def _mark_mcp_terminal_commit_untrusted(
        self,
        state: LoopState,
        tool_call_id: str,
    ) -> None:
        key = self._mcp_terminal_commit_key(state, tool_call_id)
        self._mcp_terminal_commit_outcomes[key] = "untrusted"

    def _resolve_mcp_terminal_commit_failure(
        self,
        state: LoopState,
        *,
        tool_call_id: str,
        candidates: tuple[AgentEvent, ...],
        error: BaseException,
    ) -> None:
        if isinstance(error, EventPublicationAfterCommitError):
            self._mark_mcp_terminal_commit_full(state, tool_call_id)
            return
        del candidates
        outcome = self.runtime_session.resolved_event_write_outcome(error)
        if outcome.status == "unknown":
            self._mark_mcp_terminal_commit_untrusted(state, tool_call_id)
            return
        if outcome.status == "none":
            self._mark_mcp_terminal_commit_none(state, tool_call_id)
            return
        self._mark_mcp_terminal_commit_full(state, tool_call_id)

    def _committed_tool_result_events(
        self,
        state: LoopState,
        *,
        tool_call_id: str,
        start_event_id: str | None = None,
    ) -> list[AgentEvent]:
        events = _completed_tool_result_events(
            self.runtime_session.event_log,
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            start_event_id=start_event_id,
        )
        if not any(isinstance(event, ToolResultEndEvent) for event in events):
            return []
        return events

    def _record_tool_result_events(
        self,
        state: LoopState,
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

    def _plan_state(self, state: LoopState) -> PlanWorkflowState:
        plan_state = state.scratchpad.get("plan_state")
        if isinstance(plan_state, PlanWorkflowState):
            return plan_state
        plan_state = PlanWorkflowState()
        state.scratchpad["plan_state"] = plan_state
        return plan_state

    def _plan_revision_required(self, state: LoopState) -> bool:
        return (
            bool(state.scratchpad.get("plan_revision_required"))
            and self._plan_state(state).active
        )

    def _consume_plan_interaction_budget(self, state: LoopState) -> bool:
        consumed = int(state.scratchpad.get("plan_interactions", 0))
        if consumed >= state.budget.max_plan_interactions_per_run:
            state.status = LoopStatus.FAILED
            state.stop_reason = RunStopReason.PLAN_INTERACTION_BUDGET
            state.error_message = "plan interaction budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.scratchpad["plan_interactions"] = consumed + 1
        return True

    async def _emit_plan_budget_error_result(
        self,
        state: LoopState,
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
        yield await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
            state=state,
        )

    async def _mark_plan_budget_exceeded(
        self, state: LoopState, *, kind: str
    ) -> AgentEvent:
        message = f"plan {kind} budget exceeded"
        state.status = LoopStatus.FAILED
        state.stop_reason = RunStopReason.PLAN_INTERACTION_BUDGET
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
            state=state,
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
        state: LoopState,
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
                stored_events = await self.runtime_session.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message="tool call denied by user approval",
                        result_state=ToolResultState.DENIED,
                        arguments=_tool_block_arguments_for_semantics(block),
                        failure_stage="permission_denied",
                    ),
                    state=state,
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
                stored_events = await self.runtime_session.emit_many(
                    self._typed_tool_result_error_events(
                        state,
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                        failure_stage="malformed_arguments",
                    ),
                    state=state,
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
        state: LoopState,
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
                capacity = self.runtime_session.physical_dispatch_capacity(
                    PhysicalOperationKind.TOOL_CALL
                )
                if capacity <= 0:
                    await self.runtime_session.ensure_physical_operation_headroom(
                        PhysicalOperationKind.TOOL_CALL
                    )
                    capacity = self.runtime_session.physical_dispatch_capacity(
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
        state: LoopState,
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
                _tool_result_message_from_events(
                    batch_events, call.name, result_block
                )
            )
            if call.id in reservations:
                state.tool_call_count += 1

    async def _commit_tool_admissions(
        self,
        state: LoopState,
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
            binding = resolve_run_rollout_binding(
                self.runtime_session,
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
                transition = await self.runtime_session.emit(
                    build_rollout_phase_transition_event(
                        event_context=self._event_context(state),
                        account=binding.account,
                        state=binding.parent_state,
                        plan=plan,
                    ),
                    state=state,
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
        result = await RuntimeSessionToolExecutionEventCommitPort(
            runtime_session=self.runtime_session,
            state=state,
        ).commit_gate_batch(
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
                self.runtime_session.tool_execution_terminal_registry.install_admitted_batch(
                    run_id=state.run_id,
                    reservations=tuple(reservations.values()),
                )
            except BaseException:
                # Admission is already durable.  Losing its sole process owner is
                # an unknown execution state, so no physical tool may start.
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise
        executable_calls = [call for call in calls if call.id in reservations]
        return (
            (*prelude_events, *result.committed_events),
            executable_calls,
            reservations,
        )

    async def _commit_tool_terminal(
        self,
        state: LoopState,
        *,
        terminal_event: ToolResultEndCandidate,
        reservation: RolloutReservationFact,
    ) -> tuple[AgentEvent, ...]:
        settlement = self._tool_rollout_settlement_event(
            state,
            terminal_event=terminal_event,
            reservation=reservation,
        )
        candidates = (
            await self.runtime_session.tool_terminal_projection_service.prepare_batch(
                (terminal_event, settlement)
            )
        )
        terminal_registry = self.runtime_session.tool_execution_terminal_registry
        terminal_registry.freeze_terminal(
            run_id=state.run_id,
            reservation=reservation,
            candidates=candidates,
        )
        try:
            result = await RuntimeSessionToolExecutionEventCommitPort(
                runtime_session=self.runtime_session,
                state=state,
            ).commit_terminal_batch_and_settlement(
                terminal_candidates=tuple(
                    event for event in candidates if event.id != settlement.id
                ),
                settlement_candidate=settlement,
                expected_reservation_fingerprint=reservation.semantic_fingerprint,
            )
        except BaseException:
            if self.runtime_session.reconciliation_required:
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

    async def _stream_tool_batch_events(
        self,
        state: LoopState,
        batch: list[ToolCall],
        batch_events: list[AgentEvent],
        *,
        exposure: CapabilityExposurePlan,
        reservations: dict[str, RolloutReservationFact],
    ) -> AsyncIterator[AgentEvent]:
        tap = _ToolBatchTap({call.id for call in batch})
        self.runtime_session.publisher.subscribe(tap)
        executor = ToolExecutor(
            registry=self.tool_executor.registry,
            record_event=self.runtime_session.make_thread_recorder(state=state),
            artifact_service=self.tool_executor.artifact_service,
            runtime_session_id=self.runtime_session.runtime_session_id,
            semantics_registry=self.tool_executor.semantics_registry,
            essential_capture_policy=(self.tool_executor.essential_capture_policy),
        )

        async def execute_call(
            call: ToolCall,
        ) -> ToolExecutionResult | ToolExecutionSuspended:
            descriptor = exposure.descriptors_by_name.get(call.name)
            borrow_authority = state.scratchpad.get(
                "capability_execution_borrow_authority"
            )
            borrow_kind = str(
                state.scratchpad.get("capability_execution_borrow_kind", "parent")
            )
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
                        context_id=_optional_scratchpad_str(
                            state, "current_context_id"
                        ),
                        model_call_index=_optional_scratchpad_int(
                            state, "current_model_call_index"
                        ),
                        **self._tool_permission_kwargs(state),
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
                    context_id=_optional_scratchpad_str(state, "current_context_id"),
                    model_call_index=_optional_scratchpad_int(
                        state, "current_model_call_index"
                    ),
                    **self._tool_permission_kwargs(state),
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
            self.runtime_session.publisher.unsubscribe(tap)
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
                    )

    async def _suspend_tool_execution(
        self,
        state: LoopState,
        suspended: ToolExecutionSuspended,
        *,
        reservation: RolloutReservationFact,
    ) -> AsyncIterator[AgentEvent]:
        payload = dict(suspended.payload)
        payload.setdefault(
            "interaction_id", f"{suspended.interaction_kind}:{uuid4().hex}"
        )
        payload.setdefault("tool_call_id", suspended.tool_call_id)
        payload.setdefault("tool_name", suspended.tool_name)
        payload.setdefault("run_id", state.run_id)
        payload["rollout_reservation_id"] = reservation.reservation_id
        payload["rollout_reservation_fingerprint"] = reservation.semantic_fingerprint
        if suspended.interaction_kind == "mcp_input_required":
            payload.setdefault(
                "mcp_installation_id",
                self.runtime_session.mcp_installation_id,
            )
        original_pending_tool_calls = list(state.pending_tool_calls)
        original_pending_kind = state.pending_interaction_kind
        original_pending_payload = dict(state.pending_interaction_payload)
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
            **self._event_context(state).event_fields(),
            interaction_kind=suspended.interaction_kind,
            tool_call_id=suspended.tool_call_id,
            tool_name=suspended.tool_name,
            payload=payload,
        )
        supervisor = self.runtime_session.mcp_supervisor
        reservation_id = payload.get("mcp_pending_lease_reservation_id")
        interaction_id = str(payload["interaction_id"])
        terminal_registry = self.runtime_session.tool_execution_terminal_registry
        terminal_registry.freeze_suspension(
            run_id=state.run_id,
            reservation=reservation,
            candidates=(suspension_event,),
        )
        try:
            commit_result = await RuntimeSessionToolExecutionEventCommitPort(
                runtime_session=self.runtime_session,
                state=state,
            ).commit_suspension(
                suspension_candidate=suspension_event,
                reservation_id=reservation.reservation_id,
                expected_reservation_fingerprint=reservation.semantic_fingerprint,
            )
            stored = commit_result.committed_events[0]
            if commit_result.reconciliation_required:
                raise RuntimeError(
                    "tool suspension committed without a healthy reducer fold"
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
        except BaseException as suspension_error:
            outcome = self.runtime_session.resolved_event_write_outcome(
                suspension_error
            )
            if outcome.status == "unknown":
                # UNKNOWN keeps both the WAITING_USER carrier and the sole
                # Supervisor reservation, then fails closed.
                terminal_registry.mark_commit_outcome_unknown(
                    run_id=state.run_id,
                    reservation=reservation,
                )
                raise
            if outcome.status == "none":
                state.pending_tool_calls = original_pending_tool_calls
                state.pending_interaction_kind = original_pending_kind
                state.pending_interaction_payload = original_pending_payload
                state.status = original_status
                state.stop_reason = original_stop_reason
                state.last_transition = original_transition
                if supervisor is not None and isinstance(reservation_id, str):
                    supervisor.abort_pending_lease(interaction_id, reservation_id)
                async for _event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=suspended.tool_call_id,
                    tool_call_name=suspended.tool_name,
                    output=(
                        "Tool suspension could not be durably recorded; "
                        "the admitted call was terminated fail-closed."
                    ),
                    result_state=ToolResultState.ERROR,
                    tool_observation_timing_seed=dict(
                        payload.get("tool_observation_timing_seed") or {}
                    )
                    or None,
                    tool_arguments=dict(
                        (payload.get("original_request") or {}).get(
                            "arguments", {}
                        )
                    ),
                    rollout_reservation=reservation,
                ):
                    pass
                raise suspension_error
            stored = next(
                event
                for event in outcome.committed_events
                if event.id == suspension_event.id
            )
            terminal_registry.mark_suspended(
                run_id=state.run_id,
                reservation=reservation,
            )
            if supervisor is not None and isinstance(reservation_id, str):
                supervisor.confirm_pending_lease(interaction_id, reservation_id)
            raise
        terminal_registry.mark_suspended(
            run_id=state.run_id,
            reservation=reservation,
        )
        if supervisor is not None and isinstance(reservation_id, str):
            supervisor.confirm_pending_lease(interaction_id, reservation_id)
        yield stored

    def _recover_or_fail_model(self, state: LoopState) -> bool:
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

    def _event_context(self, state: LoopState) -> EventContext:
        return EventContext(
            run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id
        )


def _next_model_call_index(state: LoopState) -> int:
    value = state.scratchpad.get("model_call_index")
    if not isinstance(value, int):
        value = 0
    value += 1
    state.scratchpad["model_call_index"] = value
    return value


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


def _tool_call_in_flight(state: LoopState) -> bool:
    authority = state.scratchpad.get("capability_execution_borrow_authority")
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
        event = frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
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


def _optional_scratchpad_str(state: LoopState, key: str) -> str | None:
    value = state.scratchpad.get(key)
    return value if isinstance(value, str) else None


def _run_start_for_id(runtime_session: RuntimeSession, *, run_id: str) -> RunStartEvent:
    start = runtime_session.long_horizon_state_store.run_start(run_id)
    if start is None:
        raise RuntimeError("run terminalization requires exactly one RunStart")
    return start


def _tool_result_boundary_events(
    event_log: EventLog,
    *,
    run_id: str,
    tool_call_id: str,
    start_event_id: str | None = None,
) -> list[AgentEvent]:
    ids = (
        start_event_id or f"tool_result_start:{run_id}:{tool_call_id}",
        f"tool_result_end:{run_id}:{tool_call_id}",
    )
    decoded = [
        raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for raw in event_log.read_raw_events_by_id(ids)
    ]
    if any(
        event.run_id != run_id
        or getattr(event, "tool_call_id", None) != tool_call_id
        or not isinstance(event, (ToolResultStartEvent, ToolResultEndEvent))
        for event in decoded
    ):
        raise RuntimeError("tool-result boundary exact reference identity mismatch")
    return decoded


def _completed_tool_result_events(
    event_log: EventLog,
    *,
    run_id: str,
    tool_call_id: str,
    start_event_id: str | None = None,
) -> list[AgentEvent]:
    boundaries = _tool_result_boundary_events(
        event_log,
        run_id=run_id,
        tool_call_id=tool_call_id,
        start_event_id=start_event_id,
    )
    starts = tuple(
        event for event in boundaries if isinstance(event, ToolResultStartEvent)
    )
    ends = tuple(event for event in boundaries if isinstance(event, ToolResultEndEvent))
    if not ends:
        return []
    if len(starts) != 1 or len(ends) != 1:
        raise RuntimeError("completed tool result lacks unique boundaries")
    start_sequence = starts[0].sequence
    end_sequence = ends[0].sequence
    if (
        start_sequence is None
        or end_sequence is None
        or end_sequence < start_sequence
    ):
        raise RuntimeError("completed tool-result sequence range is invalid")
    snapshot = event_log.read_raw_range_snapshot(
        minimum_sequence=start_sequence,
        through_sequence=end_sequence,
        max_events=4_096,
        max_payload_bytes=16 * 1024 * 1024,
    )
    return [
        decoded
        for raw in snapshot.events
        if (decoded := raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)).run_id
        == run_id
        and getattr(decoded, "tool_call_id", None) == tool_call_id
        and isinstance(
            decoded,
            (
                ToolResultStartEvent,
                ToolResultTextDeltaEvent,
                ToolResultDataDeltaEvent,
                ToolResultEndEvent,
            ),
        )
    ]


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


def _optional_scratchpad_int(state: LoopState, key: str) -> int | None:
    value = state.scratchpad.get(key)
    return value if isinstance(value, int) else None


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


def _remove_plan_runtime_instructions(state: LoopState) -> None:
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
) -> Literal["memory", "working_context", "mixed"]:
    raw = projection.get("projection_kind") if projection else None
    if raw in {"working_context", "mixed"}:
        return raw
    return "memory"


def _accepted_plan_artifact_id(run_id: str, exit_request_id: str) -> str:
    return f"artifact:plan:{_sanitize_artifact_part(run_id)}:{_sanitize_artifact_part(exit_request_id)}:accepted"


def _sanitize_artifact_part(value: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
        or "unknown"
    )
