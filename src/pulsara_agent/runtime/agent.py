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
from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    CapabilityGateDecisionEvent,
    ConfirmResult,
    ContextCompiledEvent,
    CustomEvent,
    EventContext,
    ExceedMaxItersEvent,
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
    ToolResultEndEvent,
    ToolResultDataDeltaEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.event.events import utc_now
from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import (
    Msg,
    SystemMsg,
    ToolCallBlock,
    ToolCallState,
    ToolResultState,
)
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.runtime.context import build_compiled_context
from pulsara_agent.runtime.context_engine import (
    ContextBudgetExceeded,
    ContextLifecycleCoordinator,
)
from pulsara_agent.runtime.context_engine.tool_results import (
    ToolResultRenderDecisionCache,
    make_tool_result_render_decision_cache,
    render_segmented_llm_messages,
)
from pulsara_agent.runtime.approval import ApprovalResolution
from pulsara_agent.runtime.compaction.inline import (
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
from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    SubagentRunEntryFact,
    text_sha256,
)
from pulsara_agent.primitives.subagent import (
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
    McpInputRequiredInteractionResolution,
    PlanExitResolution,
    PlanInteractionResolution,
    PlanQuestionResolution,
    PlanWorkflowState,
    normalize_plan_question_options,
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
    deferred_release = False
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError as cancelled:
        deferred_release = True

        def release_after_real_thread_completion(
            completed: asyncio.Task[ToolExecutionResult | ToolExecutionSuspended],
        ) -> None:
            try:
                completed.exception()
            except BaseException:
                pass
            finally:
                release_borrow()

        thread_task.add_done_callback(release_after_real_thread_completion)
        # The tool thread may still emit result events or mutate external
        # state.  Keep the run task alive until that real execution boundary
        # closes; Host stop/close applies its own bounded deadline around this
        # task and therefore blocks teardown without writing RunEnd early.
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        raise cancelled
    finally:
        if not deferred_release:
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
    allowed_skill_names = frozenset(
        getattr(profile, "allowed_skill_names", ()) or ()
    )
    if not allowed_tool_names and not allowed_descriptor_ids and not allowed_skill_names:
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


def compose_system_prompt(
    base: str | None,
    *,
    runtime_context_prompt: str | None = None,
    memory_prompt: str | None = None,
    capability_prompt: str | None = None,
    active_skill_prompt: str | None = None,
) -> str | None:
    parts = [
        part
        for part in (
            base,
            runtime_context_prompt,
            memory_prompt,
            capability_prompt,
            active_skill_prompt,
        )
        if part
    ]
    if not parts:
        return None
    return "\n\n".join(parts)


def _with_memory_context_prompt(
    system_prompt: str | None, memory_prompt: str | None
) -> str | None:
    return compose_system_prompt(system_prompt, memory_prompt=memory_prompt)


def render_runtime_context_prompt(
    *,
    workspace_root: str,
    workspace_kind: WorkspaceKind,
    terminal_current_cwd: str,
) -> str:
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_text = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC offset unknown"
    timezone_name = now.tzname() or offset_text
    workspace_mode = (
        "project workspace; treat workspace facts as durable project context."
        if workspace_kind == "project"
        else "transient scratch workspace; do not treat workspace facts as durable project context."
    )
    return "\n".join(
        [
            "<runtime-context>",
            f"Current date: {now.date().isoformat()}",
            f"Local timezone: {timezone_name} ({offset_text})",
            f"Workspace kind: {workspace_kind} ({workspace_mode})",
            f"Workspace root: {workspace_root}",
            f"Terminal current cwd: {terminal_current_cwd}",
            "Terminal workdir, when provided, must stay inside workspace_root; when unsure, omit workdir or run pwd.",
            "Relative terminal workdir values resolve from workspace_root.",
            "Read-only filesystem tools may read ordinary text files outside workspace_root, but write/edit tools and terminal workdir remain workspace-scoped.",
            "</runtime-context>",
        ]
    )


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
        self.context_lifecycle = ContextLifecycleCoordinator()
        self.tool_result_render_decision_cache: ToolResultRenderDecisionCache = (
            make_tool_result_render_decision_cache()
        )
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
        self.runtime_session.subagent_runtime = self.subagent_runtime
        self._subagent_dangling_repair_done = False
        self._mcp_terminal_commit_outcomes: dict[
            tuple[str, str], Literal["not_attempted", "attempting", "none", "full", "untrusted"]
        ] = {}
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
                    await self._complete_mcp_pending_lease(
                        resolution.interaction_id
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

    def close(self) -> None:
        self.runtime_session.close()

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
        if not run_view.task_text_complete or run_view.task_text is None:
            raise ValueError(
                "child subagent run requires a fully hydrated task artifact"
            )
        if run.task_artifact_id is None:
            raise ValueError("child subagent run requires a durable task artifact")
        child_state = child_agent.new_state()
        child_target = child_agent.resolve_run_model_target()
        child_permission = child_agent._capture_run_permission_snapshot(child_state)
        child_run_start_id = f"run_start:subagent:{uuid4().hex}"
        task_observed_at = run.created_at.isoformat()
        render_policy = build_child_result_render_policy(
            renderer_version="subagent-result:v1",
            max_summary_chars=run.budget_snapshot.max_result_summary_chars_per_child,
            max_artifact_refs=run.budget_snapshot.max_result_artifact_refs_per_child,
        )
        validate_child_render_policy_against_budget(
            render_policy, run.budget_snapshot
        )
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
        )
        entry_bundle = await SubagentRunEntryDriver().prepare_and_commit(
            child_agent=child_agent,
            state=child_state,
            prepared=prepared_child_entry,
            prior_messages=[],
        )
        result = await child_agent.run_committed_entry(
            entry_bundle.draft,
            entry_bundle.committed,
        )
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
        await subagent_runtime.fail(
            run.subagent_run_id,
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
                if isinstance(exc, asyncio.CancelledError) and state.stop_request is not None:
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
                confirmation = self.runtime_session.confirm_event_batch(
                    (exposure_event,)
                )
                if confirmation.missing_event_ids:
                    raise
                confirmed = tuple(confirmation.committed_events)
            if len(confirmed) != 1 or not isinstance(
                confirmed[0], CapabilityExposureResolvedEvent
            ):
                raise RuntimeError(
                    "initial capability exposure confirmation was not exact"
                ) from exc
            self._require_run_working_set(state).install_initial_exposure(
                plan=exposure,
                fact=resolved_exposure.fact,
            )
            raise
        if not isinstance(stored_exposure, CapabilityExposureResolvedEvent):
            raise RuntimeError("capability exposure commit returned wrong event type")
        self._require_run_working_set(state).install_initial_exposure(
            plan=exposure,
            fact=resolved_exposure.fact,
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
        )

    async def _emit_capability_gate_decision(
        self,
        state: LoopState,
        fact: CapabilityGateDecisionFact,
    ) -> AsyncIterator[AgentEvent]:
        yield await self.runtime_session.emit(
            CapabilityGateDecisionEvent(
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
                availability=fact.availability.value
                if fact.availability is not None
                else None,
                permission_category=fact.permission_category,
                effective_permission_category=fact.effective_permission_category,
                effective_read_only=fact.effective_read_only,
                capability_context=fact.capability_context,
            ),
            state=state,
        )

    async def _emit_capability_access_denial(
        self,
        state: LoopState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        result_state = (
            ToolResultState.ERROR
            if decision.reason and "capability_descriptor_missing" in decision.reason
            else ToolResultState.DENIED
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=decision.reason or "tool call denied by capability exposure",
            result_state=result_state,
            tool_observation_timing_seed=tool_observation_timing_seed,
        ):
            yield event
        fact = self._capability_gate_decision_fact(
            state,
            call,
            exposure=exposure,
            decision=decision,
            result_state=result_state,
        )
        async for event in self._emit_capability_gate_decision(state, fact):
            yield event

    async def _emit_permission_gate_denial(
        self,
        state: LoopState,
        call: ToolCall,
        *,
        exposure: CapabilityExposurePlan,
        decision: PermissionDecision,
        tool_observation_timing_seed: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        result_state = (
            ToolResultState.ERROR
            if decision.reason and "capability_descriptor_missing" in decision.reason
            else ToolResultState.DENIED
        )
        stored_events = await self.runtime_session.emit_many(
            build_tool_result_error_events(
                self._event_context(state),
                tool_call_id=call.id,
                tool_call_name=call.name,
                message=decision.reason or "tool call denied by permission gate",
                state=result_state,
                tool_observation_timing_seed=tool_observation_timing_seed,
            ),
            state=state,
        )
        for event in stored_events:
            yield event
        fact = self._capability_gate_decision_fact(
            state,
            call,
            exposure=exposure,
            decision=decision,
            result_state=result_state,
        )
        async for event in self._emit_capability_gate_decision(state, fact):
            yield event
        result_block = _tool_result_from_event_slice(stored_events, call.id)
        _remember_tool_result_event_span(state, stored_events, call.id)
        state.tool_results.append(result_block)
        state.messages.append(
            _tool_result_message_from_events(stored_events, call.name, result_block)
        )

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
        state.scratchpad["plan_entry_audit_emitted"] = True
        yield event

    async def _stream_model_loop(
        self,
        state: LoopState,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent]:
        while state.status is LoopStatus.RUNNING:
            if self._apply_stop_request(state):
                break
            if state.turn_index >= self.budget.max_turns:
                state.status = LoopStatus.FAILED
                state.stop_reason = RunStopReason.MAX_TURNS
                state.error_message = (
                    f"agent turn budget exceeded: max_turns={self.budget.max_turns}"
                )
                state.transition(LoopTransition.EXCEED_MAX_ITERS)
                event = await self.runtime_session.emit(
                    ExceedMaxItersEvent(
                        **self._event_context(state).event_fields(),
                        name="agent_runtime",
                        max_iters=self.budget.max_turns,
                    ),
                    state=state,
                )
                yield event
                break

            async for event in self._project_memory(state):
                yield event

            model_call_index = _next_model_call_index(state)
            resolved_call = self.llm_runtime.resolve_call(
                target=self._require_run_model_target(state),
                purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            )
            runtime_context_prompt = render_runtime_context_prompt(
                workspace_root=str(self.runtime_session.workspace_root),
                workspace_kind=self.workspace_kind,
                terminal_current_cwd=str(
                    self.runtime_session.terminal_sessions.current_cwd(
                        owner_host_session_id=self.runtime_session.terminal_owner_host_session_id
                    )
                ),
            )
            memory_prompt = getattr(
                self.memory_hooks, "memory_context_prompt", lambda: None
            )()
            subagent_results_prompt: str | None = None
            pending_subagent_results = ()
            if (
                self._subagent_parent_features_enabled
                and self.subagent_runtime is not None
            ):
                subagent_results_prompt, pending_subagent_results = (
                    self.subagent_runtime.render_pending_results_section(
                        max_results=self.budget.max_subagent_results_per_parent_compile,
                    )
                )
            compiled_context = None
            compile_attempt_index = 0
            context_retry_index = 0
            while state.status is LoopStatus.RUNNING:
                compile_attempt_index += 1
                try:
                    compiled_context = build_compiled_context(
                        state=state,
                        tools=exposure.direct_tool_specs,
                        system_prompt=self.system_prompt,
                        budget=self.budget,
                        context_id=f"context:{uuid4().hex}",
                        model_call_index=model_call_index,
                        resolved_call=resolved_call,
                        exposure=exposure,
                        current_user_anchor=f"user-message:{state.run_id}",
                        runtime_session_id=self.runtime_session.runtime_session_id,
                        component_prompts=tuple(
                            (component_id, text)
                            for component_id, text in (
                                ("runtime_context", runtime_context_prompt),
                                ("memory:hook_prompt", memory_prompt),
                                ("capability:catalog", exposure.catalog_prompt),
                                (
                                    "capability:active_skill",
                                    exposure.active_skill_prompt,
                                ),
                                (_SUBAGENT_RESULTS_SECTION_ID, subagent_results_prompt),
                            )
                            if text
                        ),
                        lifecycle_coordinator=self.context_lifecycle,
                        tool_result_render_decision_cache=self.tool_result_render_decision_cache,
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
                    yield await self.runtime_session.emit(
                        ContextCompiledEvent(
                            **self._event_context(state).event_fields(),
                            status="pressure",
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
                        ),
                        state=state,
                    )
                    if (
                        context_retry_index == 0
                        and _context_budget_pressure_is_recoverable(exc)
                    ):
                        previous_mid_turn_compaction = state.scratchpad.get(
                            "mid_turn_compaction"
                        )
                        async for event in self._maybe_compact_mid_turn_before_followup(
                            state
                        ):
                            yield event
                        if (
                            state.scratchpad.get("mid_turn_compaction")
                            != previous_mid_turn_compaction
                        ):
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
            if compiled_context is None:
                break
            state.scratchpad["current_context_id"] = compiled_context.context_id
            state.scratchpad["current_model_call_index"] = model_call_index
            yield await self.runtime_session.emit(
                ContextCompiledEvent(
                    **self._event_context(state).event_fields(),
                    context_id=compiled_context.context_id,
                    model_call_index=model_call_index,
                    compile_attempt_index=compile_attempt_index,
                    context_retry_index=context_retry_index,
                    resolved_call=resolved_call.fact,
                    budget=compiled_context.budget.to_event_value(),
                    sections=[
                        section.to_event_value()
                        for section in compiled_context.sections
                    ],
                    tool_specs=[
                        tool.to_event_value() for tool in compiled_context.tool_specs
                    ],
                    diagnostics=[
                        diagnostic.to_event_value()
                        for diagnostic in compiled_context.diagnostics
                    ],
                    lifecycle_decisions=[
                        decision.to_event_value()
                        for decision in compiled_context.lifecycle_decisions
                    ],
                    tool_result_render_decisions=[
                        dict(decision)
                        for decision in compiled_context.tool_result_render_decisions
                    ],
                    tool_result_budget_report=dict(
                        compiled_context.tool_result_budget_report
                    ),
                ),
                state=state,
            )
            context = replace(
                compiled_context.llm_context,
                resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
                target_fingerprint=resolved_call.target.fact.target_fingerprint,
            )
            deliverable_subagent_results = (
                pending_subagent_results
                if _compiled_section_included(
                    compiled_context, _SUBAGENT_RESULTS_SECTION_ID
                )
                else ()
            )
            delivered_subagent_results = False

            reply_had_run_error = False
            try:
                async for event in self.llm_runtime.stream(
                    call=resolved_call,
                    context=context,
                    event_context=self._event_context(state),
                ):
                    stored = await self.runtime_session.emit(event, state=state)
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
                if not self._recover_or_fail_model(state):
                    break
                state.begin_next_turn()
                continue

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
                            _plan_revision_required_instruction(
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
                tool_observation_timing_seed={**timing_seed, "resumed_at": utc_now()}
                if timing_seed
                else None,
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
                tool_observation_timing_seed=(
                    {**timing_seed, "resumed_at": utc_now()}
                    if timing_seed
                    else None
                ),
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
                                tool_observation_timing_seed=(
                                    {**timing_seed, "resumed_at": utc_now()}
                                    if timing_seed
                                    else None
                                ),
                            ):
                                yield event
                            async for event in self._after_mcp_resume_terminal_result(
                                state,
                                interaction_id=resolution.interaction_id,
                            ):
                                yield event
                            return
                        async for event in self._suspend_tool_execution(state, result):
                            yield event
                        return
                    async for event in self._emit_tool_result_and_record(
                        state,
                        tool_call_id=tool_call_id,
                        tool_call_name=tool_name,
                        output=result.output,
                        result_state=result.status,
                        tool_observation_timing_seed={
                            **timing_seed,
                            "resumed_at": utc_now(),
                        }
                        if timing_seed
                        else None,
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
    ) -> AsyncIterator[AgentEvent]:
        model_visible_messages = [
            message.model_copy(deep=True) for message in state.messages
        ]
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = ()
        if state.run_model_target is not None:
            current_user_anchor = f"user-message:{state.run_id}"
            segmented = render_segmented_llm_messages(
                model_visible_messages,
                self.budget,
                current_user_anchor,
                token_estimator=state.run_model_target.token_estimator,
                decision_cache=self.tool_result_render_decision_cache,
            )
            protected_model_visible_messages_after = (
                *(segmented.current_user_messages or ()),
                *(segmented.current_run_tail_messages or ()),
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
        for event in result.events:
            yield event

    async def _continue_after_tool_before_followup(
        self,
        state: LoopState,
    ) -> AsyncIterator[AgentEvent]:
        state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
        async for event in self._maybe_compact_mid_turn_before_followup(state):
            yield event
        state.begin_next_turn()

    async def _resolve_plan_question(
        self,
        state: LoopState,
        payload: dict,
        resolution: PlanQuestionResolution,
    ) -> AsyncIterator[AgentEvent]:
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
        ):
            yield event

    async def _resolve_plan_exit(
        self,
        state: LoopState,
        payload: dict,
        resolution: PlanExitResolution,
    ) -> AsyncIterator[AgentEvent]:
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
            yield await self.runtime_session.emit(
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
            plan_state.finish(
                accepted_plan_summary=accepted_summary,
                accepted_plan_artifact_id=accepted_artifact_id,
            )
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
        pending = state.scratchpad.get("pending_run_end_candidate")
        if isinstance(pending, RunEndEvent):
            candidate = pending
        elif pending is not None:
            raise RuntimeError("pending RunEnd candidate has invalid type")
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
        state.scratchpad["pending_run_end_candidate"] = candidate
        try:
            stored = await self.runtime_session.emit(candidate, state=state)
        except BaseException as exc:
            if isinstance(exc, EventPublicationAfterCommitError):
                confirmed = tuple(exc.result.committed_events)
            else:
                try:
                    confirmation = self.runtime_session.confirm_event_batch(
                        (candidate,)
                    )
                except BaseException:
                    self.runtime_session.latch_event_commit_outcome_unknown()
                    raise
                if confirmation.missing_event_ids:
                    state.scratchpad["run_end_commit_state"] = "pending"
                    try:
                        stored_retry = await self.runtime_session.emit(
                            candidate, state=state
                        )
                    except BaseException as retry_error:
                        if isinstance(
                            retry_error, EventPublicationAfterCommitError
                        ):
                            retry_confirmed = tuple(
                                retry_error.result.committed_events
                            )
                        else:
                            try:
                                retry_confirmation = (
                                    self.runtime_session.confirm_event_batch(
                                        (candidate,)
                                    )
                                )
                            except BaseException:
                                self.runtime_session.latch_event_commit_outcome_unknown()
                                raise
                            if retry_confirmation.missing_event_ids:
                                raise
                            retry_confirmed = tuple(
                                retry_confirmation.committed_events
                            )
                        if len(retry_confirmed) != 1 or not isinstance(
                            retry_confirmed[0], RunEndEvent
                        ):
                            self.runtime_session.latch_event_commit_outcome_unknown()
                            raise RuntimeError(
                                "RunEnd retry confirmation was not exact"
                            ) from retry_error
                        state.finalized = True
                        state.scratchpad["run_end_commit_state"] = "committed"
                        state.scratchpad.pop("pending_run_end_candidate", None)
                        raise
                    if not isinstance(stored_retry, RunEndEvent):
                        raise RuntimeError(
                            "RunEnd bounded retry returned wrong event type"
                        )
                    state.finalized = True
                    state.scratchpad["run_end_commit_state"] = "committed"
                    state.scratchpad.pop("pending_run_end_candidate", None)
                    yield stored_retry
                    return
                confirmed = tuple(confirmation.committed_events)
            if len(confirmed) != 1 or not isinstance(confirmed[0], RunEndEvent):
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise RuntimeError("RunEnd confirmation was not exact") from exc
            state.finalized = True
            state.scratchpad["run_end_commit_state"] = "committed"
            state.scratchpad.pop("pending_run_end_candidate", None)
            raise
        if not isinstance(stored, RunEndEvent):
            raise RuntimeError("RunEnd commit returned wrong event type")
        state.finalized = True
        state.scratchpad["run_end_commit_state"] = "committed"
        state.scratchpad.pop("pending_run_end_candidate", None)
        yield stored

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

    async def _mark_tool_budget_exceeded(
        self, state: LoopState, *, attempted_count: int
    ) -> AgentEvent:
        message = (
            "tool call budget exceeded before execution: "
            f"current={state.tool_call_count}, attempted={attempted_count}, max={self.budget.max_tool_calls}"
        )
        state.status = LoopStatus.FAILED
        state.stop_reason = RunStopReason.TOOL_ERROR_BUDGET
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="tool_budget_exceeded",
                metadata={
                    "current_tool_call_count": state.tool_call_count,
                    "attempted_tool_call_count": attempted_count,
                    "max_tool_calls": self.budget.max_tool_calls,
                },
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
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
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
            unique_calls: list[ToolCall] = []
            for call in parsed_calls:
                if call.id not in duplicate_ids:
                    unique_calls.append(call)
                    continue
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=f"Duplicate tool_call_id in assistant reply: {call.id}",
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
            async for event in self._emit_workflow_gate_decisions(
                state, executable_calls, exposure=exposure
            ):
                yield event
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

        for call in executable_calls:
            fact = self._capability_gate_decision_fact(
                state,
                call,
                exposure=exposure,
                decision=decision,
            )
            async for event in self._emit_capability_gate_decision(state, fact):
                yield event

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
        allow_fact = self._capability_gate_decision_fact(
            state,
            workflow_call,
            exposure=exposure,
            decision=PermissionDecision.allow(),
        )
        async for event in self._emit_capability_gate_decision(state, allow_fact):
            yield event
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
        try:
            if workflow_call.name == "enter_plan":
                async for event in self._execute_enter_plan(state, workflow_call):
                    yield event
            elif workflow_call.name == "ask_plan_question":
                async for event in self._execute_ask_plan_question(
                    state, workflow_call
                ):
                    yield event
            elif workflow_call.name == "exit_plan":
                async for event in self._execute_exit_plan(state, workflow_call):
                    yield event
            else:
                async for event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=workflow_call.id,
                    tool_call_name=workflow_call.name,
                    output=f"unknown workflow tool: {workflow_call.name}",
                    result_state=ToolResultState.ERROR,
                ):
                    yield event
        except Exception as exc:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=workflow_call.id,
                tool_call_name=workflow_call.name,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
                result_state=ToolResultState.ERROR,
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
            ):
                yield event

    async def _execute_enter_plan(
        self, state: LoopState, call: ToolCall
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
        yield await self.runtime_session.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="agent",
                previous_permission_mode=previous_mode.value,
                previous_permission_policy=previous_policy.to_dict(),
                reason=reason,
            ),
            state=state,
        )
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
        ):
            yield event
        state.status = LoopStatus.FINISHED
        state.stop_reason = RunStopReason.FINAL
        state.transition(LoopTransition.FINISH)

    async def _execute_ask_plan_question(
        self, state: LoopState, call: ToolCall
    ) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="ask_plan_question can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(
                state, call, kind="interaction"
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
        }
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _execute_exit_plan(
        self, state: LoopState, call: ToolCall
    ) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="exit_plan can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(
                state, call, kind="interaction"
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
    ) -> AsyncIterator[AgentEvent]:
        candidates = tuple(
            build_tool_result_error_events(
                self._event_context(state),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                message=output,
                state=result_state,
                tool_observation_timing_seed=tool_observation_timing_seed,
            )
        )
        commit_outcome_key = (state.run_id, tool_call_id)
        track_mcp_terminal = (
            commit_outcome_key in self._mcp_terminal_commit_outcomes
        )
        if track_mcp_terminal:
            self._mcp_terminal_commit_outcomes[commit_outcome_key] = "attempting"
        try:
            stored_events = await self.runtime_session.emit_many(
                candidates,
                state=state,
            )
        except EventPublicationAfterCommitError as exc:
            if track_mcp_terminal:
                self._mcp_terminal_commit_outcomes[commit_outcome_key] = "full"
            stored_events = list(exc.result.committed_events)
            self._record_tool_result_events(
                state,
                stored_events=stored_events,
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
            raise
        except BaseException:
            try:
                confirmation = self.runtime_session.confirm_event_batch(candidates)
            except BaseException:
                # A failed confirmation is UNKNOWN, never NONE.  Preserve any
                # external resource owner and block further mutation/teardown.
                if track_mcp_terminal:
                    self._mcp_terminal_commit_outcomes[commit_outcome_key] = (
                        "untrusted"
                    )
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise
            if confirmation.missing_event_ids:
                # The complete stable batch is absent, so the caller may safely
                # restore its pre-write process-local state and retry.
                if track_mcp_terminal:
                    self._mcp_terminal_commit_outcomes[commit_outcome_key] = "none"
                raise
            if track_mcp_terminal:
                self._mcp_terminal_commit_outcomes[commit_outcome_key] = "full"
            stored_events = list(confirmation.committed_events)
            self._record_tool_result_events(
                state,
                stored_events=stored_events,
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
            raise
        if track_mcp_terminal:
            self._mcp_terminal_commit_outcomes[commit_outcome_key] = "full"
        for event in stored_events:
            yield event
        self._record_tool_result_events(
            state,
            stored_events=stored_events,
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

    def _committed_tool_result_events(
        self,
        state: LoopState,
        *,
        tool_call_id: str,
    ) -> list[AgentEvent]:
        events = [
            event
            for event in self.runtime_session.event_log.iter()
            if event.run_id == state.run_id
            and getattr(event, "tool_call_id", None) == tool_call_id
            and isinstance(
                event,
                (
                    ToolResultStartEvent,
                    ToolResultTextDeltaEvent,
                    ToolResultDataDeltaEvent,
                    ToolResultEndEvent,
                ),
            )
        ]
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
    ) -> AsyncIterator[AgentEvent]:
        message = f"plan {kind} budget exceeded"
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=message,
            result_state=ToolResultState.ERROR,
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
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message="tool call denied by user approval",
                        state=ToolResultState.DENIED,
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
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
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
        for batch in _tool_batches(parsed_calls, self.tool_executor, exposure=exposure):
            if state.tool_call_count + len(batch) > self.budget.max_tool_calls:
                yield await self._mark_tool_budget_exceeded(
                    state, attempted_count=len(batch)
                )
                return
            batch_events: list[AgentEvent] = []
            async for event in self._stream_tool_batch_events(
                state, batch, batch_events, exposure=exposure
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
                state.tool_call_count += 1

    async def _stream_tool_batch_events(
        self,
        state: LoopState,
        batch: list[ToolCall],
        batch_events: list[AgentEvent],
        *,
        exposure: CapabilityExposurePlan,
    ) -> AsyncIterator[AgentEvent]:
        tap = _ToolBatchTap({call.id for call in batch})
        self.runtime_session.publisher.subscribe(tap)
        executor = ToolExecutor(
            registry=self.tool_executor.registry,
            record_event=self.runtime_session.make_thread_recorder(state=state),
            artifact_service=self.tool_executor.artifact_service,
            runtime_session_id=self.runtime_session.runtime_session_id,
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
                    context_id=_optional_scratchpad_str(
                        state, "current_context_id"
                    ),
                    model_call_index=_optional_scratchpad_int(
                        state, "current_model_call_index"
                    ),
                    **self._tool_permission_kwargs(state),
                ),
                release_borrow=release_borrow,
            )

        tasks = [asyncio.create_task(execute_call(call)) for call in batch]
        pending = set(tasks)
        completed_tool_calls: set[str] = set()

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
                if pending:
                    done, pending = await asyncio.wait(
                        pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        outcome = task.result()
                        if isinstance(outcome, ToolExecutionSuspended):
                            async for event in self._suspend_tool_execution(
                                state, outcome
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
                    continue
                if len(completed_tool_calls) < len(batch):
                    event = await tap.queue.get()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
        finally:
            self.runtime_session.publisher.unsubscribe(tap)
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _suspend_tool_execution(
        self,
        state: LoopState,
        suspended: ToolExecutionSuspended,
    ) -> AsyncIterator[AgentEvent]:
        payload = dict(suspended.payload)
        payload.setdefault(
            "interaction_id", f"{suspended.interaction_kind}:{uuid4().hex}"
        )
        payload.setdefault("tool_call_id", suspended.tool_call_id)
        payload.setdefault("tool_name", suspended.tool_name)
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
        suspension_event = CustomEvent(
            **self._event_context(state).event_fields(),
            name="tool_execution_suspended",
            value={
                "interaction_kind": suspended.interaction_kind,
                "tool_call_id": suspended.tool_call_id,
                "tool_name": suspended.tool_name,
                "payload": payload,
            },
        )
        supervisor = self.runtime_session.mcp_supervisor
        reservation_id = payload.get("mcp_pending_lease_reservation_id")
        interaction_id = str(payload["interaction_id"])
        try:
            stored = await self.runtime_session.emit(suspension_event, state=state)
        except EventPublicationAfterCommitError as exc:
            # The suspension fact is already canonical.  Preserve/confirm its
            # process-local lease owner and surface the committed event rather
            # than turning a hook failure into an unrecoverable pending record.
            stored = next(
                event
                for event in exc.result.committed_events
                if event.id == suspension_event.id
            )
        except BaseException:
            try:
                confirmation = self.runtime_session.confirm_event_batch(
                    (suspension_event,)
                )
            except BaseException:
                # Confirmation failure is UNKNOWN.  Keep both the WAITING_USER
                # carrier and the sole Supervisor reservation, then fail closed.
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise
            if confirmation.missing_event_ids:
                state.pending_tool_calls = original_pending_tool_calls
                state.pending_interaction_kind = original_pending_kind
                state.pending_interaction_payload = original_pending_payload
                state.status = original_status
                state.stop_reason = original_stop_reason
                state.last_transition = original_transition
                if supervisor is not None and isinstance(reservation_id, str):
                    supervisor.abort_pending_lease(interaction_id, reservation_id)
                raise
            stored = next(
                event
                for event in confirmation.committed_events
                if event.id == suspension_event.id
            )
            if supervisor is not None and isinstance(reservation_id, str):
                supervisor.confirm_pending_lease(interaction_id, reservation_id)
            raise
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
    report = exc.tool_result_budget_report
    diagnostics = report.get("diagnostics") if isinstance(report, dict) else None
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, dict)
        and diagnostic.get("code")
        in {
            "tool_result_total_budget_unsatisfied",
            "max_tool_results_per_context_exceeded",
        }
        for diagnostic in diagnostics
    )


def _compiled_section_included(compiled_context, section_id: str) -> bool:
    return any(
        section.id == section_id and section.included
        for section in compiled_context.sections
    )


def _optional_scratchpad_str(state: LoopState, key: str) -> str | None:
    value = state.scratchpad.get(key)
    return value if isinstance(value, str) else None


def _optional_scratchpad_int(state: LoopState, key: str) -> int | None:
    value = state.scratchpad.get(key)
    return value if isinstance(value, int) else None


def _is_overridden_hook(instance: object, name: str, base: type) -> bool:
    method = getattr(type(instance), name, None)
    return method is not None and method is not getattr(base, name, None)


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
    if end is not None and isinstance(
        end.metadata.get("tool_observation_timing"), dict
    ):
        timing = dict(end.metadata["tool_observation_timing"])
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


def _plan_revision_required_instruction(user_feedback: str) -> str:
    feedback = user_feedback.strip() or "(no additional feedback text was provided)"
    return (
        "Plan revision is still pending. The user requested a revision with this feedback:\n"
        f"{feedback}\n\n"
        "You must now present the revised plan by calling exit_plan. Do not provide a plain-text "
        "final answer or implementation summary. Only call ask_plan_question if a new material "
        "ambiguity genuinely blocks the revised plan."
    )


def _accepted_plan_artifact_id(run_id: str, exit_request_id: str) -> str:
    return f"artifact:plan:{_sanitize_artifact_part(run_id)}:{_sanitize_artifact_part(exit_request_id)}:accepted"


def _sanitize_artifact_part(value: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
        or "unknown"
    )
