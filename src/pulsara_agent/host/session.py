"""Long-lived conversation session wrapper around AgentRuntime."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    RunStartEvent,
)
from pulsara_agent.primitives.mcp import (
    MAX_MCP_DIAGNOSTIC_CODE_CHARS,
    MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS,
    McpDiagnosticFact,
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
)
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.message import SystemMsg
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    PendingApproval,
    pending_approval_from_state,
)
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    TerminalAccess,
    parse_permission_mode,
    preset_to_policy,
)
from pulsara_agent.runtime.permission_snapshot import validate_preset_policy_payload
from pulsara_agent.runtime.plan import (
    McpInputRequiredInteractionResolution,
    PLAN_ACTIVE_INSTRUCTION,
    PLAN_ACTIVE_INSTRUCTION_NAME,
    PLAN_ENTRY_INSTRUCTION,
    PLAN_ENTRY_INSTRUCTION_NAME,
    PendingInteraction,
    PendingMcpInputRequired,
    PendingPlanInteraction,
    PlanInteractionResolution,
    PlanWorkflowState,
    pending_plan_interaction_from_state,
    pending_mcp_input_required_from_state,
    reduce_plan_workflow_state,
)
from pulsara_agent.runtime.recovery import AbortKind, StopRequest
from pulsara_agent.runtime.session import EventPublicationAfterCommitError
from pulsara_agent.runtime.state import LoopState, LoopStatus
from pulsara_agent.runtime.mcp.store import load_mcp_server_configs
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpInstalledCapabilitySnapshot,
    McpManagerSlot,
    McpPendingInstallationAudit,
    McpReconcileTicket,
    McpServerCandidate,
    McpServerSnapshot,
    McpServerStatus,
    redact_mcp_error_message,
)
from pulsara_agent.runtime.terminal import WorkspaceTerminalLease
from pulsara_agent.runtime.wiring import AgentRuntimeWiring
from pulsara_agent.capability.providers.mcp import (
    McpCapabilityProvider,
    build_mcp_installation,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


class HostSessionBusyError(RuntimeError):
    """Raised when a HostSession already has an active run."""


class HostSessionPendingApprovalError(RuntimeError):
    """Raised when a HostSession is suspended on a pending approval."""


class HostSessionPendingInteractionError(RuntimeError):
    """Raised when a HostSession is suspended on any pending user interaction."""


class HostSessionLifecycle(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class _StreamError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_STREAM_DONE = object()
_STREAM_QUEUE_MAX_ITEMS = 128


def _replace_mcp_tool_bindings(
    current: tuple[object, ...], mcp_tools: tuple[object, ...]
) -> tuple[object, ...]:
    return (
        *(tool for tool in current if not isinstance(tool, McpCapabilityTool)),
        *mcp_tools,
    )


def _replace_mcp_capability_provider(
    current: CapabilityRuntime,
    mcp_installation: McpInstalledCapabilitySnapshot,
) -> CapabilityRuntime:
    providers = []
    inserted = False
    for provider in current.providers:
        if isinstance(provider, McpCapabilityProvider):
            if mcp_installation.snapshots and not inserted:
                providers.append(McpCapabilityProvider(mcp_installation))
                inserted = True
            continue
        providers.append(provider)
    if mcp_installation.snapshots and not inserted:
        providers.append(McpCapabilityProvider(mcp_installation))
    return CapabilityRuntime(providers=tuple(providers))


def _mcp_surface_semantic_key(
    snapshots: tuple[McpServerSnapshot, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                snapshot.server_id,
                snapshot.status.value,
                snapshot.required,
                snapshot.event_safe_config_fingerprint,
                snapshot.snapshot_semantic_fingerprint,
            )
            for snapshot in snapshots
        )
    )


def _mcp_pending_audit(
    *,
    old: McpInstalledCapabilitySnapshot,
    new: McpInstalledCapabilitySnapshot,
    candidates: tuple[McpServerCandidate, ...],
    changed_server_ids: set[str],
    fallback_trigger: str,
    stale_discard_counts: dict[str, int],
) -> McpPendingInstallationAudit:
    candidate_by_server = {
        candidate.server_snapshot.server_id: candidate for candidate in candidates
    }
    server_facts: list[McpInstalledServerSnapshotFact] = []
    triggers: set[str] = set()
    for snapshot in new.snapshots:
        candidate = candidate_by_server.get(snapshot.server_id)
        trigger = candidate.trigger if candidate is not None else fallback_trigger
        changed = snapshot.server_id in changed_server_ids
        if changed:
            triggers.add(trigger)
        status = {
            McpServerStatus.STARTING: "running",
            McpServerStatus.READY: "ready",
            McpServerStatus.DEGRADED: "degraded",
            McpServerStatus.FAILED: "failed",
            McpServerStatus.NEEDS_AUTH: "needs_auth",
            McpServerStatus.DISABLED: "disabled",
        }.get(snapshot.status, "failed")
        attempt = McpReconcileAttemptSummaryFact(
            server_id=snapshot.server_id,
            reconcile_attempt_id=snapshot.reconcile_attempt_id,
            reconcile_trigger=trigger,  # type: ignore[arg-type]
            attempt_status=status,  # type: ignore[arg-type]
            retry_attempt=candidate.retry_attempt if candidate is not None else 0,
            request_count=candidate.request_count if candidate is not None else 0,
            page_count=candidate.page_count if candidate is not None else 0,
            cache_outcome=(candidate.cache_outcome if candidate is not None else "not_applicable"),  # type: ignore[arg-type]
            stale_candidates_discarded_since_previous_install=(
                stale_discard_counts.get(snapshot.server_id, 0)
            ),
        )
        diagnostics = tuple(_mcp_diagnostic_fact(item) for item in snapshot.diagnostics[:16])
        timing = snapshot.timing
        if timing is None:
            raise ValueError("MCP installed snapshot requires lifecycle timing")
        server_facts.append(
            McpInstalledServerSnapshotFact(
                server_id=snapshot.server_id,
                status=snapshot.status.value,  # type: ignore[arg-type]
                required=snapshot.required,
                changed_in_this_installation=changed,
                attempt=attempt,
                snapshot_id=snapshot.snapshot_id,
                discovery_generation=snapshot.discovery_generation,
                event_safe_config_fingerprint=snapshot.event_safe_config_fingerprint,
                snapshot_semantic_fingerprint=snapshot.snapshot_semantic_fingerprint,
                protocol_version=snapshot.protocol_version,
                tool_count=len(snapshot.tools),
                resource_count=len(snapshot.resources),
                resource_template_count=len(snapshot.resource_templates),
                prompt_count=len(snapshot.prompts),
                instructions_chars=len(snapshot.instructions or ""),
                lifecycle_timing=timing,
                diagnostics=diagnostics,
                catalog_artifact_id=None,
            )
        )
    old_names = {str(getattr(item, "name", "")) for item in old.descriptors}
    new_names = {str(getattr(item, "name", "")) for item in new.descriptors}
    changed_names = tuple(sorted(old_names.symmetric_difference(new_names)))
    bounded_names = changed_names[:64]
    return McpPendingInstallationAudit(
        event_id=f"mcp_installation_event:{uuid4().hex}",
        installation_id=new.installation_id,
        previous_installation_id=(
            None if old.installation_id == "mcp_installation:empty" else old.installation_id
        ),
        config_epoch=new.config_epoch,
        event_safe_config_set_fingerprint=new.event_safe_config_set_fingerprint,
        installation_triggers=tuple(sorted(triggers)),  # type: ignore[arg-type]
        coalesced_installation_count=0,
        coalesced_attempt_summaries=(),
        coalesced_attempt_summaries_omitted=0,
        server_snapshots=tuple(server_facts),
        total_installed_tool_count=len(new.tools),
        added_tool_count=len(new_names.difference(old_names)),
        revoked_tool_count=len(old_names.difference(new_names)),
        changed_tool_names_bounded=bounded_names,
        changed_tool_names_omitted=max(0, len(changed_names) - len(bounded_names)),
        diagnostics=tuple(
            McpDiagnosticFact(
                severity=getattr(item, "severity", "warning"),
                code=str(
                    getattr(item, "code", "mcp_installation_diagnostic")
                )[:MAX_MCP_DIAGNOSTIC_CODE_CHARS],
                message=redact_mcp_error_message(
                    getattr(item, "message", "MCP installation diagnostic")
                )[:MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS],
            )
            for item in new.diagnostics[:16]
        ),
        baseline_tool_names=frozenset(old_names),
        current_tool_names=frozenset(new_names),
    )


def _mcp_diagnostic_fact(item: dict[str, Any]) -> McpDiagnosticFact:
    metadata: dict[str, object] = {}
    for key, value in list(item.items())[:16]:
        if key in {"severity", "code", "message"}:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            metadata[str(key)[:128]] = value
        else:
            metadata[str(key)[:128]] = redact_mcp_error_message(value)[:256]
    return McpDiagnosticFact(
        severity=str(item.get("severity") or "warning"),  # type: ignore[arg-type]
        code=str(item.get("code") or "mcp_server_diagnostic")[
            :MAX_MCP_DIAGNOSTIC_CODE_CHARS
        ],
        message=redact_mcp_error_message(
            item.get("message")
            or item.get("error_type")
            or "MCP server diagnostic"
        )[:MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS],
        metadata=metadata,
    )


def _permission_mode_rank(mode: PermissionMode | None) -> int:
    if mode is None:
        return 0
    return {
        PermissionMode.READ_ONLY: 1,
        PermissionMode.ASK_PERMISSIONS: 2,
        PermissionMode.ACCEPT_EDITS: 3,
        PermissionMode.BYPASS_PERMISSIONS: 4,
    }[mode]


class _StreamObserver:
    """Bounded, detachable observation channel for one Host-owned run."""

    __slots__ = ("attached", "queue")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX_ITEMS)
        self.attached = True

    async def emit(self, item: Any) -> None:
        if self.attached:
            await self.queue.put(item)

    def detach(self) -> None:
        # Mark detached before making space. A producer already blocked in put()
        # may enqueue one final item after the drain, but every subsequent emit
        # becomes a no-op and the queue remains bounded.
        self.attached = False
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return


@dataclass(slots=True)
class HostSession:
    host_session_id: str
    conversation_id: str
    workspace: ResolvedWorkspace
    wiring: AgentRuntimeWiring
    terminal_lease: WorkspaceTerminalLease | None = None
    mcp_supervisor: McpServerSupervisor | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    active_run_id: str | None = None
    stopping_run_id: str | None = None
    suspended_run_id: str | None = None
    pending_interaction: PendingInteraction | None = None
    plan_state: PlanWorkflowState = field(default_factory=PlanWorkflowState)
    _lifecycle: HostSessionLifecycle = field(default=HostSessionLifecycle.OPEN)
    _suspended_state: LoopState | None = None
    _active_state: LoopState | None = None
    _active_task: asyncio.Task[Any] | None = None
    _compaction_listeners: list[Callable[[AgentEvent], None]] = field(
        default_factory=list, init=False, repr=False
    )
    _run_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _stop_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _mcp_installation_faulted: bool = field(default=False, init=False, repr=False)
    _mcp_installation_diagnostics: list[dict[str, str]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        reduced = reduce_plan_workflow_state(
            self.wiring.runtime_wiring.event_log.iter()
        )
        if (
            reduced.active
            or reduced.latest_accepted_plan_summary
            or reduced.latest_accepted_plan_artifact_id
        ):
            self.plan_state = reduced

    @property
    def closed(self) -> bool:
        return self._lifecycle is HostSessionLifecycle.CLOSED

    @property
    def lifecycle(self) -> HostSessionLifecycle:
        return self._lifecycle

    def begin_close(self) -> None:
        """Synchronously close the mutation gate before async teardown starts."""
        if self._lifecycle is HostSessionLifecycle.OPEN:
            self._lifecycle = HostSessionLifecycle.CLOSING

    @property
    def runtime_session_id(self) -> str:
        return self.wiring.runtime_wiring.runtime_session.runtime_session_id

    @property
    def has_live_processes(self) -> bool:
        return self.wiring.runtime_wiring.runtime_session.terminal_sessions.has_live_processes(
            owner_host_session_id=self.host_session_id
        )

    @property
    def terminal_summary(self) -> dict[str, object]:
        terminal_sessions = self.wiring.runtime_wiring.runtime_session.terminal_sessions
        processes = terminal_sessions.list_processes(
            owner_host_session_id=self.host_session_id
        )
        return {
            "has_live_processes": terminal_sessions.has_live_processes(
                owner_host_session_id=self.host_session_id
            ),
            "live_process_count": terminal_sessions.live_process_count(
                owner_host_session_id=self.host_session_id
            ),
            "finished_process_count": terminal_sessions.finished_process_count(
                owner_host_session_id=self.host_session_id
            ),
            "pending_completion_count": terminal_sessions.pending_completion_count(
                owner_host_session_id=self.host_session_id
            ),
            "processes": [process.to_payload() for process in processes],
        }

    async def _apply_mcp_safe_point(
        self,
        *,
        trigger: str,
        prepared_ticket: McpReconcileTicket | None = None,
    ) -> None:
        """Apply background MCP candidates at a Host-owned safe point."""

        supervisor = self.mcp_supervisor
        if supervisor is None:
            return
        if self._mcp_installation_faulted:
            raise RuntimeError(
                "MCP installation commit faulted; this HostSession must be closed and reopened"
            )
        configs = load_mcp_server_configs(workspace_root=self.workspace.workspace_root)
        ticket = prepared_ticket or supervisor.prepare(configs, trigger=trigger)
        try:
            await supervisor.await_required(ticket)
        except Exception as exc:
            reconfigured_server_id = self._pending_mcp_reconfigured_server_id(
                supervisor
            )
            if reconfigured_server_id is None:
                raise
            supervisor.terminalize_attempt_for_pending_reconfiguration(
                ticket,
                exc,
                server_id=reconfigured_server_id,
            )
        else:
            reconfigured_server_id = self._pending_mcp_reconfigured_server_id(
                supervisor
            )
            if reconfigured_server_id is not None:
                supervisor.terminalize_attempt_for_pending_reconfiguration(
                    ticket,
                    RuntimeError(
                        "pending MCP binding was reconfigured before its replacement "
                        "generation became installable"
                    ),
                    server_id=reconfigured_server_id,
                )
        batch = supervisor.drain_installable_candidates(
            expected_epoch=ticket.config_epoch
        )
        starting = supervisor.current_starting_snapshots()
        if not batch.candidates and not starting:
            return

        old_installation = self.wiring.runtime_wiring.mcp_installation
        configs_by_server = {config.server_id: config for config in configs}
        snapshots_by_server = {
            snapshot.server_id: snapshot for snapshot in old_installation.snapshots
        }
        slots_by_server: dict[str, McpManagerSlot] = {}
        retiring_slot_ids: list[str] = []
        for server_id in snapshots_by_server:
            slot = supervisor.installed_slot(server_id)
            if slot is not None:
                if slot.lifecycle == "installed":
                    slots_by_server[server_id] = slot
                elif slot.lifecycle == "retiring":
                    retiring_slot_ids.append(slot.slot_id)

        latest_candidates: dict[str, McpServerCandidate] = {}
        for candidate in batch.candidates:
            latest_candidates[candidate.server_snapshot.server_id] = candidate
        changed_server_ids: set[str] = set()
        for server_id, candidate in latest_candidates.items():
            changed_server_ids.add(server_id)
            old_slot = slots_by_server.pop(server_id, None)
            if old_slot is not None:
                retiring_slot_ids.append(old_slot.slot_id)
            if server_id not in configs_by_server:
                snapshots_by_server[server_id] = candidate.server_snapshot
                continue
            snapshots_by_server[server_id] = candidate.server_snapshot
            if candidate.manager_slot is not None:
                slots_by_server[server_id] = candidate.manager_slot
        for snapshot in starting:
            current_slot = supervisor.installed_slot(snapshot.server_id)
            if (
                snapshot.server_id not in snapshots_by_server
                or current_slot is None
                or current_slot.lifecycle == "retiring"
                or not supervisor.binding_matches_current_desired_runtime(
                    current_slot.binding_identity
                )
            ):
                old_slot = slots_by_server.pop(snapshot.server_id, None)
                if old_slot is not None:
                    retiring_slot_ids.append(old_slot.slot_id)
                snapshots_by_server[snapshot.server_id] = snapshot
                changed_server_ids.add(snapshot.server_id)

        new_snapshots = tuple(
            snapshots_by_server[server_id]
            for server_id in sorted(snapshots_by_server)
        )
        semantic_changed = _mcp_surface_semantic_key(old_installation.snapshots) != (
            _mcp_surface_semantic_key(new_snapshots)
        )
        old_slot_identities = frozenset(old_installation.binding_identities)
        new_slot_identities = frozenset(
            slot.binding_identity for slot in slots_by_server.values()
        )
        slot_changed = old_slot_identities != new_slot_identities
        if not semantic_changed and not slot_changed:
            supervisor.reject_candidates(tuple(latest_candidates.values()))
            return

        installation_id = f"mcp_installation:{uuid4().hex}"
        stale_discard_counts = supervisor.stale_discard_counts()
        try:
            new_installation = build_mcp_installation(
                supervisor=supervisor,
                config_epoch=ticket.config_epoch,
                event_safe_config_set_fingerprint=ticket.event_safe_config_set_fingerprint,
                snapshots=new_snapshots,
                configs_by_server=configs_by_server,
                slots_by_server=slots_by_server,
                installation_id=installation_id,
                previous_installation=old_installation,
            )
            new_capability_runtime = _replace_mcp_capability_provider(
                self.wiring.agent_runtime.capability_runtime,
                new_installation,
            )
            runtime_session = self.wiring.runtime_wiring.runtime_session
            new_extra_bindings = _replace_mcp_tool_bindings(
                runtime_session.extra_tool_bindings,
                tuple(new_installation.tools),
            )
            pending_audit = _mcp_pending_audit(
                old=old_installation,
                new=new_installation,
                candidates=tuple(latest_candidates.values()),
                changed_server_ids=changed_server_ids,
                fallback_trigger=trigger,
                stale_discard_counts=stale_discard_counts,
            )
        except Exception as exc:
            supervisor.reject_candidates(tuple(latest_candidates.values()))
            has_required_change = any(
                candidate.runtime_spec.config.required
                for candidate in latest_candidates.values()
            ) or any(
                snapshot.required
                for snapshot in starting
                if snapshot.server_id in changed_server_ids
            )
            if not has_required_change:
                supervisor.restore_retiring_slots(tuple(retiring_slot_ids))
                self._mcp_installation_diagnostics.append(
                    {
                        "code": "mcp_optional_installation_rejected",
                        "error_type": type(exc).__name__,
                        "message": redact_mcp_error_message(exc),
                    }
                )
                del self._mcp_installation_diagnostics[:-16]
                return
            raise
        except BaseException:
            supervisor.reject_candidates(tuple(latest_candidates.values()))
            raise
        try:
            supervisor.commit_slot_transition(
                candidates=tuple(latest_candidates.values()),
                retiring_slot_ids=tuple(retiring_slot_ids),
            )
            runtime_session.extra_tool_bindings = new_extra_bindings
            self.wiring = replace(
                self.wiring,
                runtime_wiring=replace(
                    self.wiring.runtime_wiring,
                    mcp_installation=new_installation,
                ),
            )
            self.wiring.agent_runtime.refresh_capability_runtime(
                new_capability_runtime
            )
            runtime_session.set_mcp_installation_contract(
                installation_id=new_installation.installation_id,
                pending_audit=pending_audit,
            )
            supervisor.acknowledge_stale_discard_counts(stale_discard_counts)
        except BaseException:
            self._mcp_installation_faulted = True
            raise

        runtime_session = self.wiring.runtime_wiring.runtime_session
        if self._suspended_state is not None:
            self._suspended_state.scratchpad.pop("capability_exposure", None)
        if self._active_state is not None:
            self._active_state.scratchpad.pop("capability_exposure", None)
        revoked_servers = sorted(
            old_installation.ready_server_ids.difference(
                new_installation.ready_server_ids
            )
        )
        subagent_runtime = getattr(runtime_session, "subagent_runtime", None)
        if (revoked_servers or retiring_slot_ids) and subagent_runtime is not None:
            retiring_identity_set = frozenset(
                identity
                for identity in old_installation.binding_identities
                if identity.slot_id in set(retiring_slot_ids)
            )
            await subagent_runtime.fail_children_for_mcp_binding_change(
                retiring_identity_set,
                reason_message=(
                    "Parent MCP installation changed a child-visible binding generation."
                ),
            )
        await supervisor.close_retiring_slots(
            timeout_seconds=5.0,
            wait_for_borrowers=False,
        )

    def _pending_mcp_reconfigured_server_id(
        self,
        supervisor: McpServerSupervisor,
    ) -> str | None:
        state = self._suspended_state
        if state is None or state.pending_interaction_kind != "mcp_input_required":
            return None
        identity = state.pending_interaction_payload.get("mcp_binding_identity")
        if not isinstance(identity, dict):
            return None
        try:
            binding_identity = McpBindingIdentity(
                server_id=str(identity["server_id"]),
                slot_id=str(identity["slot_id"]),
                snapshot_id=str(identity["snapshot_id"]),
                discovery_generation=int(identity["discovery_generation"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if supervisor.binding_matches_current_desired_runtime(binding_identity):
            return None
        return binding_identity.server_id

    # -- Run entry points -----------------------------------------------------
    # run_turn / stream_turn / approval resume / plan resume all flow through one
    # internal execution handle (_run_owned / _stream_owned). The HostSession owns
    # the task and cancel scope; whether the caller consumes a result or an event
    # stream is only an observation difference and never changes stop/drain/close
    # semantics (contract §6.1).

    async def initialize_mcp(self, ticket: McpReconcileTicket) -> None:
        async with self._run_lock:
            await self._apply_mcp_safe_point(
                trigger="initial",
                prepared_ticket=ticket,
            )

    async def run_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AgentRunResult:
        self._raise_if_not_open("starting a new turn")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("starting a new turn")
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            run_model_target = self.wiring.agent_runtime.resolve_run_model_target()
            prior_messages = await self._prepare_prior_messages_for_turn(
                user_input,
                target_model_target=run_model_target,
            )
            prior_messages.extend(self._plan_runtime_messages())
            state = self._begin_active_state()
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.run_task(
                    user_input,
                    run_model_target=run_model_target,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                ),
            )

    async def stream_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("starting a new turn")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("starting a new turn")
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            run_model_target = self.wiring.agent_runtime.resolve_run_model_target()
            prior_messages = await self._prepare_prior_messages_for_turn(
                user_input,
                target_model_target=run_model_target,
            )
            prior_messages.extend(self._plan_runtime_messages())
            state = self._begin_active_state()
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_task(
                    user_input,
                    run_model_target=run_model_target,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                ),
            ):
                yield event

    def get_pending_approval(self) -> PendingApproval | None:
        return (
            self.pending_interaction
            if isinstance(self.pending_interaction, PendingApproval)
            else None
        )

    def get_pending_interaction(self) -> PendingInteraction | None:
        return self.pending_interaction

    @property
    def default_permission_mode(self) -> PermissionMode | None:
        return self.wiring.agent_runtime.permission_mode

    def default_permission_policy(self) -> EffectivePermissionPolicy:
        return self.wiring.agent_runtime.permission_policy

    @property
    def effective_next_run_permission_mode(self) -> PermissionMode | None:
        if self.plan_state.active:
            return PermissionMode.READ_ONLY
        return self.default_permission_mode

    def effective_next_run_permission_policy(self) -> EffectivePermissionPolicy:
        if self.plan_state.active:
            return preset_to_policy(PermissionMode.READ_ONLY)
        return self.default_permission_policy()

    @property
    def current_permission_mode(self) -> PermissionMode | None:
        return self.effective_next_run_permission_mode

    def current_permission_policy(self) -> EffectivePermissionPolicy:
        return self.effective_next_run_permission_policy()

    def set_permission_mode(
        self, mode: str | PermissionMode
    ) -> EffectivePermissionPolicy:
        """Switch the conversation's permission mode at a turn boundary.

        Only the user/host may call this; the agent has no self-switch tool.
        Rejected while a run is active, a turn is stopping, or an approval is
        pending, so the switch never corrupts an in-flight turn. Takes effect
        on the next turn (gate) / next execution (terminal tools)."""
        self._raise_if_not_open("switching mode")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("switching mode")
        self._raise_if_active_run()
        if self.plan_state.active:
            raise ValueError(
                "cannot switch permission mode while plan workflow is active; "
                "approve, cancel, or force-exit the plan first"
            )
        parsed = parse_permission_mode(mode)
        policy = preset_to_policy(parsed)
        self.wiring.agent_runtime.set_permission_policy(policy, mode=parsed)
        self.last_active_at = time.monotonic()
        return policy

    async def resolve_approval(self, resolution: ApprovalResolution) -> AgentRunResult:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            suspended_state = self._require_suspended_state(pending)
            await self._commit_pending_mcp_audits_for_resume(suspended_state)
            state = self._resume_active_state(pending)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_approval(
                    state, resolution
                ),
            )

    async def stream_approval_resolution(
        self,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            suspended_state = self._require_suspended_state(pending)
            audit_events = await self._commit_pending_mcp_audits_for_resume(
                suspended_state
            )
            state = self._resume_active_state(pending)
            for event in audit_events:
                yield event
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_after_approval(
                    state, resolution
                ),
            ):
                yield event

    async def resolve_plan_interaction(
        self,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            suspended_state = self._require_suspended_state(pending)
            await self._commit_pending_mcp_audits_for_resume(suspended_state)
            state = self._resume_active_state(pending)
            self._prepare_state_for_plan(state)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_plan_interaction(
                    state, resolution
                ),
            )

    async def resolve_mcp_input_required(
        self,
        resolution: McpInputRequiredInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving MCP input-required")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_mcp_input_required(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            suspended_state = self._require_suspended_state(pending)
            await self._commit_pending_mcp_audits_for_resume(suspended_state)
            state = self._resume_active_state(pending)
            try:
                return await self._run_owned(
                    state,
                    lambda: self.wiring.agent_runtime.resume_after_mcp_input_required(
                        state, resolution
                    ),
                )
            except EventPublicationAfterCommitError:
                self._capture_pending_interaction(state)
                raise

    async def exit_plan_workflow(
        self,
        *,
        source: str,
        user_feedback: str = "",
    ) -> None:
        self._raise_if_not_open("exiting plan")
        if source not in {"user_cancel", "user_force_exit"}:
            raise ValueError("plan exit source must be user_cancel or user_force_exit")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        if not self.plan_state.active:
            raise ValueError("plan workflow is not active")
        self._raise_if_active_run()
        async with self._run_lock:
            pending = self.pending_interaction
            state = self._suspended_state
            if pending is not None:
                if not isinstance(pending, PendingPlanInteraction):
                    raise HostSessionPendingInteractionError(
                        "host session has a non-plan pending interaction; resolve or stop it before exiting plan"
                    )
                if pending.kind != "exit" and source != "user_force_exit":
                    raise HostSessionPendingInteractionError(
                        "host session has a pending plan question; answer it or use force-exit before cancelling plan"
                    )
                state = self._resume_active_state(pending)
                if pending.kind == "exit":
                    await self.wiring.runtime_wiring.runtime_session.emit(
                        PlanExitResolvedEvent(
                            run_id=state.run_id,
                            turn_id=state.turn_id,
                            reply_id=state.reply_id,
                            exit_request_id=pending.exit_request_id or "",
                            tool_call_id=pending.tool_call_id,
                            decision="cancel",
                            user_feedback=user_feedback,
                        ),
                        state=state,
                    )
            else:
                state = self.wiring.agent_runtime.new_state()
                self._prepare_state_for_plan(state)
                self.active_run_id = state.run_id
                self._active_state = state
            await self._emit_plan_mode_exited(
                state,
                source=source,
                exit_request_id=pending.exit_request_id
                if isinstance(pending, PendingPlanInteraction)
                else None,
            )
            if pending is not None:
                await self.wiring.agent_runtime.abort_run(
                    state, reason=AbortKind.USER_STOP
                )
                self._suspended_state = None
                self.suspended_run_id = None
                self.pending_interaction = None
            self._finish_active_run()

    async def stream_plan_interaction_resolution(
        self,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._apply_mcp_safe_point(trigger="config_change")
            suspended_state = self._require_suspended_state(pending)
            audit_events = await self._commit_pending_mcp_audits_for_resume(
                suspended_state
            )
            state = self._resume_active_state(pending)
            self._prepare_state_for_plan(state)
            for event in audit_events:
                yield event
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_after_plan_interaction(
                    state, resolution
                ),
            ):
                yield event

    async def stop_current_turn(
        self,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
        timeout: float = 2.0,
    ) -> AgentRunResult | None:
        self._raise_if_not_open("stopping the current turn")
        async with self._stop_lock:
            task = self._active_task
            state = self._active_state
            if self.pending_interaction is not None and (task is None or task.done()):
                if self._run_lock.locked():
                    raise HostSessionBusyError("host session already has an active run")
                async with self._run_lock:
                    pending = self.pending_interaction
                    if pending is None:
                        return None
                    state = self._require_suspended_state(pending)
                    self.active_run_id = state.run_id
                    self.stopping_run_id = state.run_id
                    self.last_active_at = time.monotonic()
                    try:
                        result = await self.wiring.agent_runtime.abort_run(
                            state, reason=reason
                        )
                        self._capture_pending_interaction(result.state)
                        return result
                    finally:
                        self.active_run_id = None
                        self.stopping_run_id = None
                        self.last_active_at = time.monotonic()

            if task is None or state is None:
                return None
            if task.done():
                return None
            self.stopping_run_id = state.run_id
            state.stop_request = StopRequest(reason=reason)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                return None
            except Exception:
                pass
            # The owned task (streaming drive or run coroutine) finalizes itself
            # on a stop_request; abort_run is idempotent and yields the result.
            return await self.wiring.agent_runtime.abort_run(state, reason=reason)

    def replay_events(self, *, after_sequence: int | None = None) -> list[AgentEvent]:
        return self.wiring.runtime_wiring.event_log.iter(after_sequence=after_sequence)

    def add_compaction_listener(self, listener: Callable[[AgentEvent], None]) -> None:
        """Register a best-effort observer for terminal context compaction events."""

        self._compaction_listeners.append(listener)

    async def compact_now(self) -> dict[str, object]:
        """Manually compact this idle session before the auto threshold is reached."""

        self._raise_if_not_open("compacting context")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("compacting context")
        self._raise_if_active_run()
        service = self.wiring.runtime_wiring.compaction_service
        if service is None:
            raise RuntimeError("context compaction is not configured for this session")
        async with self._run_lock:
            event_log = self.wiring.runtime_wiring.event_log
            before_sequence = await asyncio.to_thread(event_log.next_sequence)
            try:
                target = self.wiring.agent_runtime.resolve_run_model_target()
                event = await service.compact(
                    target_model_target=target,
                    trigger="manual",
                    reason="user_requested",
                    force=True,
                )
            finally:
                await self._publish_compaction_events_after(before_sequence)
            return {
                "compacted": event is not None,
                "compaction_id": event.compaction_id if event is not None else None,
                "summary_artifact_id": event.summary_artifact_id
                if event is not None
                else None,
                "window_id": event.window_id if event is not None else None,
                "through_sequence": event.through_sequence
                if event is not None
                else None,
                "keep_after_sequence": event.keep_after_sequence
                if event is not None
                else None,
            }

    # -- Close / teardown -----------------------------------------------------

    def close(self) -> None:
        """Synchronous runtime-local close. Idempotent.

        Does NOT release the shared workspace terminal lease (HostCore/supervisor
        owns that) and does NOT delete the workspace root (HostCore does that
        last, after lease release) — see contract §6.2/§7.1.
        """
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return
        self._lifecycle = HostSessionLifecycle.CLOSED
        self.pending_interaction = None
        self._suspended_state = None
        self._active_state = None
        self._active_task = None
        self.stopping_run_id = None
        self.suspended_run_id = None
        self.wiring.agent_runtime.close()

    async def aclose(
        self,
        *,
        reason: AbortKind = AbortKind.HOST_TEARDOWN,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        """Bounded, idempotent run-control close.

        Both the active run and any suspended (pending-interaction) run get a
        typed, auditable terminal RunEnd under ``reason`` (default host-teardown,
        never masqueraded as USER_STOP) instead of being silently dropped
        (contract §6.2, decision 1)."""
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return
        self.begin_close()
        await self.drain_active_run(
            reason=reason, timeout_seconds=drain_timeout_seconds
        )
        await self._finalize_suspended_run(reason)
        subagent_runtime = getattr(
            self.wiring.runtime_wiring.runtime_session, "subagent_runtime", None
        )
        if subagent_runtime is not None:
            await subagent_runtime.cancel_active_children(
                reason_code="subagent_host_session_close",
                reason_message="HostSession is closing; active child runtimes are cancelled.",
                cancelled_by="host_shutdown",
                timeout_seconds=drain_timeout_seconds,
            )
        if self.mcp_supervisor is not None:
            await self.mcp_supervisor.aclose(
                timeout_seconds=drain_timeout_seconds
            )
        self.close()

    async def drain_active_run(
        self,
        *,
        reason: AbortKind | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Stop an in-flight run via the owned task handle.

        When ``reason`` is given the run is finalized with an auditable terminal
        outcome (a stop_request the owned task converts into a RunEnd); without a
        reason it is a best-effort cancel only.
        """
        task = self._active_task
        state = self._active_state
        if task is None or task.done() or task is asyncio.current_task():
            return
        if reason is not None and state is not None and state.stop_request is None:
            state.stop_request = StopRequest(reason=reason)
            self.stopping_run_id = state.run_id
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception:
            pass

    async def _finalize_suspended_run(self, reason: AbortKind) -> None:
        state = self._suspended_state
        if state is None:
            return
        interaction_id = (
            str(state.pending_interaction_payload.get("interaction_id"))
            if state.pending_interaction_kind == "mcp_input_required"
            and state.pending_interaction_payload.get("interaction_id") is not None
            else None
        )
        await self.wiring.agent_runtime.abort_run(state, reason=reason)
        if interaction_id is not None and self.mcp_supervisor is not None:
            self.mcp_supervisor.complete_pending_lease(interaction_id)
        self._suspended_state = None
        self.pending_interaction = None
        self.suspended_run_id = None

    def summary(self) -> dict[str, object]:
        installation = self.wiring.runtime_wiring.mcp_installation
        starting = (
            self.mcp_supervisor.current_starting_snapshots()
            if self.mcp_supervisor is not None
            else ()
        )
        return {
            "host_session_id": self.host_session_id,
            "conversation_id": self.conversation_id,
            "runtime_session_id": self.runtime_session_id,
            "workspace_kind": self.workspace.workspace_kind,
            "workspace_root": str(self.workspace.workspace_root),
            "display_label": self.workspace.display_label,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "lifecycle": self._lifecycle.value,
            "closed": self.closed,
            "active_run_id": self.active_run_id,
            "stopping_run_id": self.stopping_run_id,
            "is_stopping": self.stopping_run_id is not None,
            "suspended_run_id": self.suspended_run_id,
            "pending_approval": self.get_pending_approval().to_dict()
            if self.get_pending_approval() is not None
            else None,
            "pending_interaction": self.pending_interaction.to_dict()
            if self.pending_interaction is not None
            else None,
            "plan": self.plan_state.to_dict(),
            "has_live_processes": self.has_live_processes,
            "terminal": self.terminal_summary,
            "mcp": {
                "installation_id": installation.installation_id,
                "config_epoch": installation.config_epoch,
                "faulted": self._mcp_installation_faulted,
                "diagnostics": list(self._mcp_installation_diagnostics),
                "servers": [
                    {
                        "server_id": snapshot.server_id,
                        "status": snapshot.status.value,
                        "required": snapshot.required,
                        "snapshot_id": snapshot.snapshot_id,
                        "discovery_generation": snapshot.discovery_generation,
                        "tool_count": len(snapshot.tools),
                    }
                    for snapshot in (
                        *installation.snapshots,
                        *(
                            item
                            for item in starting
                            if item.server_id
                            not in {value.server_id for value in installation.snapshots}
                        ),
                    )
                ],
            },
        }

    # -- Internal execution primitive -----------------------------------------

    def _begin_active_state(self) -> LoopState:
        state = self.wiring.agent_runtime.new_state()
        self._prepare_state_for_plan(state)
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    def _resume_active_state(self, pending: PendingInteraction) -> LoopState:
        state = self._require_suspended_state(pending)
        starts = [
            event
            for event in self.wiring.runtime_wiring.event_log.iter()
            if isinstance(event, RunStartEvent) and event.run_id == state.run_id
        ]
        if len(starts) != 1:
            raise RuntimeError(
                "suspended run requires exactly one durable RunStart model target contract"
            )
        state.run_model_target = self.wiring.agent_runtime.rebind_run_model_target(
            starts[0].model_target
        )
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    async def _commit_pending_mcp_audits_for_resume(
        self,
        state: LoopState,
    ) -> tuple[AgentEvent, ...]:
        """Durably publish safe-point installation facts before run continuation.

        A resume has no second RunStart carrier.  The original suspended state
        remains authoritative until this batch is acknowledged, so an EventLog
        failure leaves the pending interaction and its MCP lease retryable.
        """

        runtime_session = self.wiring.runtime_wiring.runtime_session
        context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        pending = runtime_session.pending_mcp_installation_audit_events(context)
        if not pending:
            return ()
        try:
            stored = await runtime_session.emit_many(pending, state=state)
        except EventPublicationAfterCommitError as exc:
            runtime_session.acknowledge_committed_mcp_installation_audits(
                exc.result.committed_events
            )
            raise
        runtime_session.acknowledge_committed_mcp_installation_audits(stored)
        return tuple(stored)

    async def _run_owned(
        self,
        state: LoopState,
        make_result: Callable[[], Awaitable[AgentRunResult]],
    ) -> AgentRunResult:
        async def _drive() -> AgentRunResult:
            try:
                try:
                    result = await make_result()
                except asyncio.CancelledError:
                    request = state.stop_request
                    if request is None:
                        raise
                    result = await self.wiring.agent_runtime.abort_run(
                        state, reason=request.reason
                    )
                self._capture_pending_interaction(result.state)
                self._clear_plan_entry_audit_if_emitted(result.state)
                return result
            finally:
                self._finish_active_run()

        # The owned handle covers the complete execution lifecycle, including
        # cancellation -> abort/RunEnd conversion and host bookkeeping. A closer
        # that drains this task therefore cannot release runtime/terminal resources
        # while the outer wrapper is still finalizing the run.
        task: asyncio.Task[AgentRunResult] = asyncio.create_task(_drive())
        self._active_task = task
        return await task

    async def _stream_owned(
        self,
        state: LoopState,
        make_stream: Callable[[], AsyncIterator[AgentEvent]],
    ) -> AsyncIterator[AgentEvent]:
        observer = _StreamObserver()

        async def _drive() -> None:
            try:
                async for event in make_stream():
                    await observer.emit(event)
            except asyncio.CancelledError:
                request = state.stop_request
                if request is not None:
                    async for event in self.wiring.agent_runtime.stream_abort_run(
                        state, reason=request.reason
                    ):
                        await observer.emit(event)
                    await observer.emit(_STREAM_DONE)
                    return
                raise
            except (
                Exception
            ) as exc:  # surface to consumer, mirror prior direct-iteration semantics
                await observer.emit(_StreamError(exc))
                await observer.emit(_STREAM_DONE)
                return
            else:
                await observer.emit(_STREAM_DONE)
            finally:
                self._capture_pending_interaction(state)
                self._clear_plan_entry_audit_if_emitted(state)
                self._finish_active_run()

        task = asyncio.create_task(_drive())
        self._active_task = task
        stream_error: BaseException | None = None
        try:
            while True:
                item = await observer.queue.get()
                if item is _STREAM_DONE:
                    await asyncio.shield(task)
                    if stream_error is not None:
                        raise stream_error
                    break
                if isinstance(item, _StreamError):
                    stream_error = item.exc
                    continue
                yield item
        finally:
            # Closing or abandoning the transport-facing generator only detaches
            # this observer. HostSession remains the execution owner; explicit
            # stop/session close/HostCore shutdown are the cancellation paths.
            observer.detach()

    def _finish_active_run(self) -> None:
        self._notify_governance()
        self._active_task = None
        self._active_state = None
        self.active_run_id = None
        self.stopping_run_id = None
        self.last_active_at = time.monotonic()

    async def _prepare_prior_messages_for_turn(
        self,
        user_input: str,
        *,
        target_model_target,
    ):
        prior_messages = self._prior_messages()
        service = self.wiring.runtime_wiring.compaction_service
        if service is not None:
            compacted = await self._compact_if_needed_and_notify(
                service,
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=user_input,
                model_visible_messages_before=prior_messages,
                reason="preflight_context_threshold",
            )
            if compacted:
                prior_messages = self._prior_messages()
        return prior_messages

    def _prior_messages(self):
        runtime_wiring = self.wiring.runtime_wiring
        return rebuild_prior_messages(
            runtime_wiring.event_log,
            archive=runtime_wiring.archive,
            session_id=runtime_wiring.runtime_session.runtime_session_id,
        )

    async def _compact_if_needed_and_notify(self, service, **kwargs) -> bool:
        event_log = self.wiring.runtime_wiring.event_log
        before_sequence = await asyncio.to_thread(event_log.next_sequence)
        compacted = await service.compact_if_needed(**kwargs)
        compaction_events = await self._publish_compaction_events_after(before_sequence)
        terminal_event = self._latest_terminal_compaction_event(compaction_events)
        if terminal_event is not None:
            self._notify_compaction_listeners(terminal_event)
        return compacted

    async def _publish_compaction_events_after(
        self, before_sequence: int
    ) -> list[AgentEvent]:
        compaction_events = await asyncio.to_thread(
            self._compaction_events_after, before_sequence - 1
        )
        self.wiring.runtime_wiring.runtime_session.publish_stored_events(
            compaction_events
        )
        return compaction_events

    def _compaction_events_after(self, after_sequence: int) -> list[AgentEvent]:
        return [
            event
            for event in self.wiring.runtime_wiring.event_log.iter(
                after_sequence=after_sequence
            )
            if isinstance(
                event,
                (
                    ContextCompactionStartedEvent,
                    ContextCompactionCompletedEvent,
                    ContextCompactionMemoryCandidatesProposedEvent,
                    ContextCompactionFailedEvent,
                ),
            )
        ]

    def _latest_terminal_compaction_event(
        self, events: list[AgentEvent]
    ) -> AgentEvent | None:
        terminal_events = [
            event
            for event in events
            if isinstance(
                event, (ContextCompactionCompletedEvent, ContextCompactionFailedEvent)
            )
        ]
        return terminal_events[-1] if terminal_events else None

    def _notify_compaction_listeners(self, event: AgentEvent) -> None:
        for listener in list(self._compaction_listeners):
            try:
                listener(event)
            except Exception:
                continue

    def _capture_pending_interaction(self, state: LoopState) -> None:
        if state.status is LoopStatus.WAITING_USER:
            if state.pending_interaction_kind == "plan":
                self.pending_interaction = pending_plan_interaction_from_state(
                    state, self.host_session_id
                )
            elif state.pending_interaction_kind == "mcp_input_required":
                self.pending_interaction = pending_mcp_input_required_from_state(
                    state, self.host_session_id
                )
            else:
                self.pending_interaction = pending_approval_from_state(
                    state, self.host_session_id
                )
            self._suspended_state = state
            self.suspended_run_id = state.run_id
            return
        self.pending_interaction = None
        self._suspended_state = None
        self.suspended_run_id = None

    def _notify_governance(self) -> None:
        coordinator = self.wiring.runtime_wiring.governance_coordinator
        engine = self.wiring.runtime_wiring.memory_governance_engine
        if coordinator is not None and engine is not None:
            coordinator.notify(engine)

    def _require_pending_approval(self, approval_id: str) -> PendingApproval:
        pending = self.get_pending_approval()
        if pending is None:
            raise ValueError("host session has no pending approval")
        if pending.approval_id != approval_id:
            raise ValueError("approval id does not match the pending approval")
        return pending

    def _require_pending_plan_interaction(
        self, interaction_id: str
    ) -> PendingPlanInteraction:
        pending = self.pending_interaction
        if not isinstance(pending, PendingPlanInteraction):
            raise ValueError("host session has no pending plan interaction")
        if pending.interaction_id != interaction_id:
            raise ValueError(
                "plan interaction id does not match the pending interaction"
            )
        return pending

    def _require_pending_mcp_input_required(
        self, interaction_id: str
    ) -> PendingMcpInputRequired:
        pending = self.pending_interaction
        if not isinstance(pending, PendingMcpInputRequired):
            raise ValueError(
                "host session has no pending MCP input-required interaction"
            )
        if pending.interaction_id != interaction_id:
            raise ValueError(
                "MCP input-required id does not match the pending interaction"
            )
        return pending

    def _require_suspended_state(self, pending: PendingInteraction) -> LoopState:
        if self._suspended_state is None:
            raise ValueError("host session has no suspended state")
        if self._suspended_state.run_id != pending.run_id:
            raise ValueError("suspended state does not match pending interaction")
        return self._suspended_state

    def enter_plan(self, *, reason: str = "") -> EffectivePermissionPolicy:
        """Host/user entry point for Plan mode.

        This is the :plan / Plan button path: no control run is created, but
        permission is synchronously narrowed before the next model turn.
        """
        self._raise_if_not_open("entering plan")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("entering plan")
        self._raise_if_active_run()
        if not self.plan_state.active:
            self.plan_state.begin(
                source="user",
                previous_mode=self.default_permission_mode,
                previous_policy=self.default_permission_policy(),
                reason=reason,
                pending_entry_audit=False,
            )
            self._emit_user_plan_mode_entered(reason=reason)
        policy = preset_to_policy(PermissionMode.READ_ONLY)
        self.last_active_at = time.monotonic()
        return policy

    def _emit_user_plan_mode_entered(self, *, reason: str = "") -> AgentEvent:
        suffix = uuid4().hex
        return self.wiring.runtime_wiring.runtime_session.emit_from_thread(
            PlanModeEnteredEvent(
                run_id=f"run:host-plan-entry:{suffix}",
                turn_id=f"turn:host-plan-entry:{suffix}",
                reply_id=f"reply:host-plan-entry:{suffix}",
                source="user",
                previous_permission_mode=self.plan_state.pre_plan_permission_mode,
                previous_permission_policy=self.plan_state.pre_plan_permission_policy
                or {},
                reason=reason,
            )
        )

    def _plan_runtime_messages(self):
        if not self.plan_state.active:
            return []
        if self.plan_state.pending_entry_audit:
            return [
                SystemMsg(
                    PLAN_ENTRY_INSTRUCTION_NAME,
                    PLAN_ENTRY_INSTRUCTION,
                    metadata={"runtime_instruction": "plan_entry"},
                )
            ]
        return [
            SystemMsg(
                PLAN_ACTIVE_INSTRUCTION_NAME,
                PLAN_ACTIVE_INSTRUCTION,
                metadata={"runtime_instruction": "plan_active"},
            )
        ]

    def _prepare_state_for_plan(self, state: LoopState) -> None:
        state.scratchpad["host_session_id"] = self.host_session_id
        state.scratchpad["plan_state"] = self.plan_state
        if self.plan_state.active:
            state.scratchpad["plan_active"] = True
        if self.plan_state.pending_entry_audit:
            state.scratchpad["plan_entry_audit"] = {
                "source": "user",
                "previous_permission_mode": self.plan_state.pre_plan_permission_mode,
                "previous_permission_policy": self.plan_state.pre_plan_permission_policy
                or {},
                "reason": self.plan_state.entry_reason,
            }

    def _clear_plan_entry_audit_if_emitted(self, state: LoopState) -> None:
        if state.scratchpad.get("plan_entry_audit_emitted"):
            self.plan_state.pending_entry_audit = False

    def _pre_plan_policy(self) -> EffectivePermissionPolicy:
        payload = self.plan_state.pre_plan_permission_policy or {}
        if not payload or self.plan_state.pre_plan_permission_mode is None:
            raise ValueError(
                "plan workflow is missing preset previous permission facts"
            )
        validate_preset_policy_payload(
            self.plan_state.pre_plan_permission_mode,
            dict(payload),
            context="HostSession.plan_state",
        )
        return EffectivePermissionPolicy(
            profile=PermissionProfile(str(payload["profile"])),
            approval=ApprovalPolicy(str(payload["approval_policy"])),
            terminal=TerminalAccess(str(payload["terminal_access"])),
            execution_boundary="host",
            network_isolated=bool(payload.get("network_isolated", False)),
        )

    async def _emit_plan_mode_exited(
        self,
        state: LoopState,
        *,
        source: str,
        exit_request_id: str | None = None,
    ) -> None:
        restored_mode = self.plan_state.pre_plan_permission_mode
        restored_policy = self._pre_plan_policy()
        restored_mode_value = parse_permission_mode(restored_mode).value
        await self.wiring.runtime_wiring.runtime_session.emit(
            PlanModeExitedEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                source=source,  # type: ignore[arg-type]
                exit_request_id=exit_request_id,
                restored_permission_mode=restored_mode_value,
                restored_permission_policy=restored_policy.to_dict(),
            ),
            state=state,
        )
        self.plan_state.finish()

    def _raise_if_not_open(self, action: str) -> None:
        if self._lifecycle is not HostSessionLifecycle.OPEN:
            raise RuntimeError(f"host session is closed; cannot {action}")
        if self._mcp_installation_faulted:
            raise RuntimeError(
                "MCP installation commit faulted; only inspect/status/close are allowed"
            )

    def _raise_if_pending_interaction(self, action: str) -> None:
        pending = self.pending_interaction
        if pending is None:
            return
        if isinstance(pending, PendingApproval):
            raise HostSessionPendingApprovalError(
                f"host session has a pending approval; resolve or deny it before {action}"
            )
        raise HostSessionPendingInteractionError(
            f"host session has a pending user interaction; resolve or stop it before {action}"
        )

    def _raise_if_active_run(self) -> None:
        task = self._active_task
        if self._run_lock.locked() or (task is not None and not task.done()):
            raise HostSessionBusyError("host session already has an active run")


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()
