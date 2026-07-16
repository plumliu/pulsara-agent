"""Long-lived conversation session wrapper around AgentRuntime."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, AsyncIterator, Awaitable, Callable, Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    ContextWindowOpenedEvent,
    EventContext,
    EventType,
    McpCapabilitySnapshotInstalledEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    RunInteractionResumeBoundaryEvent,
    RunEndEvent,
    RunStartEvent,
    RolloutBudgetAccountOpenedEvent,
    utc_now,
)
from pulsara_agent.host.run_boundary import (
    BoundaryExecutionHandles,
    CapabilityResolveBasis,
    CommittedInteractionResumeBoundary,
    CommittedRunExecutionOwner,
    HostBoundaryStopResult,
    HostBoundaryStopUncertain,
    HostBoundaryStoppedBeforeCommit,
    HostRunBoundaryAttempt,
    HostRunBoundaryAttemptOutcome,
    InteractionResumeBoundaryInput,
    NewRunBoundaryInput,
    PreparedInteractionResumeBoundary,
    PreparedNewRunBoundary,
    RunExecutionOwnerRegistry,
    RunExecutionSegmentOwner,
    RunExecutionSegmentResult,
    RunSegmentInstallBlocked,
    RunTerminationIntent,
    derive_continuation_basis,
)
from pulsara_agent.primitives.mcp import (
    MAX_MCP_DIAGNOSTIC_CODE_CHARS,
    MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS,
    McpDiagnosticFact,
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpBindingIdentityFact,
    McpInstallationReferenceFact,
)
from pulsara_agent.primitives.capability import build_capability_resolve_basis
from pulsara_agent.primitives.model_call import (
    RunTerminationIntentAttributionFact,
    sha256_fingerprint,
)
from pulsara_agent.llm.control import RunModelCallControlOwner
from pulsara_agent.llm import ModelRole
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.primitives.permission import (
    PermissionMode,
    parse_permission_mode,
)
from pulsara_agent.primitives.run_boundary import (
    BoundaryBatchCommitStatus,
    BoundaryBatchConfirmation,
    BoundaryTranscriptSnapshotFact,
    HostRunBoundaryDisposition,
    HostRunBoundaryPhase,
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
    PlanWorkflowStateFact,
    RunExecutionActivationFact,
    resume_gate_policy_for,
)
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    DurableRunExistence,
    HostRunBoundaryIdentityFact,
    text_sha256,
)
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.transcript import rebuild_prior_messages_bounded
from pulsara_agent.message import SystemMsg
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    PendingApproval,
    pending_approval_from_state,
)
from pulsara_agent.runtime.authority_materialization import RunSeedSourceStale
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
    preset_to_policy,
)
from pulsara_agent.runtime.permission_snapshot import (
    snapshot_from_run_start_event,
    snapshot_from_mode,
    validate_preset_policy_payload,
)
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
    plan_workflow_state_fact,
    pending_plan_interaction_from_state,
    pending_mcp_input_required_from_state,
    reduce_plan_workflow_state,
)
from pulsara_agent.runtime.recovery import AbortKind, StopRequest
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CommittedHostRunEntry,
    install_run_working_set,
    prepare_agent_run_draft,
)
from pulsara_agent.runtime.session import EventPublicationAfterCommitError
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.runtime.state import LoopState, LoopStatus
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
    prepare_root_long_horizon_run,
)
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
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.runtime import (
    CapabilityRuntime,
    FrozenCapabilityExecutionSurface,
)
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


_MAX_RUN_SEED_REFREEZE_ATTEMPTS = 3


def _caused_by(error: BaseException, error_type: type[BaseException]) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


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
            cache_outcome=(
                candidate.cache_outcome if candidate is not None else "not_applicable"
            ),  # type: ignore[arg-type]
            stale_candidates_discarded_since_previous_install=(
                stale_discard_counts.get(snapshot.server_id, 0)
            ),
        )
        diagnostics = tuple(
            _mcp_diagnostic_fact(item) for item in snapshot.diagnostics[:16]
        )
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
            None
            if old.installation_id == "mcp_installation:empty"
            else old.installation_id
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
                code=str(getattr(item, "code", "mcp_installation_diagnostic"))[
                    :MAX_MCP_DIAGNOSTIC_CODE_CHARS
                ],
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
            item.get("message") or item.get("error_type") or "MCP server diagnostic"
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


class _OwnedBoundaryStreamObserver:
    """Detachable iterator whose close works even before the first pull."""

    __slots__ = ("_iterator", "_observer")

    def __init__(
        self,
        iterator: AsyncIterator[AgentEvent],
        observer: _StreamObserver,
    ) -> None:
        self._iterator = iterator
        self._observer = observer

    def __aiter__(self) -> "_OwnedBoundaryStreamObserver":
        return self

    async def __anext__(self) -> AgentEvent:
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        self._observer.detach()
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()


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
    _boundary_task: asyncio.Task[Any] | None = None
    _boundary_attempt: HostRunBoundaryAttempt | None = None
    _boundary_observer: _StreamObserver | None = None
    _preparing_state: LoopState | None = None
    _preparing_identity: HostRunBoundaryIdentityFact | None = None
    _boundary_stop_requested_run_ids: set[str] = field(
        default_factory=set, init=False, repr=False
    )
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
    _run_execution_owners: RunExecutionOwnerRegistry = field(
        default_factory=RunExecutionOwnerRegistry,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        snapshot = self.wiring.runtime_wiring.event_log.read_raw_events_by_types(
            (EventType.PLAN_MODE_ENTERED.value, EventType.PLAN_MODE_EXITED.value),
            max_events=4_096,
            max_payload_bytes=4 * 1024 * 1024,
            deadline_monotonic=time.monotonic() + 10.0,
        )
        reduced = reduce_plan_workflow_state(
            tuple(
                raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                for raw in snapshot.events
            )
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
            snapshots_by_server[server_id] for server_id in sorted(snapshots_by_server)
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
            self.wiring.agent_runtime.refresh_capability_runtime(new_capability_runtime)
            runtime_session.set_mcp_installation_contract(
                installation_id=new_installation.installation_id,
                pending_audit=pending_audit,
            )
            supervisor.acknowledge_stale_discard_counts(stale_discard_counts)
        except BaseException:
            self._mcp_installation_faulted = True
            raise

        runtime_session = self.wiring.runtime_wiring.runtime_session
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

    def _new_run_boundary_identity(
        self,
        state: LoopState,
        *,
        kind: str = "pre_run",
        attempt_number: int = 1,
    ) -> HostRunBoundaryIdentityFact:
        identity = HostRunBoundaryIdentityFact(
            boundary_id=f"run_boundary:{uuid4().hex}",
            kind=kind,
            runtime_session_id=self.runtime_session_id,
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            attempt_number=attempt_number,
            observed_at_utc=utc_now(),
        )
        if self._preparing_state is state:
            self._preparing_identity = identity
        return identity

    def _new_resume_boundary_identity(
        self,
        state: LoopState,
        *,
        interaction_id: str,
    ) -> HostRunBoundaryIdentityFact:
        counters = state.scratchpad.setdefault("resume_boundary_attempts", {})
        if not isinstance(counters, dict):
            raise RuntimeError("resume boundary attempt registry is invalid")
        attempt_number = int(counters.get(interaction_id, 0)) + 1
        counters[interaction_id] = attempt_number
        return self._new_run_boundary_identity(
            state,
            kind="pre_interaction_resume",
            attempt_number=attempt_number,
        )

    def _new_interaction_boundary_input(
        self,
        state: LoopState,
        *,
        interaction_id: str,
        interaction_kind: str,
        resolution: object,
    ) -> InteractionResumeBoundaryInput:
        suspended_token = state.scratchpad.get("suspended_state_token")
        if not isinstance(suspended_token, str):
            raise RuntimeError("suspended run lost its ABA state token")
        identity = self._new_resume_boundary_identity(
            state,
            interaction_id=interaction_id,
        )
        return InteractionResumeBoundaryInput(
            identity=identity,
            interaction_id=interaction_id,
            interaction_kind=interaction_kind,  # type: ignore[arg-type]
            resolution=resolution,
            suspended_state_token=suspended_token,
        )

    def _resolve_new_run_permission_snapshot(
        self,
        *,
        run_id: str,
    ):
        if self.plan_state.active:
            mode = PermissionMode.READ_ONLY
            source = "plan_mode"
        else:
            mode = self.default_permission_mode
            source = "session_default"
        if mode is None:
            raise ValueError("Host run boundary requires a preset permission mode")
        return snapshot_from_mode(
            runtime_session_id=self.runtime_session_id,
            run_id=run_id,
            permission_mode=mode,
            permission_snapshot_source=source,
        )

    def _mcp_installation_reference_fact(self) -> McpInstallationReferenceFact:
        installation = self.wiring.runtime_wiring.mcp_installation
        server_fingerprints = tuple(
            sorted(
                (
                    snapshot.server_id,
                    snapshot.snapshot_semantic_fingerprint,
                )
                for snapshot in installation.snapshots
            )
        )
        bindings = tuple(
            McpBindingIdentityFact(
                server_id=binding.server_id,
                slot_id=binding.slot_id,
                snapshot_id=binding.snapshot_id,
                discovery_generation=binding.discovery_generation,
            )
            for binding in sorted(
                installation.binding_identities,
                key=lambda value: (
                    value.server_id,
                    value.slot_id,
                    value.snapshot_id,
                    value.discovery_generation,
                ),
            )
        )
        return McpInstallationReferenceFact(
            installation_id=installation.installation_id,
            owner_runtime_session_id=self.runtime_session_id,
            config_epoch=installation.config_epoch,
            event_safe_config_set_fingerprint=(
                installation.event_safe_config_set_fingerprint
            ),
            server_snapshot_semantic_fingerprints=server_fingerprints,
            binding_identities=bindings,
        )

    def _plan_workflow_state_fact(self) -> PlanWorkflowStateFact:
        mode = self.default_permission_mode
        if mode is None:
            raise ValueError("session default permission must be a preset")
        return plan_workflow_state_fact(
            self.plan_state,
            inactive_default_permission_mode=mode,
        )

    def _transcript_snapshot_fact(
        self,
        *,
        preflight_terminal: AgentEvent | None,
        checkpoint_terminal: ContextCompactionCompletedEvent | None,
        source_through_sequence: int,
        source_event_count: int,
    ) -> BoundaryTranscriptSnapshotFact:
        if isinstance(
            preflight_terminal,
            (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
        ):
            if preflight_terminal.sequence is None:
                raise ValueError("preflight compaction terminal event is unsequenced")
            compaction_id = preflight_terminal.compaction_id
            terminal_id = preflight_terminal.id
            terminal_sequence = preflight_terminal.sequence
        else:
            compaction_id = None
            terminal_id = None
            terminal_sequence = None
        if checkpoint_terminal is not None:
            if checkpoint_terminal.sequence is None:
                raise ValueError("transcript checkpoint event is unsequenced")
            checkpoint_compaction_id = checkpoint_terminal.compaction_id
            checkpoint_terminal_id = checkpoint_terminal.id
            checkpoint_terminal_sequence = checkpoint_terminal.sequence
            checkpoint_keep_after_sequence = checkpoint_terminal.keep_after_sequence
            compacted_window_id = checkpoint_terminal.window_id
        else:
            checkpoint_compaction_id = None
            checkpoint_terminal_id = None
            checkpoint_terminal_sequence = None
            checkpoint_keep_after_sequence = None
            compacted_window_id = None
        return BoundaryTranscriptSnapshotFact(
            source_through_sequence=source_through_sequence,
            source_event_count=source_event_count,
            compacted_window_id=compacted_window_id,
            checkpoint_compaction_id=checkpoint_compaction_id,
            checkpoint_terminal_event_id=checkpoint_terminal_id,
            checkpoint_terminal_sequence=checkpoint_terminal_sequence,
            checkpoint_keep_after_sequence=checkpoint_keep_after_sequence,
            preflight_compaction_id=compaction_id,
            preflight_compaction_terminal_event_id=terminal_id,
            preflight_compaction_terminal_sequence=terminal_sequence,
        )

    def _capability_basis(
        self,
        *,
        identity: HostRunBoundaryIdentityFact,
        permission_snapshot,
        user_input: str,
        prior_messages: list,
        active_skill_names: frozenset[str],
        execution_surface_identity,
    ) -> CapabilityResolveBasis:
        owner = CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=identity.boundary_id,
            host_boundary_kind=identity.kind,
            runtime_session_id=identity.runtime_session_id,
            run_id=identity.run_id,
        )
        prior_fingerprint = sha256_fingerprint(
            "boundary-prior-transcript:v1",
            [message.model_dump(mode="json") for message in prior_messages],
        )
        fact = build_capability_resolve_basis(
            basis_id=f"capability_basis:{uuid4().hex}",
            basis_kind="initial",
            source_basis_id=None,
            source_basis_fingerprint=None,
            owner=owner,
            workspace_identity_fingerprint=sha256_fingerprint(
                "host-workspace-identity:v1",
                [
                    self.workspace.workspace_kind,
                    self.workspace.workspace_key,
                    self.workspace.memory_domain.memory_domain_id,
                ],
            ),
            memory_domain_id=self.workspace.memory_domain.memory_domain_id,
            permission_snapshot_id=permission_snapshot.snapshot_id,
            plan_active=self.plan_state.active,
            active_skill_names=tuple(sorted(active_skill_names)),
            user_intent_fingerprint=sha256_fingerprint(
                "host-user-intent:v1", user_input
            ),
            prior_transcript_fingerprint=prior_fingerprint,
            mcp_installation_id=execution_surface_identity.mcp_installation_id,
            execution_surface_identity=execution_surface_identity,
        )
        return CapabilityResolveBasis(
            fact=fact,
            user_input=user_input,
            prior_messages=tuple(
                message.model_copy(deep=True) for message in prior_messages
            ),
            active_skill_names=active_skill_names,
            workspace_root=self.workspace.workspace_root,
            memory_domain_id=self.workspace.memory_domain.memory_domain_id,
        )

    def _freeze_new_run_boundary_inputs(
        self,
        *,
        state: LoopState,
        identity: HostRunBoundaryIdentityFact,
        user_input: str,
        active_skill_names: frozenset[str],
        run_model_target,
        permission_snapshot,
        prior_messages: list,
        preflight_terminal: AgentEvent | None,
        checkpoint_terminal: ContextCompactionCompletedEvent | None,
        transcript_source_through_sequence: int,
        transcript_source_event_count: int,
        frozen_surface,
    ) -> None:
        transcript_fact = self._transcript_snapshot_fact(
            preflight_terminal=preflight_terminal,
            checkpoint_terminal=checkpoint_terminal,
            source_through_sequence=transcript_source_through_sequence,
            source_event_count=transcript_source_event_count,
        )
        basis = self._capability_basis(
            identity=identity,
            permission_snapshot=permission_snapshot,
            user_input=user_input,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
            execution_surface_identity=frozen_surface.identity,
        )
        state.permission_snapshot = permission_snapshot
        state.run_model_target = run_model_target
        state.scratchpad["host_run_boundary_identity"] = identity
        state.scratchpad["host_run_boundary_transcript"] = transcript_fact
        state.scratchpad["host_run_boundary_mcp"] = (
            self._mcp_installation_reference_fact()
        )
        state.scratchpad["host_run_boundary_plan"] = self._plan_workflow_state_fact()
        state.scratchpad["capability_resolve_basis"] = basis
        state.scratchpad["frozen_capability_execution_surface"] = frozen_surface
        state.scratchpad["current_user_message_fact"] = CurrentUserMessageFact(
            message_id=f"user-message:{state.run_id}",
            source_kind="host_user_input",
            text=user_input,
            observed_at_utc=identity.observed_at_utc,
            content_sha256=text_sha256(user_input),
            source_artifact_id=None,
        )
        state.scratchpad["terminal_run_end_event_id"] = f"run_end:{uuid4().hex}"
        state.scratchpad["new_run_boundary_fact"] = NewRunBoundaryFact(
            identity=identity,
            transcript=transcript_fact,
            model_target_fingerprint=run_model_target.fact.target_fingerprint,
            permission_snapshot_id=permission_snapshot.snapshot_id,
            mcp_installation_id=(frozen_surface.identity.mcp_installation_id),
            capability_basis=basis.fact,
            degraded_reason_codes=(),
        )

    async def _commit_new_run_entry(
        self,
        *,
        state: LoopState,
        prepared: PreparedNewRunBoundary,
    ) -> tuple[AgentRunDraft, CommittedHostRunEntry, tuple[AgentEvent, ...]]:
        draft = await prepare_agent_run_draft(
            self.wiring.agent_runtime,
            state,
            run_model_target=prepared.run_model_target,
            permission_snapshot=prepared.permission_snapshot,
            current_user_message=prepared.current_user_message,
            run_start_event_id=prepared.run_start_event_id,
            terminal_run_end_event_id=prepared.terminal_run_end_event_id,
            capability_basis=prepared.capability_basis.fact,
            frozen_execution_surface=prepared.frozen_execution_surface,
            new_run_boundary=prepared.new_run_boundary,
            subagent_run_entry=None,
            long_horizon=prepared.long_horizon,
            child_rollout_subaccount=None,
            prior_messages=list(prepared.owned_transcript_messages),
        )
        runtime_session = self.wiring.runtime_wiring.runtime_session
        pending_audits = prepared.pending_mcp_audits
        event_context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        window_open = ContextWindowOpenedEvent(
            id=prepared.long_horizon.contract.initial_window_open_event_id,
            **event_context.event_fields(),
            window=prepared.long_horizon.initial_window,
            opening_batch_id=prepared.long_horizon.opening_batch_id,
        )
        account = prepared.long_horizon.root_account
        if account is None:
            raise RuntimeError("host run requires a root rollout account")
        account_open = RolloutBudgetAccountOpenedEvent(
            id=f"rollout_budget_account_opened:{account.account_id}",
            **event_context.event_fields(),
            account=account,
        )
        candidates = (
            draft.run_start_event,
            window_open,
            account_open,
            *pending_audits,
        )
        self._set_boundary_candidates(candidates)
        self._set_boundary_phase(HostRunBoundaryPhase.DURABLE_COMMIT)
        self._set_boundary_commit_state("commit_in_flight")
        try:
            stored = tuple(await runtime_session.emit_many(candidates, state=state))
        except BaseException as exc:
            if isinstance(exc, EventPublicationAfterCommitError):
                self._set_boundary_commit_state("publication_failed")
                confirmed = tuple(exc.result.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=confirmed,
                )
            else:
                outcome = runtime_session.resolved_event_write_outcome(exc)
                if outcome.status != "full":
                    if outcome.status == "none":
                        runtime_session.transcript_projection_checkpoint_service.discard_prepared_run_seed(
                            state.run_id
                        )
                    self._set_boundary_commit_confirmation(
                        BoundaryBatchCommitStatus.UNKNOWN
                        if outcome.status == "unknown"
                        else BoundaryBatchCommitStatus.NONE,
                    )
                    self._set_boundary_commit_state(
                        "commit_outcome_unknown"
                        if outcome.status == "unknown"
                        else "not_started"
                    )
                    raise
                self._set_boundary_commit_state("committed")
                confirmed = tuple(outcome.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=confirmed,
                )
            runtime_session.acknowledge_committed_mcp_installation_audits(confirmed)
            committed = self._committed_host_entry_from_stored(
                confirmed,
                publication_status="failed_after_commit",
            )
            await self._adopt_committed_host_run(
                state=state,
                committed=committed,
                prepared=prepared,
            )
            if isinstance(exc, asyncio.CancelledError):
                _clear_current_task_cancellation()
            if state.stop_request is not None:
                await self._install_run_termination_intent(
                    state, state.stop_request.reason
                )
                await self._terminalize_committed_run_after_boundary_failure(
                    state=state,
                    abort_reason=state.stop_request.reason,
                )
            else:
                await self._terminalize_committed_run_after_boundary_failure(
                    state=state,
                    stop_reason=(
                        RunStopReason.RUNTIME_PUBLICATION_FAILURE
                        if isinstance(exc, EventPublicationAfterCommitError)
                        else RunStopReason.RUNTIME_EXECUTION_ERROR
                    ),
                    error_message=(
                        "run boundary failed after durable RunStart: "
                        f"{type(exc).__name__}"
                    ),
                )
            raise exc
        self._set_boundary_commit_confirmation(
            BoundaryBatchCommitStatus.FULL,
            committed_events=stored,
        )
        runtime_session.acknowledge_committed_mcp_installation_audits(stored)
        self._set_boundary_commit_state("committed")
        committed = self._committed_host_entry_from_stored(
            stored,
            publication_status="completed",
        )
        await self._adopt_committed_host_run(
            state=state,
            committed=committed,
            prepared=prepared,
        )
        return draft, committed, stored

    def _committed_host_entry_from_stored(
        self,
        stored: tuple[AgentEvent, ...],
        *,
        publication_status: Literal["completed", "failed_after_commit", "unavailable"],
    ) -> CommittedHostRunEntry:
        if not stored:
            raise RuntimeError("new-run boundary committed an empty batch")
        run_start = stored[0]
        if not isinstance(run_start, RunStartEvent) or run_start.sequence is None:
            raise RuntimeError("new-run boundary did not commit a sequenced RunStart")
        through_sequence = stored[-1].sequence
        if through_sequence is None:
            raise RuntimeError("new-run boundary committed an unsequenced audit")
        boundary = run_start.new_run_boundary
        if boundary is None:
            raise RuntimeError("Host RunStart lost its new-run boundary fact")
        return CommittedHostRunEntry(
            run_start_event=run_start,
            run_start_sequence=run_start.sequence,
            committed_through_sequence=through_sequence,
            publication_status=publication_status,
            boundary_id=boundary.identity.boundary_id,
            committed_audit_event_ids=tuple(
                event.id
                for event in stored
                if isinstance(event, McpCapabilitySnapshotInstalledEvent)
            ),
        )

    def _new_execution_handles(
        self,
        *,
        owner_id: str,
        generation: int,
        frozen_execution_surface: FrozenCapabilityExecutionSurface,
        state: str = "attempt_owned",
    ) -> BoundaryExecutionHandles:
        return BoundaryExecutionHandles(
            handle_id=f"run_execution_handles:{uuid4().hex}",
            handle_generation=generation,
            owner_id=owner_id,
            state=state,  # type: ignore[arg-type]
            mcp_installation=self.wiring.runtime_wiring.mcp_installation,
            capability_runtime=self.wiring.agent_runtime.capability_runtime,
            tool_registry=self.wiring.agent_runtime.tool_executor.registry,
            frozen_execution_surface=frozen_execution_surface,
        )

    def _register_committed_host_run_owner(
        self,
        *,
        state: LoopState,
        committed: CommittedHostRunEntry,
        prepared: PreparedNewRunBoundary,
    ) -> None:
        self.wiring.runtime_wiring.runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
            committed.run_start_event
        )
        install_run_working_set(
            state,
            committed,
            plan_snapshot=prepared.plan_snapshot,
            capability_resolve_basis=prepared.capability_basis,
            frozen_execution_surface=prepared.frozen_execution_surface,
        )
        attempt = self._boundary_attempt
        if attempt is None or attempt.execution_handles is None:
            raise RuntimeError("committed run lost its attempt-owned execution handles")
        handles = attempt.execution_handles
        if handles.frozen_execution_surface is not prepared.frozen_execution_surface:
            raise RuntimeError("committed run execution surface drifted after freeze")
        handles.transfer_to_run(state.run_id)
        owner = CommittedRunExecutionOwner(
            entry=committed,
            execution_handles=handles,
            retiring_execution_handles={},
            terminal_event_id=committed.run_start_event.terminal_run_end_event_id,
            terminal_candidate=None,
            terminal_state="open",
            terminalization_task=None,
            termination_intent=None,
            run_completion=asyncio.get_running_loop().create_future(),
            next_segment_generation=0,
            active_segment=None,
            latest_activation_owner_kind="host_run_boundary",
            latest_activation_owner_id=committed.boundary_id,
        )
        self._run_execution_owners.register(state.run_id, owner)
        state.scratchpad["run_execution_handle_id"] = handles.handle_id
        state.scratchpad["capability_execution_borrow_authority"] = (
            handles.borrow_authority
        )

    async def _adopt_committed_host_run(
        self,
        *,
        state: LoopState,
        committed: CommittedHostRunEntry,
        prepared: PreparedNewRunBoundary,
    ) -> None:
        """Install the process owner or durably close the already-started run."""

        try:
            self._register_committed_host_run_owner(
                state=state,
                committed=committed,
                prepared=prepared,
            )
        except BaseException as ownership_error:
            if isinstance(ownership_error, asyncio.CancelledError):
                _clear_current_task_cancellation()
            try:
                # RunStart is already canonical. Even if process-owner
                # installation failed before it installed the working set, the
                # stable RunEnd builder still needs the committed run contract.
                if state.run_working_set is None:
                    install_run_working_set(
                        state,
                        committed,
                        plan_snapshot=prepared.plan_snapshot,
                        capability_resolve_basis=prepared.capability_basis,
                        frozen_execution_surface=prepared.frozen_execution_surface,
                    )
                await self.wiring.agent_runtime.fail_committed_run(
                    state,
                    stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                    error_message=(
                        "committed RunStart owner installation failed: "
                        f"{type(ownership_error).__name__}"
                    ),
                )
            except BaseException:
                self.wiring.runtime_wiring.runtime_session.latch_event_commit_outcome_unknown()
                raise
            raise ownership_error

    async def _terminalize_committed_run_after_boundary_failure(
        self,
        *,
        state: LoopState,
        stop_reason: RunStopReason | None = None,
        error_message: str | None = None,
        abort_reason: AbortKind | None = None,
    ) -> AgentRunResult:
        try:
            if abort_reason is not None:
                result = await self.wiring.agent_runtime.abort_run(
                    state,
                    reason=abort_reason,
                )
            else:
                if stop_reason is None or error_message is None:
                    raise ValueError("failure terminalization requires typed reason")
                result = await self.wiring.agent_runtime.fail_committed_run(
                    state,
                    stop_reason=stop_reason,
                    error_message=error_message,
                )
        except EventPublicationAfterCommitError:
            if not state.finalized:
                raise
            result = self.wiring.agent_runtime._run_result(state)
        self._fold_run_owner_terminal(result)
        return result

    async def _prepare_and_commit_new_run_boundary(
        self,
        *,
        user_input: str,
        active_skill_names: frozenset[str],
        state: LoopState,
        identity: HostRunBoundaryIdentityFact,
    ) -> tuple[AgentRunDraft, CommittedHostRunEntry, tuple[AgentEvent, ...]]:
        """The sole PRE_RUN coordinator, called with ``_run_lock`` held."""

        self._set_boundary_phase(HostRunBoundaryPhase.ADMISSION)
        self._require_new_run_admission("starting a new turn")
        agent = self.wiring.agent_runtime
        if (
            agent._subagent_parent_features_enabled
            and agent.subagent_runtime is not None
            and not agent._subagent_dangling_repair_done
        ):
            # Repair can append parent-graph facts, so it must complete before
            # transcript/watermark freeze rather than inside the draft builder.
            await agent.subagent_runtime.repair_dangling_children()
            agent._subagent_dangling_repair_done = True
        self._set_boundary_phase(HostRunBoundaryPhase.CONTRACT_RESOLUTION)
        run_model_target = self.wiring.agent_runtime.resolve_run_model_target()
        permission_snapshot = self._resolve_new_run_permission_snapshot(
            run_id=state.run_id
        )
        self._set_boundary_phase(HostRunBoundaryPhase.MCP_REQUIRED_WAIT)
        await self._apply_mcp_safe_point(trigger="config_change")
        self._set_boundary_phase(HostRunBoundaryPhase.MCP_INSTALLATION)
        frozen_surface = (
            self.wiring.agent_runtime.capability_runtime.freeze_execution_surface(
                CapabilityExecutionSurfaceSnapshotContext(
                    workspace_root=self.workspace.workspace_root,
                    workspace_kind=self.workspace.workspace_kind,
                    available_tool_names=frozenset(
                        self.wiring.agent_runtime.tool_executor.registry.names()
                    ),
                    mcp_installation_id=(
                        self.wiring.runtime_wiring.mcp_installation.installation_id
                    ),
                ),
                tool_registry=self.wiring.agent_runtime.tool_executor.registry,
                archive=self.wiring.runtime_wiring.archive,
                runtime_session_id=self.runtime_session_id,
                owner_id=identity.boundary_id,
            )
        )
        self._set_boundary_phase(HostRunBoundaryPhase.PREFLIGHT_COMPACTION)
        (
            prior_messages,
            preflight_terminal,
            transcript_source_through_sequence,
            transcript_source_event_count,
            transcript_checkpoint_terminal,
        ) = await self._prepare_prior_messages_for_turn(
            user_input,
            target_model_target=run_model_target,
            host_boundary_id=identity.boundary_id,
        )
        prior_messages.extend(self._plan_runtime_messages())
        self._set_boundary_phase(HostRunBoundaryPhase.FINAL_FREEZE)
        self._freeze_new_run_boundary_inputs(
            state=state,
            identity=identity,
            user_input=user_input,
            active_skill_names=active_skill_names,
            run_model_target=run_model_target,
            permission_snapshot=permission_snapshot,
            prior_messages=prior_messages,
            preflight_terminal=preflight_terminal,
            checkpoint_terminal=transcript_checkpoint_terminal,
            transcript_source_through_sequence=(transcript_source_through_sequence),
            transcript_source_event_count=transcript_source_event_count,
            frozen_surface=frozen_surface,
        )
        plan_snapshot = state.scratchpad.get("host_run_boundary_plan")
        transcript_fact = state.scratchpad.get("host_run_boundary_transcript")
        capability_basis = state.scratchpad.get("capability_resolve_basis")
        if not isinstance(plan_snapshot, PlanWorkflowStateFact):
            raise RuntimeError("new-run boundary lost its plan snapshot")
        if not isinstance(transcript_fact, BoundaryTranscriptSnapshotFact):
            raise RuntimeError("new-run boundary lost its transcript fact")
        if not isinstance(capability_basis, CapabilityResolveBasis):
            raise RuntimeError("new-run boundary lost its capability basis")
        pending_audits = tuple(
            self.wiring.runtime_wiring.runtime_session.pending_mcp_installation_audit_events(
                EventContext(
                    run_id=state.run_id,
                    turn_id=state.turn_id,
                    reply_id=state.reply_id,
                )
            )
        )
        run_start_event_id = f"run_start:{uuid4().hex}"
        summarizer_target = self.wiring.agent_runtime.llm_runtime.resolve_target(
            role=ModelRole.FLASH
        )
        self.wiring.agent_runtime.require_prevalidated_rollout_pair(
            execution_profile_kind="host_root",
            execution_profile_id=f"host_{run_model_target.fact.model_role}",
            primary_target=run_model_target,
            summarizer_target=summarizer_target,
        )
        long_horizon = prepare_root_long_horizon_run(
            runtime_session_id=self.runtime_session_id,
            run_id=state.run_id,
            run_start_event_id=run_start_event_id,
            primary_target=run_model_target.fact,
            summarizer_target=summarizer_target.fact,
            graph_reducer_contract=(
                self.wiring.runtime_wiring.runtime_session.subagent_graph_checkpoint_service.reducer_binding.contract
            ),
            source_through_sequence_at_open=transcript_source_through_sequence,
            initial_projection_unit_count=0,
            initial_projection_state_fingerprint=(empty_projection_state_fingerprint()),
        )
        prepared = PreparedNewRunBoundary(
            identity=identity,
            run_model_target=run_model_target,
            permission_snapshot=permission_snapshot,
            plan_snapshot=plan_snapshot,
            mcp_installation_fact=self._mcp_installation_reference_fact(),
            owned_transcript_messages=tuple(
                message.model_copy(deep=True) for message in prior_messages
            ),
            transcript_fact=transcript_fact,
            capability_basis=capability_basis,
            current_user_message=state.scratchpad["current_user_message_fact"],
            run_start_event_id=run_start_event_id,
            terminal_run_end_event_id=state.scratchpad["terminal_run_end_event_id"],
            new_run_boundary=state.scratchpad["new_run_boundary_fact"],
            frozen_execution_surface=frozen_surface,
            pending_mcp_audits=pending_audits,
            long_horizon=long_horizon,
            diagnostics=(),
        )
        attempt = self._boundary_attempt
        if attempt is None:
            raise RuntimeError("new-run boundary lost its process owner")
        attempt.execution_handles = self._new_execution_handles(
            owner_id=identity.boundary_id,
            generation=1,
            frozen_execution_surface=frozen_surface,
        )
        draft, committed, stored = await self._commit_new_run_entry(
            state=state,
            prepared=prepared,
        )
        self._set_boundary_phase(HostRunBoundaryPhase.ACTIVATION)
        return draft, committed, stored

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
        state = self.wiring.agent_runtime.new_state()
        identity = self._new_run_boundary_identity(state)
        boundary_input = NewRunBoundaryInput(
            identity=identity,
            user_input=user_input,
            active_skill_names=active_skill_names or frozenset(),
            host_session_id=self.host_session_id,
            conversation_id=self.conversation_id,
        )
        task = self._create_owned_boundary_task(
            lambda: self._run_turn_pipeline(
                boundary_input=boundary_input,
                state=state,
            ),
            preparing_state=state,
            preparing_identity=identity,
        )
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if (
                    not task.cancelled()
                    or state.run_id not in self._boundary_stop_requested_run_ids
                ):
                    raise
                if state.finalized:
                    return self.wiring.agent_runtime._run_result(state)
                owner = self._run_execution_owners.get(state.run_id)
                if owner is None:
                    raise
                return await asyncio.shield(owner.run_completion)
        finally:
            self._boundary_stop_requested_run_ids.discard(state.run_id)

    async def _run_turn_pipeline(
        self,
        *,
        boundary_input: NewRunBoundaryInput,
        state: LoopState,
    ) -> AgentRunResult:
        async with self._run_lock:
            try:
                for attempt_index in range(_MAX_RUN_SEED_REFREEZE_ATTEMPTS):
                    try:
                        (
                            draft,
                            committed,
                            _stored,
                        ) = await self._prepare_and_commit_new_run_boundary(
                            user_input=boundary_input.user_input,
                            active_skill_names=boundary_input.active_skill_names,
                            state=state,
                            identity=boundary_input.identity,
                        )
                    except BaseException as exc:
                        if (
                            attempt_index + 1
                            >= _MAX_RUN_SEED_REFREEZE_ATTEMPTS
                            or not _caused_by(exc, RunSeedSourceStale)
                        ):
                            raise
                        self._reset_boundary_after_run_seed_source_stale()
                        continue
                    break
                else:  # pragma: no cover - bounded loop always breaks or raises.
                    raise AssertionError("run-seed re-freeze loop exhausted")
                self._activate_committed_state(state, committed)
                self._complete_boundary_attempt_after_activation()
                return await self._run_owned(
                    state,
                    lambda: self.wiring.agent_runtime.run_committed_entry(
                        draft,
                        committed,
                        active_skill_names=boundary_input.active_skill_names,
                    ),
                )
            except BaseException as exc:
                await self._terminalize_post_commit_pipeline_failure(state, exc)
                raise

    def _reset_boundary_after_run_seed_source_stale(self) -> None:
        attempt = self._boundary_attempt
        if attempt is None:
            raise RuntimeError("run-seed source retry lost its boundary owner")
        if (
            attempt.commit_confirmation is None
            or attempt.commit_confirmation.status is not BoundaryBatchCommitStatus.NONE
        ):
            raise RuntimeError(
                "run-seed source retry requires a confirmed-NONE boundary batch"
            )
        attempt.phase = HostRunBoundaryPhase.ADMISSION
        attempt.execution_handles = None
        attempt.candidate_events = ()
        attempt.candidate_event_ids = ()
        attempt.candidate_payload_fingerprints = ()
        attempt.commit_state = "not_started"
        attempt.commit_confirmation = None

    def stream_turn(
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
        state = self.wiring.agent_runtime.new_state()
        identity = self._new_run_boundary_identity(state)
        boundary_input = NewRunBoundaryInput(
            identity=identity,
            user_input=user_input,
            active_skill_names=active_skill_names or frozenset(),
            host_session_id=self.host_session_id,
            conversation_id=self.conversation_id,
        )
        return self._start_owned_boundary_stream(
            lambda: self._stream_turn_pipeline(
                boundary_input,
                state=state,
            ),
            preparing_state=state,
            preparing_identity=identity,
        )

    async def _stream_turn_pipeline(
        self,
        boundary_input: NewRunBoundaryInput,
        *,
        state: LoopState,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            try:
                (
                    draft,
                    committed,
                    stored,
                ) = await self._prepare_and_commit_new_run_boundary(
                    user_input=boundary_input.user_input,
                    active_skill_names=boundary_input.active_skill_names,
                    state=state,
                    identity=boundary_input.identity,
                )
                self._activate_committed_state(state, committed)
                self._complete_boundary_attempt_after_activation()
                for event in stored:
                    yield event
                async for event in self._stream_events_in_boundary_driver(
                    state,
                    lambda: self.wiring.agent_runtime.stream_committed_entry(
                        draft,
                        committed,
                        active_skill_names=boundary_input.active_skill_names,
                    ),
                ):
                    yield event
            except BaseException as exc:
                await self._terminalize_post_commit_pipeline_failure(state, exc)
                raise

    async def _terminalize_post_commit_pipeline_failure(
        self,
        state: LoopState,
        exc: BaseException,
    ) -> None:
        owner = self._run_execution_owners.get(state.run_id)
        if owner is None or state.finalized:
            return
        if isinstance(exc, asyncio.CancelledError):
            _clear_current_task_cancellation()
        await self._terminalize_committed_run_after_boundary_failure(
            state=state,
            stop_reason=(
                RunStopReason.RUNTIME_PUBLICATION_FAILURE
                if isinstance(exc, EventPublicationAfterCommitError)
                else RunStopReason.RUNTIME_EXECUTION_ERROR
            ),
            error_message=(f"committed Host run pipeline failed: {type(exc).__name__}"),
        )
        if state.finalized:
            self._finish_active_run()

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
        state = self._require_suspended_state(pending)
        boundary_input = self._new_interaction_boundary_input(
            state,
            interaction_id=resolution.approval_id,
            interaction_kind="approval",
            resolution=resolution,
        )
        task = self._create_owned_boundary_task(
            lambda: self._resolve_interaction_pipeline(
                pending=pending,
                boundary_input=boundary_input,
                router=lambda state: self.wiring.agent_runtime.resume_after_approval(
                    state, resolution
                ),
            ),
            preparing_state=state,
            preparing_identity=boundary_input.identity,
        )
        return await asyncio.shield(task)

    async def _resolve_interaction_pipeline(
        self,
        *,
        pending: PendingInteraction,
        boundary_input: InteractionResumeBoundaryInput,
        router: Callable[[LoopState], Awaitable[AgentRunResult]],
        prepare_plan_state: bool = False,
        recover_pending_on_publication_failure: bool = False,
    ) -> AgentRunResult:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=boundary_input.interaction_id,
                interaction_kind=boundary_input.interaction_kind,
            )
            await self._prepare_and_commit_resume_boundary(
                pending=pending,
                interaction_kind=boundary_input.interaction_kind,
                identity=boundary_input.identity,
            )
            state = self._resume_active_state(pending)
            self._complete_boundary_attempt_after_activation()
            if prepare_plan_state:
                self._prepare_state_for_plan(state)
            try:
                return await self._run_owned(state, lambda: router(state))
            except EventPublicationAfterCommitError:
                if recover_pending_on_publication_failure:
                    self._capture_pending_interaction(state)
                raise

    def stream_approval_resolution(
        self,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        state = self._require_suspended_state(pending)
        boundary_input = self._new_interaction_boundary_input(
            state,
            interaction_id=resolution.approval_id,
            interaction_kind="approval",
            resolution=resolution,
        )
        return self._start_owned_boundary_stream(
            lambda: self._stream_approval_resolution_pipeline(
                pending=pending,
                resolution=resolution,
                boundary_input=boundary_input,
            ),
            preparing_state=state,
            preparing_identity=boundary_input.identity,
        )

    async def _stream_approval_resolution_pipeline(
        self,
        *,
        pending: PendingApproval,
        resolution: ApprovalResolution,
        boundary_input: InteractionResumeBoundaryInput,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=resolution.approval_id,
                interaction_kind="approval",
            )
            (
                _state,
                _committed,
                boundary_events,
            ) = await self._prepare_and_commit_resume_boundary(
                pending=pending,
                interaction_kind="approval",
                identity=boundary_input.identity,
            )
            state = self._resume_active_state(pending)
            self._complete_boundary_attempt_after_activation()
            for event in boundary_events:
                yield event
            async for event in self._stream_events_in_boundary_driver(
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
        state = self._require_suspended_state(pending)
        boundary_input = self._new_interaction_boundary_input(
            state,
            interaction_id=resolution.interaction_id,
            interaction_kind="plan",
            resolution=resolution,
        )
        task = self._create_owned_boundary_task(
            lambda: self._resolve_interaction_pipeline(
                pending=pending,
                boundary_input=boundary_input,
                router=lambda state: (
                    self.wiring.agent_runtime.resume_after_plan_interaction(
                        state, resolution
                    )
                ),
                prepare_plan_state=True,
            ),
            preparing_state=state,
            preparing_identity=boundary_input.identity,
        )
        return await asyncio.shield(task)

    async def resolve_mcp_input_required(
        self,
        resolution: McpInputRequiredInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving MCP input-required")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_mcp_input_required(resolution.interaction_id)
        self._raise_if_active_run()
        state = self._require_suspended_state(pending)
        boundary_input = self._new_interaction_boundary_input(
            state,
            interaction_id=resolution.interaction_id,
            interaction_kind="mcp_input_required",
            resolution=resolution,
        )
        task = self._create_owned_boundary_task(
            lambda: self._resolve_interaction_pipeline(
                pending=pending,
                boundary_input=boundary_input,
                router=lambda state: (
                    self.wiring.agent_runtime.resume_after_mcp_input_required(
                        state, resolution
                    )
                ),
                recover_pending_on_publication_failure=True,
            ),
            preparing_state=state,
            preparing_identity=boundary_input.identity,
        )
        return await asyncio.shield(task)

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
        pending = self.pending_interaction
        if pending is not None:
            if not isinstance(pending, PendingPlanInteraction):
                raise HostSessionPendingInteractionError(
                    "host session has a non-plan pending interaction; resolve or stop it before exiting plan"
                )
            if pending.kind != "exit" and source != "user_force_exit":
                raise HostSessionPendingInteractionError(
                    "host session has a pending plan question; answer it or use force-exit before cancelling plan"
                )
            state = self._require_suspended_state(pending)
            boundary_input = self._new_interaction_boundary_input(
                state,
                interaction_id=pending.interaction_id,
                interaction_kind="plan",
                resolution={
                    "source": source,
                    "user_feedback": user_feedback,
                },
            )
            task = self._create_owned_boundary_task(
                lambda: self._exit_pending_plan_workflow_pipeline(
                    pending=pending,
                    source=source,
                    user_feedback=user_feedback,
                    boundary_input=boundary_input,
                ),
                preparing_state=state,
                preparing_identity=boundary_input.identity,
            )
            await asyncio.shield(task)
            return

        async with self._run_lock:
            workflow = self._plan_workflow_state_fact()
            if (
                workflow.entry_run_id is None
                or workflow.entry_turn_id is None
                or workflow.entry_reply_id is None
            ):
                raise RuntimeError(
                    "host plan workflow exit requires durable entry attribution"
                )
            await self._emit_plan_mode_exited(
                None,
                source=source,
                exit_request_id=None,
                event_context=EventContext(
                    run_id=workflow.entry_run_id,
                    turn_id=workflow.entry_turn_id,
                    reply_id=workflow.entry_reply_id,
                ),
                transition_owner="host_workflow",
                host_workflow_operation_id=f"host_plan_workflow:{uuid4().hex}",
            )

    async def _exit_pending_plan_workflow_pipeline(
        self,
        *,
        pending: PendingPlanInteraction,
        source: str,
        user_feedback: str,
        boundary_input: InteractionResumeBoundaryInput,
    ) -> None:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=pending.interaction_id,
                interaction_kind="plan",
            )
            await self._prepare_and_commit_resume_boundary(
                pending=pending,
                interaction_kind="plan",
                identity=boundary_input.identity,
            )
            state = self._resume_active_state(pending)
            self._complete_boundary_attempt_after_activation()
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
            await self._emit_plan_mode_exited(
                state,
                source=source,
                exit_request_id=pending.exit_request_id,
                event_context=None,
                transition_owner="agent_run",
                host_workflow_operation_id=None,
            )
            await self._install_run_termination_intent(state, AbortKind.USER_STOP)
            result = await self.wiring.agent_runtime.abort_run(
                state, reason=AbortKind.USER_STOP
            )
            self._fold_run_owner_terminal(result)
            self._suspended_state = None
            self.suspended_run_id = None
            self.pending_interaction = None
            self._finish_active_run()

    def stream_plan_interaction_resolution(
        self,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        state = self._require_suspended_state(pending)
        boundary_input = self._new_interaction_boundary_input(
            state,
            interaction_id=resolution.interaction_id,
            interaction_kind="plan",
            resolution=resolution,
        )
        return self._start_owned_boundary_stream(
            lambda: self._stream_plan_interaction_resolution_pipeline(
                pending=pending,
                resolution=resolution,
                boundary_input=boundary_input,
            ),
            preparing_state=state,
            preparing_identity=boundary_input.identity,
        )

    async def _stream_plan_interaction_resolution_pipeline(
        self,
        *,
        pending: PendingPlanInteraction,
        resolution: PlanInteractionResolution,
        boundary_input: InteractionResumeBoundaryInput,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=resolution.interaction_id,
                interaction_kind="plan",
            )
            (
                _state,
                _committed,
                boundary_events,
            ) = await self._prepare_and_commit_resume_boundary(
                pending=pending,
                interaction_kind="plan",
                identity=boundary_input.identity,
            )
            state = self._resume_active_state(pending)
            self._complete_boundary_attempt_after_activation()
            self._prepare_state_for_plan(state)
            for event in boundary_events:
                yield event
            async for event in self._stream_events_in_boundary_driver(
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
    ) -> AgentRunResult | HostBoundaryStopResult | None:
        self._raise_if_not_open("stopping the current turn")
        async with self._stop_lock:
            task = self._active_task
            state = self._active_state
            boundary_task = self._boundary_task
            preparing_state = self._preparing_state
            if (
                boundary_task is not None
                and not boundary_task.done()
                and (task is None or task.done())
            ):
                boundary_attempt = self._boundary_attempt
                observer = self._boundary_observer
                if observer is not None:
                    observer.detach()
                if preparing_state is not None:
                    preparing_state.stop_request = StopRequest(reason=reason)
                    self.stopping_run_id = preparing_state.run_id
                    self._boundary_stop_requested_run_ids.add(
                        preparing_state.run_id
                    )
                boundary_task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(boundary_task), timeout=timeout
                    )
                except asyncio.CancelledError:
                    pass
                except TimeoutError:
                    return None
                except Exception:
                    pass
                finally:
                    self.stopping_run_id = None
                boundary_outcome = (
                    await asyncio.shield(boundary_attempt.completion)
                    if boundary_attempt is not None
                    else None
                )
                if preparing_state is not None:
                    if preparing_state.finalized:
                        return self.wiring.agent_runtime._run_result(preparing_state)
                    if (
                        self._run_execution_owners.get(preparing_state.run_id)
                        is not None
                    ):
                        await self._install_run_termination_intent(
                            preparing_state, reason
                        )
                        result = await self.wiring.agent_runtime.abort_run(
                            preparing_state, reason=reason
                        )
                        self._fold_run_owner_terminal(result)
                        self._complete_pending_mcp_lease_for_state(preparing_state)
                        if self._suspended_state is preparing_state:
                            self._suspended_state = None
                            self.suspended_run_id = None
                            self.pending_interaction = None
                        return result
                if boundary_attempt is None or boundary_outcome is None:
                    return None
                if boundary_outcome.durable_run_existence is DurableRunExistence.NONE:
                    return HostBoundaryStoppedBeforeCommit(
                        status="cancelled_before_run_start",
                        boundary_id=boundary_attempt.boundary_id,
                        draft_run_id=boundary_attempt.draft_run_id,
                        durable_run_existence=DurableRunExistence.NONE,
                        diagnostics=boundary_outcome.diagnostics,
                    )
                if boundary_outcome.durable_run_existence in {
                    DurableRunExistence.UNKNOWN,
                    DurableRunExistence.PARTIAL_UNTRUSTED,
                }:
                    confirmation = boundary_outcome.commit_confirmation
                    if confirmation is None:
                        raise RuntimeError(
                            "uncertain boundary stop requires commit confirmation"
                        )
                    return HostBoundaryStopUncertain(
                        status=(
                            "ledger_latched"
                            if boundary_outcome.durable_run_existence
                            is DurableRunExistence.PARTIAL_UNTRUSTED
                            else "commit_outcome_unknown"
                        ),
                        boundary_id=boundary_attempt.boundary_id,
                        draft_run_id=boundary_attempt.draft_run_id,
                        durable_run_existence=(boundary_outcome.durable_run_existence),
                        commit_confirmation=confirmation,
                        diagnostics=boundary_outcome.diagnostics,
                    )
                raise RuntimeError("committed boundary stop lost its durable run owner")
            if self.pending_interaction is not None and (task is None or task.done()):
                if self._run_lock.locked():
                    raise HostSessionBusyError("host session already has an active run")
                async with self._run_lock:
                    pending = self.pending_interaction
                    if pending is None:
                        return None
                    state = self._require_suspended_state(pending)
                    await self._install_run_termination_intent(state, reason)
                    self.active_run_id = state.run_id
                    self.stopping_run_id = state.run_id
                    self.last_active_at = time.monotonic()
                    try:
                        result = await self.wiring.agent_runtime.abort_run(
                            state, reason=reason
                        )
                        self._fold_run_owner_terminal(result)
                        self._complete_pending_mcp_lease_for_state(state)
                        self._capture_pending_interaction(result.state)
                        if result.state.finalized:
                            self._retire_confirmed_run_owner(state.run_id)
                        return result
                    finally:
                        self.active_run_id = None
                        self.stopping_run_id = None
                        self.last_active_at = time.monotonic()

            if task is None or state is None:
                return None
            if task.done():
                owner = self._run_execution_owners.get(state.run_id)
                if owner is None or owner.terminal_state == "confirmed":
                    return None
                result = await self.wiring.agent_runtime.retry_run_terminalization(
                    state
                )
                self._fold_run_owner_terminal(result)
                if not state.finalized:
                    raise RuntimeError("RunEnd retry did not reach durable commit")
                self._finish_active_run()
                return result
            self.stopping_run_id = state.run_id
            self._boundary_stop_requested_run_ids.add(state.run_id)
            await self._install_run_termination_intent(state, reason)
            state.stop_request = StopRequest(reason=reason)
            runtime_session = self.wiring.runtime_wiring.runtime_session
            cancel_reason = _model_stream_cancel_reason(reason)
            active_model_handles = await runtime_session.model_stream_execution_registry.request_cancel_run(
                state.run_id,
                reason=cancel_reason,
            )
            if active_model_handles == 0:
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
            result = await self.wiring.agent_runtime.abort_run(state, reason=reason)
            self._fold_run_owner_terminal(result)
            self._complete_pending_mcp_lease_for_state(state)
            return result

    def _complete_pending_mcp_lease_for_state(self, state: LoopState) -> None:
        if (
            state.pending_interaction_kind != "mcp_input_required"
            or self.mcp_supervisor is None
        ):
            return
        interaction_id = state.pending_interaction_payload.get("interaction_id")
        if isinstance(interaction_id, str) and interaction_id:
            self.mcp_supervisor.complete_pending_lease(interaction_id)

    async def _install_run_termination_intent(
        self,
        state: LoopState,
        reason: AbortKind,
    ) -> RunTerminationIntent | None:
        owner = self._run_execution_owners.get(state.run_id)
        if owner is None:
            return None
        segment = owner.active_segment
        intent = RunTerminationIntent(
            intent_id=f"run_termination_intent:{uuid4().hex}",
            kind=reason.value,  # type: ignore[arg-type]
            requested_at_utc=utc_now(),
            requester_id=self.host_session_id,
            target_segment_id=segment.segment_id if segment is not None else None,
            target_segment_generation=(
                segment.segment_generation if segment is not None else None
            ),
        )
        _status, installed = self._run_execution_owners.install_termination_intent(
            state.run_id,
            intent,
        )
        working_set = state.run_working_set
        control_owner = (
            working_set.model_call_control_owner if working_set is not None else None
        )
        activation = (
            working_set.run_execution_activation if working_set is not None else None
        )
        if installed is not None and control_owner is not None:
            if activation is None:
                raise RuntimeError("active model control owner lacks run activation")
            attribution_payload = {
                "schema_version": "run_termination_intent_attribution.v1",
                "intent_id": installed.intent_id,
                "kind": installed.kind,
                "requested_at_utc": installed.requested_at_utc,
                "requester_id": installed.requester_id,
                "target_run_execution_activation_fingerprint": (
                    activation.activation_fingerprint
                ),
            }
            attribution = RunTerminationIntentAttributionFact(
                **attribution_payload,
                attribution_fingerprint=sha256_fingerprint(
                    "run-termination-intent-attribution:v1",
                    attribution_payload,
                ),
            )
            await control_owner.install_termination_intent(attribution)
        return installed

    def _fold_run_owner_terminal(self, result: AgentRunResult) -> None:
        owner = self._run_execution_owners.get(result.state.run_id)
        if owner is None:
            return
        if not result.state.finalized:
            candidate = result.state.scratchpad.get("pending_run_end_candidate")
            if isinstance(candidate, RunEndEvent):
                owner.terminal_candidate = candidate
            owner.terminal_state = (
                "commit_outcome_unknown"
                if self.wiring.runtime_wiring.runtime_session.reconciliation_required
                else "candidate_frozen"
            )
            return
        stored = self.wiring.runtime_wiring.event_log.get_by_id(owner.terminal_event_id)
        if isinstance(stored, RunEndEvent):
            owner.terminal_candidate = stored
        owner.terminal_state = "confirmed"
        if not owner.run_completion.done():
            owner.run_completion.set_result(result)

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
        self._boundary_task = None
        self._boundary_attempt = None
        self._boundary_observer = None
        self._preparing_state = None
        self._preparing_identity = None
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
        runtime_session = self.wiring.runtime_wiring.runtime_session
        await self.drain_active_run(
            reason=reason, timeout_seconds=drain_timeout_seconds
        )
        await runtime_session.model_stream_execution_registry.drain_all(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await self._finalize_suspended_run(reason)
        await runtime_session.tool_execution_terminal_registry.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await runtime_session.transcript_projection_checkpoint_service.request_close_cancellation()
        await self.wiring.runtime_wiring.memory_governance_executor.flush_pending_event_outbox_async(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await runtime_session.event_write_service.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        await runtime_session.transcript_projection_checkpoint_service.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        window_compaction_service = runtime_session.window_compaction_service
        if window_compaction_service is not None:
            await window_compaction_service.drain_pending(
                deadline_monotonic=time.monotonic() + drain_timeout_seconds
            )
        manifest_service = runtime_session.context_input_manifest_service
        await manifest_service.drain_pending(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        compaction_service = self.wiring.runtime_wiring.compaction_service
        if compaction_service is not None:
            await compaction_service.drain_pending_terminalizations(
                timeout_seconds=drain_timeout_seconds
            )
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
        runtime_session.require_mutation_allowed()
        if self.mcp_supervisor is not None:
            await self.mcp_supervisor.aclose(timeout_seconds=drain_timeout_seconds)
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
        boundary_task = self._boundary_task
        boundary_observer = self._boundary_observer
        preparing_state = self._preparing_state
        if boundary_observer is not None:
            boundary_observer.detach()
        task = self._active_task
        state = self._active_state
        drain_state = state
        if task is not None and not task.done() and task is not asyncio.current_task():
            active_model_handles = 0
            if reason is not None and state is not None and state.stop_request is None:
                await self._install_run_termination_intent(state, reason)
                state.stop_request = StopRequest(reason=reason)
                self.stopping_run_id = state.run_id
                active_model_handles = await self.wiring.runtime_wiring.runtime_session.model_stream_execution_registry.request_cancel_run(
                    state.run_id,
                    reason=_model_stream_cancel_reason(reason),
                )
            if active_model_handles == 0:
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
            except asyncio.CancelledError:
                pass
            except TimeoutError as exc:
                raise HostSessionBusyError(
                    "active run did not drain before close deadline"
                ) from exc
            except Exception:
                pass
        if (
            boundary_task is not None
            and not boundary_task.done()
            and boundary_task is not asyncio.current_task()
        ):
            if (
                reason is not None
                and preparing_state is not None
                and preparing_state.stop_request is None
            ):
                preparing_state.stop_request = StopRequest(reason=reason)
                self.stopping_run_id = preparing_state.run_id
            boundary_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(boundary_task), timeout=timeout_seconds
                )
            except asyncio.CancelledError:
                pass
            except TimeoutError as exc:
                raise HostSessionBusyError(
                    "run boundary did not drain before close deadline"
                ) from exc
            except Exception:
                pass
        state = self._active_state or drain_state
        if state is not None:
            owner = self._run_execution_owners.get(state.run_id)
            if owner is not None and owner.terminal_state != "confirmed":
                try:
                    result = await asyncio.wait_for(
                        self.wiring.agent_runtime.retry_run_terminalization(state),
                        timeout=timeout_seconds,
                    )
                except BaseException:
                    # A durable RunStart cannot lose its only retry owner during
                    # close.  Propagate so HostCore preserves the session/lease.
                    raise
                self._fold_run_owner_terminal(result)
                if not state.finalized:
                    raise RuntimeError(
                        "active run terminalization drain ended without RunEnd"
                    )
                self._finish_active_run()
                owner = self._run_execution_owners.get(state.run_id)
            if owner is not None and owner.terminal_state == "confirmed":
                self._run_execution_owners.retire_confirmed(state.run_id)
                if self._run_execution_owners.get(state.run_id) is not None:
                    try:
                        await self._run_execution_owners.wait_until_retired(
                            state.run_id,
                            timeout_seconds=timeout_seconds,
                        )
                    except TimeoutError as exc:
                        raise HostSessionBusyError(
                            "run execution resources did not drain before close deadline"
                        ) from exc

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
        await self._install_run_termination_intent(state, reason)
        result = await self.wiring.agent_runtime.abort_run(state, reason=reason)
        self._fold_run_owner_terminal(result)
        if interaction_id is not None and self.mcp_supervisor is not None:
            self.mcp_supervisor.complete_pending_lease(interaction_id)
        if result.state.finalized:
            self._retire_confirmed_run_owner(state.run_id)
        self._suspended_state = None
        self.pending_interaction = None
        self.suspended_run_id = None

    def summary(self) -> dict[str, object]:
        installation = self.wiring.runtime_wiring.mcp_installation
        runtime_session = self.wiring.runtime_wiring.runtime_session
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
            "boundary": self._live_boundary_projection(),
            "context_input": {
                "candidate_lifecycle_cache": (
                    runtime_session.context_candidate_lifecycle_cache.stats()
                ),
                "cache_diagnostics": list(
                    runtime_session.context_input_cache_diagnostics()
                ),
            },
            "long_horizon": self._live_long_horizon_projection(),
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

    def _live_long_horizon_projection(self) -> dict[str, object] | None:
        state = self._active_state or self._suspended_state or self._preparing_state
        if state is None or state.run_working_set is None:
            return None
        runtime_session = self.wiring.runtime_wiring.runtime_session
        contract = state.run_working_set.long_horizon_contract
        store = runtime_session.long_horizon_state_store
        account = store.rollout_account(contract.rollout_account_id)
        rollout = store.rollout_state(contract.rollout_account_id)
        window_chain = store.window_state(state.run_id)
        if account is None or rollout is None or window_chain is None:
            return {
                "status": "unavailable",
                "diagnostic": "long_horizon_live_state_missing",
            }

        window_id = window_chain.active_window_id or (
            window_chain.ordered_window_ids[-1]
            if window_chain.ordered_window_ids
            else None
        )
        projection = (
            store.projection_state(window_id) if window_id is not None else None
        )
        from pulsara_agent.event import (
            ContextCompiledEvent,
            SubagentGraphCheckpointCommittedEvent,
        )
        from pulsara_agent.runtime.long_horizon.status import (
            derive_rollout_status_shadow,
        )

        event_slice = runtime_session.context_authority_slice_cache.latest_for_basis(
            runtime_session_id=self.runtime_session_id,
            basis_id=state.run_working_set.run_start_event_id,
        )
        try:
            if event_slice is None:
                raise RuntimeError("live authority slice has not been prepared")
            shadow = derive_rollout_status_shadow(
                event_slice=event_slice,
                account_id=account.account_id,
                policy=contract.rollout_status_hint_policy,
            )
        except Exception as exc:
            shadow_payload: dict[str, object] | None = None
            shadow_diagnostic = type(exc).__name__
        else:
            shadow_payload = shadow.model_dump(mode="json")
            shadow_diagnostic = None

        status_snapshot = runtime_session.event_log.read_raw_events_by_types(
            (
                EventType.CONTEXT_COMPILED.value,
                EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED.value,
            ),
            run_ids=(state.run_id,),
            max_events=512,
            max_payload_bytes=8 * 1024 * 1024,
        )
        status_events = tuple(
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for raw in status_snapshot.events
        )
        latest_context = next(
            (
                event
                for event in reversed(status_events)
                if isinstance(event, ContextCompiledEvent)
            ),
            None,
        )
        checkpoint = next(
            (
                event
                for event in reversed(status_events)
                if isinstance(event, SubagentGraphCheckpointCommittedEvent)
            ),
            None,
        )
        input_budget = (
            latest_context.budget.input_budget_tokens
            if latest_context is not None
            else state.run_model_target.context_budget.input_budget_tokens
            if state.run_model_target is not None
            else None
        )
        input_estimate = (
            latest_context.budget.final_payload_estimated_tokens
            if latest_context is not None
            else None
        )
        budget_decision = (
            latest_context.long_horizon_context_budget_decision
            if latest_context is not None
            else None
        )
        finalization_remaining = max(
            0,
            account.finalization_reserve_milliunits
            - rollout.finalization_agent_charged_milliunits
            - rollout.finalization_agent_reserved_milliunits
            - rollout.finalization_compaction_charged_milliunits
            - rollout.finalization_compaction_reserved_milliunits
            - rollout.finalization_tool_charged_milliunits
            - rollout.finalization_tool_reserved_milliunits,
        )
        exploration_ratio_ppm = (
            rollout.exploration_charged_milliunits * 1_000_000
        ) // account.exploration_allowance_milliunits
        return {
            "status": "available",
            "run_id": state.run_id,
            "window_id": window_id,
            "window_generation": len(window_chain.ordered_window_ids),
            "projection_generation": (
                projection.projection_generation if projection is not None else 0
            ),
            "input_estimated_tokens": input_estimate,
            "input_budget_tokens": input_budget,
            "tool_projection_tokens": (
                projection.total_projected_tokens if projection is not None else 0
            ),
            "tool_projection_soft_target_tokens": (
                budget_decision.soft_tool_projection_tokens
                if budget_decision is not None
                else None
            ),
            "rollout_phase": rollout.phase.value,
            "rollout_charged_milliunits": rollout.charged_milliunits,
            "rollout_total_milliunits": account.total_budget_milliunits,
            "exploration_consumption_ratio_ppm": exploration_ratio_ppm,
            "finalization_reserve_remaining_milliunits": finalization_remaining,
            "model_call_count": rollout.model_call_count,
            "tool_call_count": rollout.tool_call_count,
            "rollout_status_shadow": shadow_payload,
            "rollout_status_shadow_diagnostic": shadow_diagnostic,
            "subagent_graph_checkpoint": (
                {
                    "checkpoint_id": checkpoint.checkpoint.checkpoint_id,
                    "through_sequence": checkpoint.checkpoint.through_sequence,
                    "delta_event_count": max(
                        0,
                        runtime_session.long_horizon_state_store.through_sequence
                        - checkpoint.checkpoint.through_sequence,
                    ),
                }
                if checkpoint is not None
                else None
            ),
        }

    def _live_boundary_projection(self) -> dict[str, object]:
        state = self._active_state or self._suspended_state or self._preparing_state
        run_owner = (
            self._run_execution_owners.get(state.run_id) if state is not None else None
        )
        segment = run_owner.active_segment if run_owner is not None else None
        latest_boundary = (
            state.run_working_set.latest_committed_resume_boundary
            if state is not None and state.run_working_set is not None
            else None
        )
        initial_identity = (
            state.scratchpad.get("host_run_boundary_identity")
            if state is not None
            else None
        )
        boundary_task_live = (
            self._boundary_task is not None and not self._boundary_task.done()
        )
        boundary_attempt = self._boundary_attempt
        identity = (
            self._preparing_identity
            if boundary_task_live and self._preparing_identity is not None
            else latest_boundary.identity
            if isinstance(latest_boundary, InteractionResumeBoundaryFact)
            else initial_identity
            if isinstance(initial_identity, HostRunBoundaryIdentityFact)
            else None
        )
        if run_owner is not None:
            live_state = "committed"
            durable_existence = "full"
        elif boundary_task_live and boundary_attempt is not None:
            live_state = "preparing"
            confirmation = (
                self._boundary_batch_confirmation(boundary_attempt)
                if boundary_attempt.commit_state
                in {"commit_in_flight", "commit_outcome_unknown", "ledger_latched"}
                else None
            )
            durable_existence = (
                "full"
                if boundary_attempt.commit_state in {"committed", "publication_failed"}
                else "none"
                if confirmation is None
                or confirmation.status is BoundaryBatchCommitStatus.NONE
                else "full"
                if confirmation.status is BoundaryBatchCommitStatus.FULL
                else "partial_untrusted"
                if confirmation.status
                in {
                    BoundaryBatchCommitStatus.PARTIAL,
                    BoundaryBatchCommitStatus.CONFLICT,
                }
                else "unknown"
            )
        else:
            live_state = "idle"
            durable_existence = "none"
        observer = self._boundary_observer
        if observer is None:
            observer_state = "detached"
        elif not observer.attached:
            observer_state = "detached"
        elif observer.queue.full():
            observer_state = "backpressured"
        else:
            observer_state = "attached"
        compaction_snapshot = self.wiring.runtime_wiring.event_log.read_raw_events_by_types(
            (
                EventType.CONTEXT_COMPACTION_STARTED.value,
                EventType.CONTEXT_COMPACTION_COMPLETED.value,
                EventType.CONTEXT_COMPACTION_FAILED.value,
            ),
            max_events=4_096,
            max_payload_bytes=8 * 1024 * 1024,
            deadline_monotonic=time.monotonic() + 5.0,
        )
        compaction_events = tuple(
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for raw in compaction_snapshot.events
        )
        started = {
            event.id
            for event in compaction_events
            if isinstance(event, ContextCompactionStartedEvent)
        }
        terminal_started = {
            event.started_event_id
            for event in compaction_events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
            and event.started_event_id is not None
        }
        return {
            "state": live_state,
            "boundary_id": identity.boundary_id if identity is not None else None,
            "kind": identity.kind.value if identity is not None else None,
            "phase": (
                boundary_attempt.phase.value
                if boundary_task_live and boundary_attempt is not None
                else "activation"
                if segment is not None
                else "durable_commit"
                if run_owner is not None
                else "ingress"
                if boundary_task_live
                else None
            ),
            "draft_run_id": state.run_id if state is not None else None,
            "started_at_utc": (
                identity.observed_at_utc if identity is not None else None
            ),
            "candidate_event_ids": (
                list(boundary_attempt.candidate_event_ids)
                if boundary_task_live and boundary_attempt is not None
                else []
            ),
            "durable_run_existence": durable_existence,
            "pending_compaction_terminalization_count": len(
                started.difference(terminal_started)
            ),
            "observer_state": observer_state,
            "active_segment_id": segment.segment_id if segment is not None else None,
            "active_segment_generation": (
                segment.segment_generation if segment is not None else None
            ),
            "active_segment_owner_kind": (
                segment.activation_owner_kind if segment is not None else None
            ),
            "active_segment_owner_id": (
                segment.activation_owner_id if segment is not None else None
            ),
            "current_execution_handle_id": (
                run_owner.execution_handles.handle_id if run_owner is not None else None
            ),
            "retiring_execution_handle_count": (
                len(run_owner.retiring_execution_handles)
                if run_owner is not None
                else 0
            ),
        }

    # -- Internal execution primitive -----------------------------------------

    def _set_boundary_phase(self, phase: HostRunBoundaryPhase) -> None:
        attempt = self._boundary_attempt
        if attempt is not None:
            attempt.phase = phase

    def _set_boundary_candidates(self, events: tuple[AgentEvent, ...]) -> None:
        attempt = self._boundary_attempt
        if attempt is not None:
            attempt.candidate_events = events
            attempt.candidate_event_ids = tuple(event.id for event in events)
            attempt.candidate_payload_fingerprints = tuple(
                sha256_fingerprint(
                    "host-boundary-event-candidate:v1",
                    event.model_dump(mode="json", exclude={"sequence"}),
                )
                for event in events
            )

    def _set_boundary_commit_state(
        self,
        state: str,
    ) -> None:
        attempt = self._boundary_attempt
        if attempt is not None:
            attempt.commit_state = state  # type: ignore[assignment]

    def _set_boundary_commit_confirmation(
        self,
        status: BoundaryBatchCommitStatus,
        *,
        committed_events: tuple[AgentEvent, ...] = (),
    ) -> None:
        attempt = self._boundary_attempt
        if attempt is None:
            return
        sequences = tuple(
            event.sequence
            for event in committed_events
            if event.sequence is not None
        )
        attempt.commit_confirmation = BoundaryBatchConfirmation(
            status=status,
            candidate_event_ids=attempt.candidate_event_ids,
            committed_event_ids=tuple(event.id for event in committed_events),
            committed_sequences=sequences,
            actual_last_sequence=max(sequences, default=None),
        )

    def _boundary_batch_confirmation(
        self,
        attempt: HostRunBoundaryAttempt,
    ) -> BoundaryBatchConfirmation | None:
        if not attempt.candidate_event_ids:
            return None
        if attempt.commit_confirmation is not None:
            return attempt.commit_confirmation
        if attempt.commit_state == "not_started":
            return None
        # A projection/status read never performs recovery I/O. An attempt that
        # has not yet published its writer-owned result is conservatively unknown.
        return BoundaryBatchConfirmation(
            status=BoundaryBatchCommitStatus.UNKNOWN,
            candidate_event_ids=attempt.candidate_event_ids,
            committed_event_ids=(),
            committed_sequences=(),
            actual_last_sequence=None,
        )

    def _finish_boundary_attempt(
        self,
        attempt: HostRunBoundaryAttempt,
    ) -> HostRunBoundaryAttemptOutcome:
        owner = self._run_execution_owners.get(attempt.draft_run_id)
        if owner is None and attempt.execution_handles is not None:
            handles = attempt.execution_handles
            if handles.state == "attempt_owned":
                handles.mark_retiring()
            if handles.state == "retiring" and handles.borrow_tracker.can_retire():
                handles.mark_closed()
        confirmation = (
            self._boundary_batch_confirmation(attempt)
            if owner is None
            and attempt.candidate_events
            and attempt.commit_state != "not_started"
            else None
        )
        if owner is not None:
            durable_existence = DurableRunExistence.FULL
        elif (
            confirmation is None
            or confirmation.status is BoundaryBatchCommitStatus.NONE
        ):
            durable_existence = DurableRunExistence.NONE
        elif confirmation.status is BoundaryBatchCommitStatus.FULL:
            durable_existence = DurableRunExistence.FULL
        elif confirmation.status in {
            BoundaryBatchCommitStatus.PARTIAL,
            BoundaryBatchCommitStatus.CONFLICT,
        }:
            durable_existence = DurableRunExistence.PARTIAL_UNTRUSTED
        else:
            durable_existence = DurableRunExistence.UNKNOWN

        if durable_existence is DurableRunExistence.PARTIAL_UNTRUSTED:
            disposition = HostRunBoundaryDisposition.SESSION_LATCHED
        elif durable_existence is DurableRunExistence.UNKNOWN:
            disposition = HostRunBoundaryDisposition.COMMIT_OUTCOME_UNKNOWN
        elif attempt.commit_state == "publication_failed":
            disposition = HostRunBoundaryDisposition.COMMITTED_BUT_PUBLICATION_FAILED
        elif (
            owner is not None
            and owner.run_completion.done()
            and owner.run_completion.result().status is LoopStatus.FAILED
        ):
            disposition = HostRunBoundaryDisposition.COMMITTED_EXECUTION_FAILED
        else:
            disposition = HostRunBoundaryDisposition.PROCEED
        terminal_event_id = (
            owner.terminal_candidate.id
            if owner is not None and owner.terminal_candidate is not None
            else owner.terminal_event_id
            if owner is not None and owner.terminal_state == "confirmed"
            else None
        )
        outcome = HostRunBoundaryAttemptOutcome(
            boundary_id=attempt.boundary_id,
            disposition=disposition,
            commit_confirmation=confirmation,
            durable_run_existence=durable_existence,
            terminal_event_id=terminal_event_id,
            diagnostics=(),
        )
        if not attempt.completion.done():
            attempt.completion.set_result(outcome)
        if (
            owner is not None
            and owner.terminal_state == "confirmed"
            and owner.active_segment is None
        ):
            self._run_execution_owners.retire_confirmed(attempt.draft_run_id)
        return outcome

    def _finish_boundary_attempt_safely(
        self,
        attempt: HostRunBoundaryAttempt,
    ) -> HostRunBoundaryAttemptOutcome:
        """Resolve boundary completion even when confirmation itself fails.

        A stop/close waiter must never depend on a Future that only the failed
        confirmation path could complete.
        """

        try:
            return self._finish_boundary_attempt(attempt)
        except BaseException:
            self.wiring.runtime_wiring.runtime_session.latch_event_commit_outcome_unknown()
            outcome = HostRunBoundaryAttemptOutcome(
                boundary_id=attempt.boundary_id,
                disposition=HostRunBoundaryDisposition.COMMIT_OUTCOME_UNKNOWN,
                commit_confirmation=BoundaryBatchConfirmation(
                    status=BoundaryBatchCommitStatus.UNKNOWN,
                    candidate_event_ids=attempt.candidate_event_ids,
                    committed_event_ids=(),
                    committed_sequences=(),
                    actual_last_sequence=None,
                )
                if attempt.candidate_event_ids
                else None,
                durable_run_existence=DurableRunExistence.UNKNOWN,
                terminal_event_id=None,
                diagnostics=(),
            )
            if not attempt.completion.done():
                attempt.completion.set_result(outcome)
            attempt.commit_state = "ledger_latched"
            return outcome

    def _complete_boundary_attempt_after_activation(self) -> None:
        attempt = self._boundary_attempt
        if attempt is None:
            return
        attempt.phase = HostRunBoundaryPhase.ACTIVATION
        self._finish_boundary_attempt_safely(attempt)
        if self._boundary_attempt is attempt:
            self._boundary_attempt = None
            self._preparing_state = None
            self._preparing_identity = None

    def _create_owned_boundary_task(
        self,
        make_awaitable: Callable[[], Awaitable[Any]],
        *,
        preparing_state: LoopState | None,
        preparing_identity: HostRunBoundaryIdentityFact,
        observer: _StreamObserver | None = None,
    ) -> asyncio.Task[Any]:
        """Install PREPARING ownership before boundary code can execute."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "HostSession boundary APIs require a running event loop"
            ) from exc
        if self._boundary_task is not None and not self._boundary_task.done():
            raise HostSessionBusyError("host session already has a preparing boundary")
        activation_gate = asyncio.Event()

        async def _drive() -> Any:
            await activation_gate.wait()
            try:
                return await make_awaitable()
            finally:
                current = asyncio.current_task()
                if self._boundary_task is current:
                    attempt = self._boundary_attempt
                    if attempt is not None and attempt.owner_task is current:
                        self._finish_boundary_attempt_safely(attempt)
                        self._boundary_attempt = None
                    self._boundary_task = None
                    self._boundary_observer = None
                    self._preparing_state = None
                    self._preparing_identity = None

        coroutine = _drive()
        try:
            task = loop.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        self._boundary_task = task
        attempt = HostRunBoundaryAttempt(
            boundary_id=preparing_identity.boundary_id,
            kind=preparing_identity.kind,
            phase=HostRunBoundaryPhase.INGRESS,
            owner_task=task,
            draft_run_id=preparing_identity.run_id,
            execution_handles=None,
            candidate_events=(),
            candidate_event_ids=(),
            candidate_payload_fingerprints=(),
            commit_state="not_started",
            completion=loop.create_future(),
        )
        self._boundary_attempt = attempt
        self._boundary_observer = observer
        self._preparing_state = preparing_state
        self._preparing_identity = preparing_identity
        activation_gate.set()
        return task

    def _start_owned_boundary_stream(
        self,
        make_stream: Callable[[], AsyncIterator[AgentEvent]],
        *,
        preparing_state: LoopState | None,
        preparing_identity: HostRunBoundaryIdentityFact,
    ) -> AsyncIterator[AgentEvent]:
        """Start a Host-owned stream driver before returning its observer."""
        observer = _StreamObserver()

        async def _drive() -> None:
            async for event in make_stream():
                await observer.emit(event)

        task = self._create_owned_boundary_task(
            _drive,
            preparing_state=preparing_state,
            preparing_identity=preparing_identity,
            observer=observer,
        )
        return _OwnedBoundaryStreamObserver(
            self._observe_owned_boundary_stream(observer=observer, task=task),
            observer,
        )

    async def _observe_owned_boundary_stream(
        self,
        *,
        observer: _StreamObserver,
        task: asyncio.Task[None],
    ) -> AsyncIterator[AgentEvent]:
        try:
            while True:
                if task.done() and observer.queue.empty():
                    await asyncio.shield(task)
                    return
                item_task = asyncio.create_task(observer.queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        (item_task, task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if item_task in done:
                        item = item_task.result()
                        yield item
                        continue
                    item_task.cancel()
                    try:
                        await item_task
                    except asyncio.CancelledError:
                        pass
                    if observer.queue.empty():
                        await asyncio.shield(task)
                        return
                finally:
                    if not item_task.done():
                        item_task.cancel()
        finally:
            observer.detach()

    def _activate_committed_state(
        self,
        state: LoopState,
        committed: CommittedHostRunEntry,
    ) -> LoopState:
        if state.run_working_set is None:
            raise RuntimeError("ACTIVE transition requires a committed RunWorkingSet")
        if (
            state.run_working_set.run_start_event_id != committed.run_start_event.id
            or state.run_working_set.run_start_sequence != committed.run_start_sequence
        ):
            raise RuntimeError("ACTIVE transition run-entry identity mismatch")
        self._run_execution_owners.require(state.run_id)
        self._prepare_state_for_plan(state)
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    def _resume_active_state(self, pending: PendingInteraction) -> LoopState:
        state = self._require_suspended_state(pending)
        working_set = state.run_working_set
        if working_set is None or not isinstance(
            working_set.latest_committed_resume_boundary,
            InteractionResumeBoundaryFact,
        ):
            raise RuntimeError(
                "suspended run must commit a typed continuation boundary before activation"
            )
        if not isinstance(
            working_set.effective_exposure_plan, CapabilityExposurePlan
        ) or not isinstance(
            working_set.effective_exposure_fact, CapabilityExposureSnapshotFact
        ):
            raise RuntimeError(
                "suspended run must install a typed continuation exposure before activation"
            )
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    def _original_run_start_for_resume(self, state: LoopState) -> RunStartEvent:
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("suspended run lost its typed RunWorkingSet")
        started = self.wiring.runtime_wiring.event_log.get_by_id(
            working_set.run_start_event_id
        )
        if not isinstance(started, RunStartEvent) or started.sequence is None:
            raise RuntimeError(
                "suspended run requires exactly one sequenced durable RunStart"
            )
        if started.run_id != state.run_id:
            raise RuntimeError("suspended RunStart owner drifted")
        if started.run_entry_kind.value != "host":
            raise RuntimeError("suspended state does not match its Host RunStart")
        return started

    async def _prepare_and_commit_resume_boundary(
        self,
        *,
        pending: PendingInteraction,
        interaction_kind: str,
        identity: HostRunBoundaryIdentityFact,
    ) -> tuple[
        LoopState,
        CommittedInteractionResumeBoundary,
        tuple[AgentEvent, ...],
    ]:
        self._set_boundary_phase(HostRunBoundaryPhase.ADMISSION)
        state = self._require_suspended_state(pending)
        self._set_boundary_phase(HostRunBoundaryPhase.CONTRACT_RESOLUTION)
        started = self._original_run_start_for_resume(state)
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("suspended run lost its typed RunWorkingSet")
        rebound_target = self.wiring.agent_runtime.rebind_run_model_target(
            started.model_target
        )
        permission_snapshot = snapshot_from_run_start_event(
            started,
            runtime_session_id=self.runtime_session_id,
        )
        if state.permission_snapshot != permission_snapshot:
            raise RuntimeError(
                "suspended permission snapshot differs from durable RunStart"
            )
        if (
            state.run_model_target is not None
            and state.run_model_target.fact.target_fingerprint
            != rebound_target.fact.target_fingerprint
        ):
            raise RuntimeError("suspended model target differs from durable RunStart")

        original_basis = working_set.capability_resolve_basis
        source_plan = working_set.effective_exposure_plan
        source_fact = working_set.effective_exposure_fact
        source_event_ref = working_set.effective_exposure_event_ref
        suspended_token = state.scratchpad.get("suspended_state_token")
        if not isinstance(original_basis, CapabilityResolveBasis):
            raise RuntimeError("suspended run lost its original capability basis")
        if (
            not isinstance(source_plan, CapabilityExposurePlan)
            or not isinstance(source_fact, CapabilityExposureSnapshotFact)
            or source_event_ref is None
        ):
            raise RuntimeError("suspended run lost its source capability exposure")
        if not isinstance(suspended_token, str):
            raise RuntimeError("suspended run lost its ABA state token")

        self._set_boundary_phase(HostRunBoundaryPhase.MCP_REQUIRED_WAIT)
        await self._apply_mcp_safe_point(trigger="config_change")
        self._set_boundary_phase(HostRunBoundaryPhase.MCP_INSTALLATION)
        frozen_surface = (
            self.wiring.agent_runtime.capability_runtime.freeze_execution_surface(
                CapabilityExecutionSurfaceSnapshotContext(
                    workspace_root=self.workspace.workspace_root,
                    workspace_kind=self.workspace.workspace_kind,
                    available_tool_names=frozenset(
                        self.wiring.agent_runtime.tool_executor.registry.names()
                    ),
                    mcp_installation_id=(
                        self.wiring.runtime_wiring.mcp_installation.installation_id
                    ),
                ),
                tool_registry=self.wiring.agent_runtime.tool_executor.registry,
                archive=self.wiring.runtime_wiring.archive,
                runtime_session_id=self.runtime_session_id,
                owner_id=identity.boundary_id,
            )
        )
        owner = CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=identity.boundary_id,
            host_boundary_kind=identity.kind,
            runtime_session_id=identity.runtime_session_id,
            run_id=identity.run_id,
        )
        continuation_basis = derive_continuation_basis(
            original_basis,
            continuation_owner=owner,
            current_execution_surface=frozen_surface,
            basis_id=f"capability_basis:continuation:{uuid4().hex}",
        )
        exposure_id = f"capability_exposure:continuation:{uuid4().hex}"
        resolved = (
            self.wiring.agent_runtime.capability_runtime.resolve_continuation_exposure(
                CapabilityProjectionResolveContext(
                    workspace_root=continuation_basis.workspace_root,
                    workspace_kind=self.workspace.workspace_kind,
                    memory_domain=self.workspace.memory_domain,
                    user_input=continuation_basis.user_input,
                    prior_messages=continuation_basis.prior_messages,
                    active_skill_names=continuation_basis.active_skill_names,
                    plan_active=continuation_basis.fact.plan_active,
                ),
                frozen_surface=frozen_surface,
                original_plan=source_plan,
                original_fact=source_fact,
                archive=self.wiring.runtime_wiring.archive,
                runtime_session_id=self.runtime_session_id,
                owner=owner,
                resolve_basis=continuation_basis.fact,
                exposure_id=exposure_id,
            )
        )
        runtime_session = self.wiring.runtime_wiring.runtime_session
        event_context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        pending_audits = tuple(
            runtime_session.pending_mcp_installation_audit_events(event_context)
        )
        source_exposure_event = self.wiring.runtime_wiring.event_log.get_by_id(
            source_event_ref.event_id
        )
        if not isinstance(source_exposure_event, CapabilityExposureResolvedEvent):
            raise RuntimeError("resume source exposure is not durable")
        exposure_revision = source_exposure_event.exposure_revision + 1
        if exposure_revision < 2:
            raise RuntimeError("resume requires a durable initial capability exposure")
        exposure_event = CapabilityExposureResolvedEvent(
            **event_context.event_fields(),
            exposure=resolved.fact,
            exposure_revision=exposure_revision,
        )
        transition = (
            "reused"
            if resolved.fact.resolution_kind == "continuation_reused"
            else "narrowed"
        )
        boundary_fact = InteractionResumeBoundaryFact(
            identity=identity,
            original_run_start_event_id=started.id,
            original_run_start_sequence=started.sequence,
            interaction_id=(
                pending.approval_id
                if isinstance(pending, PendingApproval)
                else pending.interaction_id
            ),
            interaction_kind=interaction_kind,  # type: ignore[arg-type]
            suspended_state_token_fingerprint=sha256_fingerprint(
                "suspended-state-token:v1", suspended_token
            ),
            permission_snapshot_id=permission_snapshot.snapshot_id,
            model_target_fingerprint=rebound_target.fact.target_fingerprint,
            mcp_installation_id=frozen_surface.identity.mcp_installation_id,
            source_exposure_id=source_fact.exposure_id,
            source_exposure_semantic_fingerprint=(
                source_fact.exposure_semantic_fingerprint
            ),
            source_exposure_fact_fingerprint=source_fact.exposure_fact_fingerprint,
            effective_exposure_id=resolved.fact.exposure_id,
            effective_exposure_semantic_fingerprint=(
                resolved.fact.exposure_semantic_fingerprint
            ),
            effective_exposure_fact_fingerprint=(
                resolved.fact.exposure_fact_fingerprint
            ),
            exposure_transition=transition,
            committed_mcp_audit_event_ids=tuple(
                sorted(event.id for event in pending_audits)
            ),
        )
        boundary_event = RunInteractionResumeBoundaryEvent(
            **event_context.event_fields(),
            id=identity.boundary_id,
            boundary=boundary_fact,
        )
        run_owner = self._run_execution_owners.require(state.run_id)
        incoming_execution_handles = self._new_execution_handles(
            owner_id=identity.boundary_id,
            generation=run_owner.execution_handles.handle_generation + 1,
            frozen_execution_surface=frozen_surface,
        )
        prepared = PreparedInteractionResumeBoundary(
            identity=identity,
            interaction_id=boundary_fact.interaction_id,
            interaction_kind=interaction_kind,  # type: ignore[arg-type]
            suspended_state_token=suspended_token,
            original_run_start_event=started,
            rebound_model_target=rebound_target,
            permission_snapshot=permission_snapshot,
            mcp_installation_fact=self._mcp_installation_reference_fact(),
            owned_continuation_exposure_plan=resolved.plan,
            continuation_exposure_fact=resolved.fact,
            frozen_execution_surface=frozen_surface,
            incoming_execution_handles=incoming_execution_handles,
            pending_mcp_audits=pending_audits,
            gate_policy=resume_gate_policy_for(interaction_kind),  # type: ignore[arg-type]
            diagnostics=(),
        )
        attempt = self._boundary_attempt
        if attempt is not None:
            attempt.execution_handles = incoming_execution_handles
        candidates = (*pending_audits, exposure_event, boundary_event)
        self._set_boundary_candidates(candidates)
        self._set_boundary_phase(HostRunBoundaryPhase.DURABLE_COMMIT)
        self._set_boundary_commit_state("commit_in_flight")
        try:
            stored = tuple(await runtime_session.emit_many(candidates, state=state))
        except BaseException as exc:
            if isinstance(exc, EventPublicationAfterCommitError):
                self._set_boundary_commit_state("publication_failed")
                committed_events = tuple(exc.result.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=committed_events,
                )
            else:
                outcome = runtime_session.resolved_event_write_outcome(exc)
                if outcome.status != "full":
                    self._set_boundary_commit_confirmation(
                        BoundaryBatchCommitStatus.UNKNOWN
                        if outcome.status == "unknown"
                        else BoundaryBatchCommitStatus.NONE,
                    )
                    self._set_boundary_commit_state(
                        "commit_outcome_unknown"
                        if outcome.status == "unknown"
                        else "not_started"
                    )
                    raise
                self._set_boundary_commit_state("committed")
                committed_events = tuple(outcome.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=committed_events,
                )
            runtime_session.acknowledge_committed_mcp_installation_audits(
                committed_events
            )
            await self._fold_resume_boundary_or_terminalize(
                state=state,
                prepared=prepared,
                stored=committed_events,
                publication_status="failed_after_commit",
            )
            if isinstance(exc, asyncio.CancelledError):
                _clear_current_task_cancellation()
            if state.stop_request is not None:
                await self._install_run_termination_intent(
                    state, state.stop_request.reason
                )
                await self._terminalize_committed_run_after_boundary_failure(
                    state=state,
                    abort_reason=state.stop_request.reason,
                )
            else:
                await self._terminalize_committed_run_after_boundary_failure(
                    state=state,
                    stop_reason=(
                        RunStopReason.RUNTIME_PUBLICATION_FAILURE
                        if isinstance(exc, EventPublicationAfterCommitError)
                        else RunStopReason.RUNTIME_EXECUTION_ERROR
                    ),
                    error_message=(
                        "resume boundary failed after durable commit: "
                        f"{type(exc).__name__}"
                    ),
                )
            self.pending_interaction = None
            self._suspended_state = None
            self.suspended_run_id = None
            raise exc
        self._set_boundary_commit_confirmation(
            BoundaryBatchCommitStatus.FULL,
            committed_events=stored,
        )
        runtime_session.acknowledge_committed_mcp_installation_audits(stored)
        self._set_boundary_commit_state("committed")
        committed = await self._fold_resume_boundary_or_terminalize(
            state=state,
            prepared=prepared,
            stored=stored,
            publication_status="completed",
        )
        self._set_boundary_phase(HostRunBoundaryPhase.ACTIVATION)
        return state, committed, stored

    def _fold_committed_resume_boundary(
        self,
        *,
        state: LoopState,
        prepared: PreparedInteractionResumeBoundary,
        stored: tuple[AgentEvent, ...],
        publication_status: Literal["completed", "failed_after_commit", "unavailable"],
    ) -> CommittedInteractionResumeBoundary:
        exposure_event = next(
            (
                event
                for event in stored
                if isinstance(event, CapabilityExposureResolvedEvent)
                and event.id
                and event.exposure.exposure_id
                == prepared.continuation_exposure_fact.exposure_id
            ),
            None,
        )
        boundary_event = next(
            (
                event
                for event in stored
                if isinstance(event, RunInteractionResumeBoundaryEvent)
                and event.boundary.identity.boundary_id == prepared.identity.boundary_id
            ),
            None,
        )
        if (
            exposure_event is None
            or exposure_event.sequence is None
            or boundary_event is None
            or boundary_event.sequence is None
        ):
            raise RuntimeError("resume boundary batch was not fully committed")
        through_sequence = stored[-1].sequence
        if through_sequence is None:
            raise RuntimeError("resume boundary batch ended with an unsequenced event")
        run_owner = self._run_execution_owners.require(state.run_id)
        current_handles = run_owner.execution_handles
        activation_blocked = (
            run_owner.terminal_state != "open"
            or run_owner.termination_intent is not None
        )
        incoming = prepared.incoming_execution_handles
        if incoming.frozen_execution_surface is not prepared.frozen_execution_surface:
            raise RuntimeError(
                "committed resume execution surface drifted after freeze"
            )
        current_runtime_handles_unchanged = (
            current_handles.mcp_installation is incoming.mcp_installation
            and current_handles.capability_runtime is incoming.capability_runtime
            and current_handles.tool_registry is incoming.tool_registry
            and current_handles.frozen_execution_surface.identity
            == incoming.frozen_execution_surface.identity
        )
        if activation_blocked:
            incoming.mark_retiring()
            if incoming.borrow_tracker.can_retire():
                incoming.mark_closed()
            state.scratchpad["resume_activation_blocked"] = True
        elif current_runtime_handles_unchanged:
            incoming.mark_retiring()
            if incoming.borrow_tracker.can_retire():
                incoming.mark_closed()
            self._run_execution_owners.set_latest_activation_owner(
                state.run_id,
                owner_kind="host_resume_boundary",
                owner_id=boundary_event.id,
            )
        else:
            swap = self._run_execution_owners.swap_execution_handles_after_continuation_commit(
                state.run_id,
                expected_current_handle_id=current_handles.handle_id,
                incoming=incoming,
                committed_continuation_event_id=boundary_event.id,
            )
            if swap.status == "swap_skipped_terminating":
                incoming.mark_retiring()
                if incoming.borrow_tracker.can_retire():
                    incoming.mark_closed()
                state.scratchpad["resume_activation_blocked"] = True
                activation_blocked = True
            else:
                state.scratchpad["run_execution_handle_id"] = swap.current_handle_id
                state.scratchpad["capability_execution_borrow_authority"] = (
                    incoming.borrow_authority
                )
        if not activation_blocked:
            state.run_model_target = prepared.rebound_model_target
            state.permission_snapshot = prepared.permission_snapshot
            working_set = state.run_working_set
            if working_set is None:
                raise RuntimeError("committed continuation lost RunWorkingSet")
            working_set.install_continuation(
                run_model_target=prepared.rebound_model_target,
                permission_snapshot=prepared.permission_snapshot,
                plan=prepared.owned_continuation_exposure_plan,
                fact=prepared.continuation_exposure_fact,
                event_ref=event_reference_from_stored(
                    exposure_event,
                    runtime_session_id=(
                        self.wiring.runtime_wiring.runtime_session.runtime_session_id
                    ),
                ),
                boundary=boundary_event.boundary,
                boundary_ref=event_reference_from_stored(
                    boundary_event,
                    runtime_session_id=(
                        self.wiring.runtime_wiring.runtime_session.runtime_session_id
                    ),
                ),
                frozen_execution_surface=prepared.frozen_execution_surface,
                validated_suspended_state_token_fingerprint=(
                    boundary_event.boundary.suspended_state_token_fingerprint
                ),
            )
        state.scratchpad.pop("suspended_state_token", None)
        return CommittedInteractionResumeBoundary(
            prepared=prepared,
            exposure_event_id=exposure_event.id,
            exposure_event_sequence=exposure_event.sequence,
            boundary_event_id=boundary_event.id,
            boundary_event_sequence=boundary_event.sequence,
            committed_audit_event_ids=tuple(
                event.id
                for event in stored
                if event.id in {audit.id for audit in prepared.pending_mcp_audits}
            ),
            committed_through_sequence=through_sequence,
            publication_status=publication_status,
        )

    async def _fold_resume_boundary_or_terminalize(
        self,
        *,
        state: LoopState,
        prepared: PreparedInteractionResumeBoundary,
        stored: tuple[AgentEvent, ...],
        publication_status: Literal["completed", "failed_after_commit", "unavailable"],
    ) -> CommittedInteractionResumeBoundary:
        try:
            return self._fold_committed_resume_boundary(
                state=state,
                prepared=prepared,
                stored=stored,
                publication_status=publication_status,
            )
        except BaseException as fold_error:
            if isinstance(fold_error, asyncio.CancelledError):
                _clear_current_task_cancellation()
            try:
                await self._terminalize_committed_run_after_boundary_failure(
                    state=state,
                    stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                    error_message=(
                        f"committed resume fold failed: {type(fold_error).__name__}"
                    ),
                )
            except BaseException:
                self.wiring.runtime_wiring.runtime_session.latch_event_commit_outcome_unknown()
                raise
            raise fold_error

    async def _run_owned(
        self,
        state: LoopState,
        make_result: Callable[[], Awaitable[AgentRunResult]],
    ) -> AgentRunResult:
        async def _drive() -> AgentRunResult:
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

        async def _drive_and_finalize() -> AgentRunResult:
            installed = self._run_execution_owners.require(state.run_id).active_segment
            if installed is None:
                raise RuntimeError("run driver started before segment ownership")
            self._install_working_set_activation(state, installed)
            result: AgentRunResult | None = None
            try:
                result = await _drive()
                return result
            finally:
                folded_result = result or self.wiring.agent_runtime._run_result(state)
                working_set = state.run_working_set
                if (
                    working_set is not None
                    and working_set.model_call_control_owner is not None
                ):
                    await working_set.model_call_control_owner.retire()
                    working_set.model_call_control_owner = None
                self._run_execution_owners.complete_segment(
                    state.run_id,
                    segment_id=installed.segment_id,
                    segment_generation=installed.segment_generation,
                    result=RunExecutionSegmentResult(
                        segment_id=installed.segment_id,
                        segment_generation=installed.segment_generation,
                        disposition=(
                            "waiting_user"
                            if folded_result.status is LoopStatus.WAITING_USER
                            else "run_terminal"
                            if folded_result.state.finalized
                            else "terminalization_pending"
                        ),
                        run_result=folded_result,
                    ),
                )
                if folded_result.status is not LoopStatus.WAITING_USER:
                    self._fold_run_owner_terminal(folded_result)
                    if folded_result.state.finalized:
                        self._finish_active_run()
                else:
                    self._finish_active_run()

        owner = self._run_execution_owners.require(state.run_id)
        activation_kind = (
            "interaction_resume"
            if owner.latest_activation_owner_kind == "host_resume_boundary"
            else "initial"
        )
        segment = self._run_execution_owners.install_segment(
            state.run_id,
            activation_kind=activation_kind,
            activation_owner_kind=owner.latest_activation_owner_kind,
            activation_owner_id=owner.latest_activation_owner_id,
            driver_factory=_drive_and_finalize,
            observer=None,
        )
        if isinstance(segment, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {segment.reason}"
            )
        task = segment.driver_task
        if task is None:
            raise RuntimeError("installed run segment has no driver task")
        self._active_task = None if task.done() else task
        return await asyncio.shield(task)

    async def _stream_events_in_boundary_driver(
        self,
        state: LoopState,
        make_stream: Callable[[], AsyncIterator[AgentEvent]],
    ) -> AsyncIterator[AgentEvent]:
        boundary_task = asyncio.current_task()
        if boundary_task is None or boundary_task is not self._boundary_task:
            raise RuntimeError("stream execution must run in the Host boundary driver")
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1)

        async def _drive_segment() -> AgentRunResult:
            installed = self._run_execution_owners.require(state.run_id).active_segment
            if installed is None:
                raise RuntimeError("stream driver started before segment ownership")
            self._install_working_set_activation(state, installed)
            try:
                async for event in make_stream():
                    await queue.put(event)
            except asyncio.CancelledError:
                request = state.stop_request
                if request is None:
                    raise
                _clear_current_task_cancellation()
                async for event in self.wiring.agent_runtime.stream_abort_run(
                    state, reason=request.reason
                ):
                    await queue.put(event)
            self._capture_pending_interaction(state)
            self._clear_plan_entry_audit_if_emitted(state)
            return self.wiring.agent_runtime._run_result(state)

        owner = self._run_execution_owners.require(state.run_id)
        activation_kind = (
            "interaction_resume"
            if owner.latest_activation_owner_kind == "host_resume_boundary"
            else "initial"
        )
        segment = self._run_execution_owners.install_segment(
            state.run_id,
            activation_kind=activation_kind,
            activation_owner_kind=owner.latest_activation_owner_kind,
            activation_owner_id=owner.latest_activation_owner_id,
            driver_factory=_drive_segment,
            observer=None,
        )
        if isinstance(segment, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {segment.reason}"
            )
        task = segment.driver_task
        if task is None:
            raise RuntimeError("installed streaming segment has no driver task")
        self._active_task = task
        result: AgentRunResult | None = None
        try:
            while True:
                if task.done() and queue.empty():
                    result = await task
                    break
                item_task = asyncio.create_task(queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        (item_task, task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if item_task in done:
                        yield item_task.result()
                        continue
                    item_task.cancel()
                    try:
                        await item_task
                    except asyncio.CancelledError:
                        pass
                    if queue.empty():
                        result = await task
                        break
                finally:
                    if not item_task.done():
                        item_task.cancel()
        finally:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            folded_result = result or self.wiring.agent_runtime._run_result(state)
            working_set = state.run_working_set
            if (
                working_set is not None
                and working_set.model_call_control_owner is not None
            ):
                await working_set.model_call_control_owner.retire()
                working_set.model_call_control_owner = None
            self._run_execution_owners.complete_segment(
                state.run_id,
                segment_id=segment.segment_id,
                segment_generation=segment.segment_generation,
                result=RunExecutionSegmentResult(
                    segment_id=segment.segment_id,
                    segment_generation=segment.segment_generation,
                    disposition=(
                        "waiting_user"
                        if folded_result.status is LoopStatus.WAITING_USER
                        else "run_terminal"
                        if folded_result.state.finalized
                        else "terminalization_pending"
                    ),
                    run_result=folded_result,
                ),
            )
            if folded_result.status is not LoopStatus.WAITING_USER:
                self._fold_run_owner_terminal(folded_result)
                if folded_result.state.finalized:
                    self._finish_active_run()
            else:
                self._finish_active_run()

    @staticmethod
    def _install_working_set_activation(
        state: LoopState,
        segment: RunExecutionSegmentOwner,
    ) -> None:
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("run segment activation requires a working set")
        payload = {
            "schema_version": "run_execution_activation.v1",
            "activation_owner_kind": segment.activation_owner_kind,
            "activation_owner_id": segment.activation_owner_id,
            "segment_generation": segment.segment_generation,
        }
        activation = RunExecutionActivationFact(
            **payload,
            activation_fingerprint=sha256_fingerprint(
                "run-execution-activation:v1", payload
            ),
        )
        current = working_set.run_execution_activation
        if working_set.model_call_control_owner is not None:
            raise RuntimeError("run working-set segment already has a control owner")
        if current is not None and current != activation:
            if activation.segment_generation <= current.segment_generation:
                raise RuntimeError("run working-set activation generation regressed")
            if working_set.process_segment_id == segment.segment_id:
                raise RuntimeError("run segment identity reused with a new activation")
        working_set.run_execution_activation = activation
        working_set.process_segment_id = segment.segment_id
        working_set.model_call_control_owner = RunModelCallControlOwner(
            run_id=state.run_id,
            activation=activation,
            segment_id=segment.segment_id,
            segment_generation=segment.segment_generation,
        )

    def _finish_active_run(self) -> None:
        run_id = (
            self._active_state.run_id
            if self._active_state is not None
            else self.active_run_id
        )
        if run_id is not None:
            self._retire_confirmed_run_owner(run_id)
        self._notify_governance()
        self._active_task = None
        self._active_state = None
        self.active_run_id = None
        self.stopping_run_id = None
        self.last_active_at = time.monotonic()

    def _retire_confirmed_run_owner(self, run_id: str) -> None:
        owner = self._run_execution_owners.get(run_id)
        if owner is not None and owner.terminal_state == "confirmed":
            self._run_execution_owners.retire_confirmed(run_id)

    async def _prepare_prior_messages_for_turn(
        self,
        user_input: str,
        *,
        target_model_target,
        host_boundary_id: str,
    ):
        transcript = await self._bounded_prior_transcript()
        prior_messages = list(transcript.messages)
        terminal_event: AgentEvent | None = None
        service = self.wiring.runtime_wiring.compaction_service
        if service is not None:
            compacted, terminal_event = await self._compact_if_needed_and_notify(
                service,
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=user_input,
                model_visible_messages_before=prior_messages,
                reason="preflight_context_threshold",
                host_boundary_id=host_boundary_id,
                host_boundary_kind="pre_run",
            )
            if compacted:
                transcript = await self._bounded_prior_transcript()
                prior_messages = list(transcript.messages)
        return (
            prior_messages,
            terminal_event,
            transcript.source_through_sequence,
            transcript.source_event_count,
            transcript.checkpoint_event,
        )

    async def _bounded_prior_transcript(self):
        runtime_wiring = self.wiring.runtime_wiring
        deadline = time.monotonic() + 30.0
        return await runtime_wiring.runtime_session.context_input_io_service.execute(
            operation_name="host-pre-run-transcript-projection",
            operation=lambda: rebuild_prior_messages_bounded(
                runtime_wiring.event_log,
                archive=runtime_wiring.archive,
                session_id=runtime_wiring.runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    async def _compact_if_needed_and_notify(
        self, service, **kwargs
    ) -> tuple[bool, AgentEvent | None]:
        event_log = self.wiring.runtime_wiring.event_log
        before_sequence = await asyncio.to_thread(event_log.next_sequence)
        compacted = await service.compact_if_needed(**kwargs)
        compaction_events = await self._publish_compaction_events_after(before_sequence)
        terminal_event = self._latest_terminal_compaction_event(compaction_events)
        if terminal_event is not None:
            self._notify_compaction_listeners(terminal_event)
        return compacted, terminal_event

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
        event_log = self.wiring.runtime_wiring.event_log
        through_sequence = event_log.next_sequence() - 1
        if through_sequence <= after_sequence:
            return []
        snapshot = event_log.read_raw_range_snapshot(
            minimum_sequence=after_sequence + 1,
            through_sequence=through_sequence,
            max_events=4_096,
            max_payload_bytes=16 * 1024 * 1024,
            deadline_monotonic=time.monotonic() + 10.0,
        )
        return [
            event
            for raw in snapshot.events
            if isinstance(
                event := raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
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
            # Process-local ABA token: stable for retries of this pending
            # interaction, rotated only after a committed continuation consumes it.
            if not isinstance(state.scratchpad.get("suspended_state_token"), str):
                state.scratchpad["suspended_state_token"] = (
                    f"suspended_state:{uuid4().hex}"
                )
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
        state.scratchpad.pop("suspended_state_token", None)

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
            runtime_session = self.wiring.runtime_wiring.runtime_session
            defer_entry_audit = (
                not runtime_session.allow_unbootstrapped_test_events
                and runtime_session.materialization_account_store.snapshot() is None
                and runtime_session.event_log.next_sequence() == 1
            )
            self.plan_state.begin(
                source="user",
                previous_mode=self.default_permission_mode,
                previous_policy=self.default_permission_policy(),
                reason=reason,
                pending_entry_audit=defer_entry_audit,
            )
            if not defer_entry_audit:
                self._emit_user_plan_mode_entered(reason=reason)
        policy = preset_to_policy(PermissionMode.READ_ONLY)
        self.last_active_at = time.monotonic()
        return policy

    def _emit_user_plan_mode_entered(self, *, reason: str = "") -> AgentEvent:
        suffix = uuid4().hex
        stored = self.wiring.runtime_wiring.runtime_session.emit_from_thread(
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
        self.plan_state.apply_durable_event(stored)
        return stored

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
        state: LoopState | None,
        *,
        source: str,
        exit_request_id: str | None = None,
        event_context: EventContext | None = None,
        transition_owner: str,
        host_workflow_operation_id: str | None,
    ) -> None:
        restored_mode = self.plan_state.pre_plan_permission_mode
        restored_policy = self._pre_plan_policy()
        restored_mode_value = parse_permission_mode(restored_mode).value
        context = event_context or (
            EventContext(
                run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id
            )
            if state is not None
            else None
        )
        if context is None:
            raise RuntimeError("plan mode exit requires event attribution")
        stored_exit = await self.wiring.runtime_wiring.runtime_session.emit(
            PlanModeExitedEvent(
                **context.event_fields(),
                source=source,  # type: ignore[arg-type]
                exit_request_id=exit_request_id,
                restored_permission_mode=restored_mode_value,
                restored_permission_policy=restored_policy.to_dict(),
                transition_owner=transition_owner,  # type: ignore[arg-type]
                host_workflow_operation_id=host_workflow_operation_id,
            ),
            state=state,
        )
        self.plan_state.apply_durable_event(stored_exit)

    def _raise_if_not_open(self, action: str) -> None:
        if self._lifecycle is not HostSessionLifecycle.OPEN:
            raise RuntimeError(f"host session is closed; cannot {action}")
        if self._mcp_installation_faulted:
            raise RuntimeError(
                "MCP installation commit faulted; only inspect/status/close are allowed"
            )

    def _require_new_run_admission(self, action: str) -> None:
        """Authoritative lock-held PRE_RUN admission recheck."""

        self._raise_if_not_open(action)
        self.wiring.runtime_wiring.runtime_session.require_mutation_allowed()
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction(action)
        task = self._active_task
        if self.active_run_id is not None or (task is not None and not task.done()):
            raise HostSessionBusyError("host session already has an active run")

    def _require_resume_admission(
        self,
        *,
        interaction_id: str,
        interaction_kind: str,
    ) -> None:
        """Authoritative lock-held PRE_INTERACTION_RESUME identity recheck."""

        self._raise_if_not_open("resolving a pending interaction")
        self.wiring.runtime_wiring.runtime_session.require_mutation_allowed()
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        task = self._active_task
        if self.active_run_id is not None or (task is not None and not task.done()):
            raise HostSessionBusyError("host session already has an active run")
        pending = self.pending_interaction
        if pending is None:
            raise HostSessionPendingInteractionError(
                "host session no longer has a pending interaction"
            )
        expected_type = {
            "approval": PendingApproval,
            "plan": PendingPlanInteraction,
            "mcp_input_required": PendingMcpInputRequired,
        }[interaction_kind]
        if not isinstance(pending, expected_type):
            raise HostSessionPendingInteractionError(
                "pending interaction kind changed while waiting for admission"
            )
        current_interaction_id = (
            pending.approval_id
            if isinstance(pending, PendingApproval)
            else pending.interaction_id
        )
        if current_interaction_id != interaction_id:
            raise HostSessionPendingInteractionError(
                "pending interaction identity changed while waiting for admission"
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
        boundary_task = self._boundary_task
        if (
            self._run_lock.locked()
            or (task is not None and not task.done())
            or (boundary_task is not None and not boundary_task.done())
        ):
            raise HostSessionBusyError("host session already has an active run")


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()


def _model_stream_cancel_reason(
    reason: AbortKind,
) -> Literal["user_stop", "host_teardown"]:
    if reason is AbortKind.USER_STOP:
        return "user_stop"
    return "host_teardown"
