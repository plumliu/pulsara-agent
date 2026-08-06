"""Long-lived conversation session wrapper around AgentRuntime."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

import asyncio
from hashlib import sha256
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    ContextWindowOpenedEvent,
    EventContext,
    EventType,
    McpCapabilitySnapshotInstalledEvent,
    McpInputRequiredResumeFailedEvent,
    McpInputRequiredResolutionSubmittedEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ProviderInputAppendCommittedEvent,
    PromptQueueCommittedToProviderInputEvent,
    PromptQueueReservationInstalledEvent,
    UserSteerCommittedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    RolloutBudgetAccountOpenedEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessObservationDeliveryDeferredEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
    ToolExecutionSuspendedEvent,
    utc_now,
)
from pulsara_agent.host.run_boundary import (
    CapabilityResolveBasis,
    HostBoundaryStopResult,
    HostBoundaryStopUncertain,
    HostBoundaryStoppedBeforeCommit,
    HostRunBoundaryAttempt,
    HostRunBoundaryAttemptOutcome,
    NewRunBoundaryInput,
    PreparedNewRunBoundary,
    PreparedNewRunBoundaryAuthority,
    QueuedPromptRunDelivery,
    derive_continuation_basis,
)
from pulsara_agent.runtime.run_execution.continuation import (
    CommittedInteractionResumeBoundary,
    PreparedInteractionResumeBoundary,
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
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredResolutionAttemptFact,
    McpInputRequiredSourceAuthorityFact,
    build_runtime_event_deadline_budget,
)
from pulsara_agent.ports.mcp import PreparedMcpInputRequiredResolution
from pulsara_agent.ports.mcp_secret import McpElicitationAction
from pulsara_agent.primitives.capability import build_capability_resolve_basis
from pulsara_agent.primitives.model_call import (
    ModelCallControlDisposition,
    sha256_fingerprint,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.user_carrier import encode_human_input, encode_runtime_request
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.host.ingress import (
    HostIngressAttemptOwner,
    HostIngressCapacityError,
    HostIngressClosedError,
    HostIngressCoordinator,
    default_permission_policy_fingerprint,
)
from pulsara_agent.ports.host_ingress import (
    ActiveRunMonitorSafePointLease,
    ActiveRunPromptSteerSafePointLease,
    HostIngressAdmissionStale,
)
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
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.host_ingress import (
    HostIngressItemPlacementFact,
    HostRunIngressAttributionFact,
    HostRunIngressSemanticFact,
    HumanRunIngressFact,
    RuntimeRequestRunIngressFact,
)
from pulsara_agent.primitives.terminal_observation import TerminalAutonomousDeliveryFact
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.transcript import rebuild_prior_messages_bounded
from pulsara_agent.message import SystemMsg
from pulsara_agent.ports.model_lifecycle import (
    BackgroundModelCallAdmissionLeaseIdentity,
    BackgroundModelCallAdmissionProof,
    CompactionMemoryExtractionSessionDriverHandle,
    DriverRegistrationLease,
)
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    PendingApproval,
)
from pulsara_agent.runtime.authority_materialization import RunSeedSourceStale
from pulsara_agent.runtime.agent import (
    AgentRunResult,
    agent_run_result_from_terminal_outcome,
)
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.terminal_presentation.history_capacity import (
    presentation_run_growth_source_fingerprint,
)
from pulsara_agent.ports.run_execution import (
    RunActivationOutcome,
    RunReconciliationRequired,
    RunSegmentInstallBlocked,
    RunSuspendedOutcome,
    RunTerminalOutcome,
    RunTerminalOutputPending,
    RunTerminalizationPending,
    RunTerminationIntent,
)
from pulsara_agent.runtime.run_execution.interaction_transition import (
    InteractionTransitionCommitReceipt,
    InteractionTransitionNotCommitted,
    InteractionTransitionReconciliationRequired,
    PreparedInteractionResumeAttempt,
    RuntimeInteractionTransitionService,
)
from pulsara_agent.ports.interaction_transition import (
    InteractionTransitionFull,
    InteractionTransitionNone,
    InteractionTransitionUntrusted,
)
from pulsara_agent.runtime.run_execution.service import (
    RunActivationDispatch,
    RunActivationService,
)
from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner
from pulsara_agent.ports.run_execution import (
    PreparedRunOwnerReservationKey,
    build_prepared_run_owner_reservation_key,
)
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
    reduce_plan_workflow_state,
)
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CommittedHostRunEntry,
)
from pulsara_agent.runtime.session import EventPublicationAfterCommitError
from pulsara_agent.runtime.terminal.notification import (
    TerminalNotificationAdmissionStale,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
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
from pulsara_agent.runtime.terminal.ui_stream import TerminalMonitorUISubscription
from pulsara_agent.runtime.wiring import AgentRuntimeWiring
from pulsara_agent.capability.providers.mcp import McpCapabilityProvider
from pulsara_agent.runtime.mcp.installation import build_mcp_installation
from pulsara_agent.capability.runtime import (
    CapabilityRuntime,
    FrozenCapabilityExecutionSurface,
)
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionInvocationFailed,
    ContextCompactionPublicationFailedAfterCommit,
)


HostActivationResult: TypeAlias = (
    AgentRunResult
    | RunSuspendedOutcome
    | RunTerminalizationPending
    | RunTerminalOutputPending
    | RunReconciliationRequired
)


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


class _HostBackgroundModelCallAdmissionLease:
    """Borrower-scoped idle-Host reservation held through ModelCallStart FULL."""

    def __init__(
        self,
        *,
        identity: BackgroundModelCallAdmissionLeaseIdentity,
        proof: BackgroundModelCallAdmissionProof,
        validator: Callable[[BackgroundModelCallAdmissionProof], None],
        releaser: Callable[[BackgroundModelCallAdmissionLeaseIdentity], None],
    ) -> None:
        self._identity = identity
        self._proof = proof
        self._validator = validator
        self._releaser = releaser
        self._state: Literal[
            "issued",
            "in_flight",
            "consumed",
            "released",
            "reconciliation_required",
        ] = "issued"
        self._resolved_model_call_id: str | None = None
        self._released_lock = False
        self._lock = RLock()

    @property
    def identity(self) -> BackgroundModelCallAdmissionLeaseIdentity:
        return self._identity

    @property
    def proof(self) -> BackgroundModelCallAdmissionProof:
        return self._proof

    @property
    def state(
        self,
    ) -> Literal[
        "issued",
        "in_flight",
        "consumed",
        "released",
        "reconciliation_required",
    ]:
        with self._lock:
            return self._state

    def begin_model_start(self) -> None:
        with self._lock:
            if self._state != "issued":
                raise RuntimeError("background model admission is not issuable")
            self._validator(self._proof)
            self._state = "in_flight"

    def validate_model_start(self, *, resolved_model_call_id: str) -> None:
        with self._lock:
            if self._state != "in_flight":
                raise RuntimeError("background model admission is not in flight")
            if self._resolved_model_call_id not in {None, resolved_model_call_id}:
                raise RuntimeError("background model-call identity drifted")
            self._validator(self._proof)
            self._resolved_model_call_id = resolved_model_call_id

    def confirm_model_start_full(self) -> None:
        with self._lock:
            if self._state == "consumed":
                return
            if self._state != "in_flight":
                raise RuntimeError("background model admission cannot confirm FULL")
            self._state = "consumed"
        self._release_host_lock_once()

    def mark_reconciliation_required(self) -> None:
        with self._lock:
            if self._state in {"consumed", "released"}:
                return
            self._state = "reconciliation_required"
        self._release_host_lock_once()

    def release(self) -> None:
        with self._lock:
            if self._state not in {"consumed", "reconciliation_required"}:
                self._state = "released"
        self._release_host_lock_once()

    def _release_host_lock_once(self) -> None:
        with self._lock:
            if self._released_lock:
                return
            self._released_lock = True
        self._releaser(self._identity)


_STREAM_QUEUE_MAX_ITEMS = 128


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
        authority = snapshot.authority
        protocol_semantic = (
            authority.surface_semantic.protocol_semantic
            if authority is not None
            else None
        )
        negotiation = (
            authority.discovery_attribution.negotiation
            if authority is not None
            else None
        )
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
                protocol_behavior_era=(
                    protocol_semantic.behavior_era.value
                    if protocol_semantic is not None
                    else None
                ),
                negotiation_wire_receipt_fingerprint=(
                    negotiation.negotiation_wire_receipt_fingerprint
                    if negotiation is not None
                    else None
                ),
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
        total_installed_tool_count=len(new.ordered_binding_installations),
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


def _consume_background_task_outcome(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


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
    reopen_deadline_monotonic: float | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    plan_state: PlanWorkflowState = field(default_factory=PlanWorkflowState)
    _lifecycle: HostSessionLifecycle = field(default=HostSessionLifecycle.OPEN)
    _boundary_attempt: HostRunBoundaryAttempt | None = None
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
    _run_activation_service: RunActivationService = field(init=False, repr=False)
    _interaction_transition_port: RuntimeInteractionTransitionService = field(
        init=False, repr=False
    )
    _ingress_coordinator: HostIngressCoordinator = field(init=False, repr=False)
    _host_event_loop: asyncio.AbstractEventLoop | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _terminal_notification_dispatch_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _terminal_notification_dispatch_error: str | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _terminal_notification_dispatch_enabled: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _active_run_monitor_safe_point_lease: ActiveRunMonitorSafePointLease | None = field(
        default=None, init=False, repr=False
    )
    _active_run_prompt_steer_safe_point_lease: (
        ActiveRunPromptSteerSafePointLease | None
    ) = field(default=None, init=False, repr=False)
    _stop_intent_revision: int = field(default=0, init=False, repr=False)
    _termination_intent_revision: int = field(default=0, init=False, repr=False)
    _background_model_call_admission_generation: int = field(
        default=0, init=False, repr=False
    )
    _background_model_call_admission_lease: (
        _HostBackgroundModelCallAdmissionLease | None
    ) = field(default=None, init=False, repr=False)
    _compaction_memory_extraction_driver: (
        CompactionMemoryExtractionSessionDriverHandle | None
    ) = field(default=None, init=False, repr=False)
    _compaction_memory_extraction_registration: DriverRegistrationLease | None = field(
        default=None, init=False, repr=False
    )
    _host_open_deadline_monotonic: float = field(init=False, repr=False)
    _subagent_dangling_repair_done: bool = field(default=False, init=False, repr=False)
    _terminal_application_services: Any = field(default=None, init=False, repr=False)
    _presentation_history_run_reservations: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        runtime_session = self.wiring.runtime_wiring.runtime_session
        activation_service = self.wiring.run_activation_service
        if activation_service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        self._run_activation_service = activation_service
        self._interaction_transition_port = (
            activation_service.build_interaction_transition_service(
                runtime_session_id=runtime_session.runtime_session_id,
                commit_resume_boundary=self._commit_interaction_transition_attempt,
                classify_write_failure=self._classify_interaction_transition_failure,
            )
        )
        runtime_deadline = runtime_session.runtime_open_deadline_monotonic
        self._host_open_deadline_monotonic = (
            runtime_deadline
            if self.reopen_deadline_monotonic is None
            else self.reopen_deadline_monotonic
        )
        if self._host_open_deadline_monotonic != runtime_deadline:
            raise ValueError("Host and RuntimeSession reopen deadlines diverged")
        if self._host_open_deadline_monotonic <= time.monotonic():
            raise TimeoutError(
                "Host open deadline expired before HostSession bootstrap"
            )
        self._ingress_coordinator = HostIngressCoordinator(
            host_session_id=self.host_session_id,
            permission_policy_fingerprint=default_permission_policy_fingerprint(),
        )
        from pulsara_agent.runtime.terminal_application.services import (
            TerminalApplicationServices,
        )

        self._terminal_application_services = TerminalApplicationServices(
            host_session=self
        )
        self.wiring.runtime_wiring.runtime_session.bind_host_ingress_commit_validator(
            self._ingress_coordinator.validate_run_start_event
        )
        self.wiring.runtime_wiring.runtime_session.bind_host_ingress_commit_guard(
            self._ingress_coordinator.run_start_commit_guard
        )
        self.wiring.runtime_wiring.runtime_session.bind_terminal_notification_listener(
            self._on_terminal_notification_committed
        )
        self.wiring.runtime_wiring.runtime_session.bind_active_run_monitor_safe_point(
            provider=self._borrow_active_run_monitor_safe_point,
            validator=self._validate_active_run_monitor_safe_point,
            releaser=self._release_active_run_monitor_safe_point,
            commit_guard_factory=(self._active_run_monitor_safe_point_commit_guard),
        )
        self.wiring.runtime_wiring.runtime_session.bind_active_run_prompt_steer_safe_point(
            provider=self._borrow_active_run_prompt_steer_safe_point,
            validator=self._validate_active_run_prompt_steer_safe_point,
            releaser=self._release_active_run_prompt_steer_safe_point,
            commit_guard_factory=self._active_run_prompt_steer_commit_guard,
        )
        snapshot = self.wiring.runtime_wiring.event_log.read_raw_events_by_types(
            (EventType.PLAN_MODE_ENTERED.value, EventType.PLAN_MODE_EXITED.value),
            max_events=4_096,
            max_payload_bytes=4 * 1024 * 1024,
            deadline_monotonic=self._host_open_deadline_monotonic,
        )
        reduced = reduce_plan_workflow_state(
            tuple(
                decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for raw in snapshot.events
            )
        )
        if (
            reduced.active
            or reduced.latest_accepted_plan_summary
            or reduced.latest_accepted_plan_artifact_id
        ):
            self.plan_state = reduced
        self._terminal_application_services.resume_pending_queue_deliveries()

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
            self._terminal_application_services.stop_admission()
        lease = self._background_model_call_admission_lease
        if lease is not None:
            lease.release()

    @property
    def runtime_session_id(self) -> str:
        return self.wiring.runtime_wiring.runtime_session.runtime_session_id

    @property
    def terminal_application_services(self):
        """Renderer-neutral application boundary for terminal clients."""

        return self._terminal_application_services

    async def repair_dangling_children_once(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if self._subagent_dangling_repair_done:
            return
        subagent_runtime = self.wiring.subagent_runtime
        if subagent_runtime is not None:
            await subagent_runtime.repair_dangling_children(
                deadline_monotonic=deadline_monotonic
            )
        self._subagent_dangling_repair_done = True

    @property
    def active_run_id(self) -> str | None:
        view = self._run_activation_service.active_host_run_view()
        return view.run_id if view is not None else None

    @property
    def suspended_run_id(self) -> str | None:
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return None
        view = self._run_activation_service.suspended_host_run_view()
        return view.run_id if view is not None else None

    @property
    def stopping_run_id(self) -> str | None:
        view = self._run_activation_service.stopping_host_run_view()
        if view is not None:
            return view.run_id
        attempt = self._boundary_attempt
        if (
            attempt is not None
            and attempt.prepared_activation is not None
            and attempt.prepared_activation.run_id
            in self._boundary_stop_requested_run_ids
        ):
            return attempt.prepared_activation.run_id
        return None

    @property
    def pending_interaction(self) -> PendingInteraction | None:
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return None
        owner = self._run_activation_service.suspended_host_run_view()
        view = owner.pending_interaction_view if owner is not None else None
        return view if isinstance(view, PendingInteraction) else None

    def _current_boundary_task(self) -> asyncio.Task[object] | None:
        attempt = self._boundary_attempt
        return attempt.owner_task if attempt is not None else None

    def _current_boundary_observer(self) -> _StreamObserver | None:
        attempt = self._boundary_attempt
        observer = attempt.observer if attempt is not None else None
        return observer if isinstance(observer, _StreamObserver) else None

    def _take_boundary_execution_ownership(self) -> HostRunBoundaryAttempt:
        attempt = self._boundary_attempt
        incoming = asyncio.current_task()
        if attempt is None or incoming is None:
            raise RuntimeError("Host ingress lost its boundary execution owner")
        previous = attempt.owner_task
        if previous is incoming:
            return attempt
        if previous.done():
            raise RuntimeError("completed boundary owner cannot transfer execution")
        prepared = attempt.prepared_activation
        if prepared is not None:
            prepared.transfer_owner_task(expected=previous, incoming=incoming)
        attempt.owner_task = incoming
        return attempt

    def _finish_ingress_owned_boundary(self, attempt: HostRunBoundaryAttempt) -> None:
        if self._boundary_attempt is not attempt:
            return
        if attempt.owner_task is not asyncio.current_task():
            return
        self._finish_boundary_attempt_safely(attempt)
        self._boundary_attempt = None

    async def acquire_compaction_memory_model_safe_point(
        self,
        operation_id: str,
        deadline_monotonic: float,
    ) -> _HostBackgroundModelCallAdmissionLease | None:
        """Reserve an idle Host only until the background ModelStart is durable."""

        if deadline_monotonic <= time.monotonic() or self._run_lock.locked():
            return None
        timeout = min(0.1, max(0.0, deadline_monotonic - time.monotonic()))
        try:
            await asyncio.wait_for(self._run_lock.acquire(), timeout=timeout)
        except TimeoutError:
            return None
        try:
            runtime_session = self.wiring.runtime_wiring.runtime_session
            host_state = self._ingress_coordinator.state_fact()
            if (
                self._lifecycle is not HostSessionLifecycle.OPEN
                or not self._ingress_coordinator.can_borrow_background_model_call()
                or self.active_run_id is not None
                or self.stopping_run_id is not None
                or self.pending_interaction is not None
                or self._boundary_attempt is not None
                and not self._boundary_attempt.owner_task.done()
                or runtime_session.reconciliation_required
                or runtime_session.model_stream_execution_registry.active_handle_count()
                != 0
                or self._background_model_call_admission_lease is not None
            ):
                self._run_lock.release()
                return None
            runtime_session.require_mutation_allowed()
            self._background_model_call_admission_generation += 1
            generation = self._background_model_call_admission_generation
            lease_id = "background-model-admission:" + context_fingerprint(
                "background-model-call-admission-id:v1",
                (self.runtime_session_id, operation_id, generation),
            ).removeprefix("sha256:")
            expires_at = min(deadline_monotonic, time.monotonic() + 10.0)
            proof_payload = {
                "lease_id": lease_id,
                "lease_generation": generation,
                "runtime_session_id": self.runtime_session_id,
                "operation_id": operation_id,
                "host_state_generation": host_state.state_generation,
                "active_run_frontier_fingerprint": context_fingerprint(
                    "background-model-call-active-run-frontier:v1", ()
                ),
                "permission_policy_revision": host_state.permission_policy_revision,
                "permission_policy_fingerprint": (
                    host_state.permission_policy_fingerprint
                ),
                "stop_intent_revision": self._stop_intent_revision,
                "close_intent_revision": host_state.close_intent_revision,
                "expected_provider_input_generation_revision": (
                    runtime_session.provider_input_generation_store.through_sequence
                ),
                "expires_at_monotonic": expires_at,
            }
            proof = BackgroundModelCallAdmissionProof(
                **proof_payload,
                proof_fingerprint=context_fingerprint(
                    "background-model-call-admission-proof:v1", proof_payload
                ),
            )
            identity_payload = {
                "lease_id": lease_id,
                "lease_generation": generation,
                "runtime_session_id": self.runtime_session_id,
                "operation_id": operation_id,
                "admission_proof_fingerprint": proof.proof_fingerprint,
            }
            identity = BackgroundModelCallAdmissionLeaseIdentity(
                **identity_payload,
                identity_fingerprint=context_fingerprint(
                    "background-model-call-admission-lease-identity:v1",
                    identity_payload,
                ),
            )
            lease = _HostBackgroundModelCallAdmissionLease(
                identity=identity,
                proof=proof,
                validator=self._validate_background_model_call_admission,
                releaser=self._release_background_model_call_admission,
            )
            self._background_model_call_admission_lease = lease
            return lease
        except BaseException:
            if self._run_lock.locked():
                self._run_lock.release()
            raise

    def _validate_background_model_call_admission(
        self,
        proof: BackgroundModelCallAdmissionProof,
    ) -> None:
        runtime_session = self.wiring.runtime_wiring.runtime_session
        host_state = self._ingress_coordinator.state_fact()
        lease = self._background_model_call_admission_lease
        if (
            lease is None
            or lease.proof != proof
            or not self._run_lock.locked()
            or self._lifecycle is not HostSessionLifecycle.OPEN
            or not self._ingress_coordinator.can_borrow_background_model_call()
            or self.active_run_id is not None
            or self.stopping_run_id is not None
            or self.pending_interaction is not None
            or self._boundary_attempt is not None
            and not self._boundary_attempt.owner_task.done()
            or host_state.state_generation != proof.host_state_generation
            or host_state.permission_policy_revision != proof.permission_policy_revision
            or host_state.permission_policy_fingerprint
            != proof.permission_policy_fingerprint
            or host_state.close_intent_revision != proof.close_intent_revision
            or self._stop_intent_revision != proof.stop_intent_revision
            or runtime_session.provider_input_generation_store.through_sequence
            != proof.expected_provider_input_generation_revision
            or time.monotonic() >= proof.expires_at_monotonic
        ):
            raise HostIngressAdmissionStale(
                "background model-call Host authority became stale"
            )
        runtime_session.require_mutation_allowed()

    def _release_background_model_call_admission(
        self,
        identity: BackgroundModelCallAdmissionLeaseIdentity,
    ) -> None:
        lease = self._background_model_call_admission_lease
        if lease is not None and lease.identity == identity:
            self._background_model_call_admission_lease = None
            if self._run_lock.locked():
                self._run_lock.release()

    def install_compaction_memory_extraction_driver(
        self,
        *,
        projection_service: object,
        connection_provider: object,
    ) -> None:
        """Register the one live session driver after Host ownership exists."""

        if self._compaction_memory_extraction_registration is not None:
            raise RuntimeError(
                "compaction memory extraction driver is already installed"
            )
        driver, registration = self.wiring.build_compaction_memory_extraction_driver(
            projection_service=projection_service,
            connection_provider=connection_provider,
            safe_point_acquirer=self.acquire_compaction_memory_model_safe_point,
            on_result_full=self._notify_governance,
        )
        self._compaction_memory_extraction_driver = driver
        self._compaction_memory_extraction_registration = registration
        projection_service.wake(self.runtime_session_id)

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

        stale_discard_counts = supervisor.stale_discard_counts()
        runtime_session = self.wiring.runtime_wiring.runtime_session
        execution_port = runtime_session.mcp_tool_execution_port
        if execution_port is None:
            raise RuntimeError("MCP execution port is unavailable during installation")
        try:
            new_installation = build_mcp_installation(
                execution_port=execution_port,
                artifact_options=runtime_session.artifact_service.options,
                config_epoch=ticket.config_epoch,
                event_safe_config_set_fingerprint=ticket.event_safe_config_set_fingerprint,
                snapshots=new_snapshots,
                configs_by_server=configs_by_server,
                slots_by_server=slots_by_server,
                installation_id=None,
                previous_installation=old_installation,
            )
            new_capability_runtime = _replace_mcp_capability_provider(
                self.wiring.agent_runtime.capability_runtime,
                new_installation,
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
            runtime_session.dynamic_tool_installations = (
                new_installation.ordered_binding_installations
            )
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
        subagent_runtime = self.wiring.subagent_runtime
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
        run_id = self.suspended_run_id
        view = (
            self._run_activation_service.run_view(run_id)
            if run_id is not None
            else None
        )
        binding = view.pending_mcp_binding_identity if view is not None else None
        if binding is None:
            return None
        try:
            binding_identity = McpBindingIdentity(
                server_id=binding.server_id,
                slot_id=binding.slot_id,
                snapshot_id=binding.snapshot_id,
                discovery_generation=binding.discovery_generation,
            )
        except (AttributeError, TypeError, ValueError):
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

    async def recover_deferred_mcp_runs(
        self,
        run_ids: tuple[str, ...],
        *,
        deadline_monotonic: float,
    ) -> None:
        """Rebind restart-safe stateless MCP continuations after discovery."""

        if not run_ids:
            return
        if len(run_ids) != 1:
            raise RuntimeError("Host reopen cannot own multiple deferred MCP runs")
        from pulsara_agent.host.mcp_recovery import recover_host_mcp_run

        recovered = await recover_host_mcp_run(
            self,
            run_id=run_ids[0],
            deadline_monotonic=deadline_monotonic,
        )
        reservation_id = self.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service.adopt_recovered_run_growth(
            recovered.run_id
        )
        self._presentation_history_run_reservations[recovered.run_id] = reservation_id
        if recovered.recovery_state == "awaiting_client_input":
            await self._sync_ingress_waiting_state()
            return
        if not isinstance(
            recovered.prepared_resolution,
            PreparedMcpInputRequiredResolution,
        ):
            raise RuntimeError("replay-ready MCP recovery lost its resolution")
        await self._ingress_coordinator.adopt_recovered_active_run(
            run_start_event_id=recovered.run_start_event_id
        )

        async def on_settled(outcome: RunActivationOutcome) -> None:
            self._on_activation_settled(outcome)
            pending = self.pending_interaction
            await self._ingress_coordinator.settle_recovered_active_run(
                resume_match_key=(
                    _pending_interaction_match_key(pending)
                    if pending is not None
                    else None
                )
            )

        dispatch = self._run_activation_service.start_resume_result_activation(
            run_id=recovered.run_id,
            host_session_id=self.host_session_id,
            interaction_kind="mcp_input_required",
            resolution=recovered.prepared_resolution,
            on_activation_settled=on_settled,
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            await self._ingress_coordinator.settle_recovered_active_run(
                resume_match_key=None
            )
            raise RuntimeError(
                f"recovered MCP continuation activation was blocked: {dispatch.reason}"
            )

    def _new_run_boundary_identity(
        self,
        *,
        kind: str = "pre_run",
        attempt_number: int = 1,
    ) -> HostRunBoundaryIdentityFact:
        identity = HostRunBoundaryIdentityFact(
            boundary_id=f"run_boundary:{uuid4().hex}",
            kind=kind,
            runtime_session_id=self.runtime_session_id,
            run_id=f"run:{uuid4().hex}",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
            attempt_number=attempt_number,
            observed_at_utc=utc_now(),
        )
        return identity

    def _prepare_interaction_resume_attempt(
        self,
        *,
        pending: PendingInteraction,
        interaction_kind: Literal["approval", "plan", "mcp_input_required"],
        resolution: object,
    ) -> PreparedInteractionResumeAttempt:
        if isinstance(pending, PendingApproval):
            interaction_id = pending.approval_id
            resolution_kind = "approval"
        elif isinstance(pending, PendingMcpInputRequired):
            interaction_id = pending.interaction_id
            resolution_kind = "mcp_input_required"
        else:
            if pending.kind == "question":
                interaction_id = pending.question_id
                resolution_kind = "plan_question"
            else:
                interaction_id = pending.exit_request_id
                resolution_kind = "plan_exit"
            if not isinstance(interaction_id, str) or not interaction_id:
                raise RuntimeError("plan suspension lost its durable interaction id")
        return self._interaction_transition_port.prepare_resume(
            run_id=pending.run_id,
            interaction_id=interaction_id,
            interaction_kind=interaction_kind,
            resolution_kind=resolution_kind,
            resolution=resolution,
        )

    async def _commit_interaction_transition_attempt(
        self,
        prepared: PreparedInteractionResumeAttempt,
    ) -> tuple[CommittedInteractionResumeBoundary, tuple[AgentEvent, ...]]:
        committed, stored = await self._prepare_and_commit_resume_boundary(
            prepared_attempt=prepared,
            pending=prepared.pending_public_view,  # type: ignore[arg-type]
            interaction_kind=prepared.interaction_kind,
            identity=prepared.boundary_identity,
            resolution=prepared.resolution,
        )
        return committed, stored

    def _classify_interaction_transition_failure(
        self, exc: BaseException
    ) -> Literal["none", "unknown", "other"]:
        if isinstance(
            exc,
            (HostIngressAdmissionStale, TerminalNotificationAdmissionStale),
        ):
            return "none"
        attempt = self._boundary_attempt
        confirmation = attempt.commit_confirmation if attempt is not None else None
        if (
            confirmation is not None
            and confirmation.status is BoundaryBatchCommitStatus.NONE
        ):
            return "none"
        if (
            confirmation is not None
            and confirmation.status is BoundaryBatchCommitStatus.UNKNOWN
        ):
            return "unknown"
        if attempt is not None and attempt.commit_state == "not_started":
            # Contract resolution and preparation failures happen before any
            # stable transition candidate reaches the RuntimeSession writer.
            # They are ordinary deterministic errors, not uncertain commits.
            return "other"
        if (
            confirmation is not None
            and confirmation.status is BoundaryBatchCommitStatus.FULL
        ):
            # The ledger transition is already FULL. A later fold or
            # terminalization error is not an uncertain write outcome and the
            # original typed failure must remain observable to the caller.
            return "other"
        try:
            outcome = (
                self.wiring.runtime_wiring.runtime_session.resolved_event_write_outcome(
                    exc
                )
            )
        except BaseException:
            return "other"
        if outcome.status == "none":
            return "none"
        if outcome.status == "unknown":
            return "unknown"
        return "other"

    @staticmethod
    def _require_full_interaction_transition(
        receipt: InteractionTransitionCommitReceipt,
    ) -> tuple[AgentEvent, ...]:
        if isinstance(receipt.outcome, InteractionTransitionNone):
            raise InteractionTransitionNotCommitted(
                "interaction resume candidate was not committed"
            )
        if isinstance(receipt.outcome, InteractionTransitionUntrusted):
            raise InteractionTransitionReconciliationRequired(
                "interaction resume requires durable reconciliation"
            )
        if not isinstance(receipt.outcome, InteractionTransitionFull):
            raise RuntimeError("interaction transition returned an invalid outcome")
        return receipt.committed_events

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
        ingress_semantic_fingerprint: str,
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
            user_intent_fingerprint=ingress_semantic_fingerprint,
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
        ingress_owner: HostIngressAttemptOwner,
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
    ) -> PreparedNewRunBoundaryAuthority:
        transcript_fact = self._transcript_snapshot_fact(
            preflight_terminal=preflight_terminal,
            checkpoint_terminal=checkpoint_terminal,
            source_through_sequence=transcript_source_through_sequence,
            source_event_count=transcript_source_event_count,
        )
        host_run_ingress = self._build_host_run_ingress(
            ingress_owner=ingress_owner,
            identity=identity,
            user_input=user_input,
        )
        basis = self._capability_basis(
            identity=identity,
            permission_snapshot=permission_snapshot,
            user_input=user_input,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
            execution_surface_identity=frozen_surface.identity,
            ingress_semantic_fingerprint=(
                host_run_ingress.semantic_identity.ingress_semantic_fingerprint
            ),
        )
        host_ingress_admission_proof = self._ingress_coordinator.admission_proof(
            ingress_owner,
            ingress_fact_fingerprint=host_run_ingress.fact_fingerprint,
        )
        current_user_message = CurrentUserMessageFact(
            message_id=f"user-message:{identity.run_id}",
            source_kind=(
                "host_runtime_request"
                if isinstance(host_run_ingress, RuntimeRequestRunIngressFact)
                else "host_user_input"
            ),
            text=user_input,
            observed_at_utc=identity.observed_at_utc,
            content_sha256=text_sha256(user_input),
            source_artifact_id=None,
        )
        terminal_run_end_event_id = f"run_end:{uuid4().hex}"
        new_run_boundary = NewRunBoundaryFact(
            identity=identity,
            transcript=transcript_fact,
            model_target_fingerprint=run_model_target.fact.target_fingerprint,
            permission_snapshot_id=permission_snapshot.snapshot_id,
            mcp_installation_id=(frozen_surface.identity.mcp_installation_id),
            capability_basis=basis.fact,
            degraded_reason_codes=(),
        )
        authority = PreparedNewRunBoundaryAuthority(
            identity=identity,
            transcript=transcript_fact,
            mcp_installation=self._mcp_installation_reference_fact(),
            plan=self._plan_workflow_state_fact(),
            capability_basis=basis,
            frozen_execution_surface=frozen_surface,
            host_run_ingress=host_run_ingress,
            host_ingress_admission_proof=host_ingress_admission_proof,
            current_user_message=current_user_message,
            terminal_run_end_event_id=terminal_run_end_event_id,
            new_run_boundary=new_run_boundary,
        )
        attempt = self._boundary_attempt
        if attempt is None:
            raise RuntimeError("new-run boundary lost its process owner")
        attempt.prepared_authority = authority
        return authority

    def _build_host_run_ingress(
        self,
        *,
        ingress_owner: HostIngressAttemptOwner,
        identity: HostRunBoundaryIdentityFact,
        user_input: str,
    ) -> HumanRunIngressFact | RuntimeRequestRunIngressFact:
        if ingress_owner.kind == "runtime":
            return self._build_runtime_run_ingress(
                ingress_owner=ingress_owner,
                identity=identity,
                request_text=user_input,
            )
        return self._build_human_run_ingress(
            ingress_owner=ingress_owner,
            identity=identity,
            user_input=user_input,
        )

    def _build_human_run_ingress(
        self,
        *,
        ingress_owner: HostIngressAttemptOwner,
        identity: HostRunBoundaryIdentityFact,
        user_input: str,
    ) -> HumanRunIngressFact:
        accepted_ordinal = ingress_owner.accepted_ingress_ordinal
        if accepted_ordinal is None or ingress_owner.owner_state != "preparing":
            raise RuntimeError("human ingress has not been admitted for preparation")
        encoded = encode_human_input(
            user_input,
            causal_occurrence_semantic_fingerprint=context_fingerprint(
                "host-human-ingress-occurrence:v1",
                {
                    "ingress_id": ingress_owner.ingress_id,
                    "runtime_session_id": self.runtime_session_id,
                    "run_id": identity.run_id,
                },
            ),
        )
        human = encoded.semantic_fact
        selected = self.wiring.runtime_wiring.runtime_session.terminal_notification_store.pending_notifications(
            include_unmonitored_completions=True,
            maximum_items=8,
        )
        ingress_owner.selected_notifications = selected
        ingress_owner.selected_notification_head_fingerprints = tuple(
            item.process_head_fingerprint for item in selected
        )
        attachments = tuple(item.attachment for item in selected)
        semantic_fingerprints = (
            human.semantic_fingerprint,
            *(
                item.observation_wire_semantic.wire_semantic_fingerprint
                for item in attachments
            ),
        )
        placements = tuple(
            build_frozen_fact(
                HostIngressItemPlacementFact,
                schema_version="host_ingress_item_placement.v1",
                item_kind="human_input" if index == 0 else "runtime_notification",
                item_semantic_fingerprint=fingerprint,
                accepted_ingress_ordinal=accepted_ordinal,
                item_ordinal=index,
            )
            for index, fingerprint in enumerate(semantic_fingerprints)
        )
        semantic = build_frozen_fact(
            HostRunIngressSemanticFact,
            schema_version="host_run_ingress_semantic.v1",
            ordered_current_input_semantic_fingerprints=semantic_fingerprints,
        )
        attribution = build_frozen_fact(
            HostRunIngressAttributionFact,
            schema_version="host_run_ingress_attribution.v1",
            ingress_id=ingress_owner.ingress_id,
            host_session_id=self.host_session_id,
            conversation_id=self.conversation_id,
            observed_at_utc=identity.observed_at_utc,
            ingress_semantic_fingerprint=semantic.ingress_semantic_fingerprint,
            ordered_item_placements=placements,
        )
        return build_frozen_fact(
            HumanRunIngressFact,
            schema_version="human_run_ingress.v1",
            semantic_identity=semantic,
            attribution=attribution,
            human_message=human,
            attached_runtime_notifications=attachments,
        )

    def _build_runtime_run_ingress(
        self,
        *,
        ingress_owner: HostIngressAttemptOwner,
        identity: HostRunBoundaryIdentityFact,
        request_text: str,
    ) -> RuntimeRequestRunIngressFact:
        accepted_ordinal = ingress_owner.accepted_ingress_ordinal
        if accepted_ordinal is None or ingress_owner.owner_state != "preparing":
            raise RuntimeError("runtime ingress has not been admitted for preparation")
        selected = tuple(ingress_owner.selected_notifications)
        if not selected and ingress_owner.selection_wake_chain_id is not None:
            chain_snapshot = self.wiring.runtime_wiring.runtime_session.terminal_notification_store.autonomy_chain_snapshot(
                ingress_owner.selection_wake_chain_id
            )
            policy = chain_snapshot.attribution.resolved_policy
            selected = self.wiring.runtime_wiring.runtime_session.terminal_notification_store.pending_notifications(
                include_unmonitored_completions=False,
                maximum_items=policy.maximum_notifications_per_autonomous_ingress,
                wake_chain_id=ingress_owner.selection_wake_chain_id,
                include_automatic_delivery_deferred=False,
            )
            if not selected:
                raise HostIngressAdmissionStale(
                    "runtime ingress notification selection is no longer pending"
                )
            ingress_owner.selected_notifications = selected
            ingress_owner.selected_notification_head_fingerprints = tuple(
                item.process_head_fingerprint for item in selected
            )
            ingress_owner.expected_autonomy_chain_state_fingerprint = (
                chain_snapshot.state.state_fingerprint
            )
            ingress_owner.proposed_automatic_delivery_ordinal = (
                chain_snapshot.state.last_automatic_delivery_ordinal + 1
            )
        if not selected:
            raise RuntimeError("runtime ingress lost its notification owner")
        attachments = tuple(item.attachment for item in selected)
        chains = {item.wake_chain_id for item in attachments}
        if len(chains) != 1 or None in chains:
            raise RuntimeError("autonomous runtime ingress crosses wake chains")
        wake_chain_id = next(iter(chains))
        encoded = encode_runtime_request(
            request_text,
            request_kind="terminal_process_observation",
            business_occurrence_semantic_fingerprint=context_fingerprint(
                "host-terminal-monitor-runtime-request-occurrence:v1",
                tuple(item.attachment_fingerprint for item in attachments),
            ),
            lifecycle_class="current_run_transcript",
        )
        runtime_request = encoded.semantic_fact
        ordinal = ingress_owner.proposed_automatic_delivery_ordinal
        if ordinal is None:
            raise RuntimeError("runtime ingress lacks automatic delivery ordinal")
        chain_snapshot = self.wiring.runtime_wiring.runtime_session.terminal_notification_store.autonomy_chain_snapshot(
            wake_chain_id
        )
        if (
            ingress_owner.expected_autonomy_chain_state_fingerprint
            != chain_snapshot.state.state_fingerprint
        ):
            raise RuntimeError("runtime ingress autonomy chain state became stale")
        chain_policy_fingerprint = (
            chain_snapshot.attribution.resolved_policy.policy_fingerprint
        )
        autonomy_delivery = build_frozen_fact(
            TerminalAutonomousDeliveryFact,
            schema_version="terminal_autonomous_delivery.v1",
            wake_chain_id=wake_chain_id,
            ordered_source_attachment_fingerprints=tuple(
                item.attachment_fingerprint for item in attachments
            ),
            delivery_kind="autonomous_run_start",
            automatic_delivery_ordinal=ordinal,
            chain_policy_fingerprint=chain_policy_fingerprint,
        )
        semantic_fingerprints = (
            runtime_request.semantic_fingerprint,
            *(
                item.observation_wire_semantic.wire_semantic_fingerprint
                for item in attachments
            ),
        )
        placements = tuple(
            build_frozen_fact(
                HostIngressItemPlacementFact,
                schema_version="host_ingress_item_placement.v1",
                item_kind="runtime_request" if index == 0 else "runtime_notification",
                item_semantic_fingerprint=fingerprint,
                accepted_ingress_ordinal=accepted_ordinal,
                item_ordinal=index,
            )
            for index, fingerprint in enumerate(semantic_fingerprints)
        )
        semantic = build_frozen_fact(
            HostRunIngressSemanticFact,
            schema_version="host_run_ingress_semantic.v1",
            ordered_current_input_semantic_fingerprints=semantic_fingerprints,
        )
        attribution = build_frozen_fact(
            HostRunIngressAttributionFact,
            schema_version="host_run_ingress_attribution.v1",
            ingress_id=ingress_owner.ingress_id,
            host_session_id=self.host_session_id,
            conversation_id=self.conversation_id,
            observed_at_utc=identity.observed_at_utc,
            ingress_semantic_fingerprint=semantic.ingress_semantic_fingerprint,
            ordered_item_placements=placements,
        )
        return build_frozen_fact(
            RuntimeRequestRunIngressFact,
            schema_version="runtime_request_run_ingress.v1",
            semantic_identity=semantic,
            attribution=attribution,
            runtime_request=runtime_request,
            source_notifications=attachments,
            autonomy_delivery=autonomy_delivery,
        )

    async def _commit_new_run_entry(
        self,
        *,
        prepared_activation: PreparedRunActivationOwner,
        prepared: PreparedNewRunBoundary,
    ) -> tuple[AgentRunDraft, CommittedHostRunEntry, tuple[AgentEvent, ...]]:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        draft = await service.prepare_run_draft(
            prepared_activation,
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
            host_run_ingress=prepared.host_run_ingress,
            host_ingress_admission_proof=(prepared.host_ingress_admission_proof),
            prior_messages=list(prepared.owned_transcript_messages),
        )
        runtime_session = self.wiring.runtime_wiring.runtime_session
        pending_audits = prepared.pending_mcp_audits
        event_context = EventContext(
            run_id=prepared.identity.run_id,
            turn_id=prepared.identity.turn_id,
            reply_id=prepared.identity.reply_id,
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
        notification_candidates = self._run_start_notification_candidates(
            ingress_owner=prepared.ingress_owner,
            run_start=draft.run_start_event,
        )
        candidates: tuple[AgentEvent, ...] = (
            draft.run_start_event,
            *notification_candidates,
            window_open,
            account_open,
            *pending_audits,
        )
        transaction_companion = None
        queue_delivery = prepared.queued_prompt_delivery
        if queue_delivery is not None:
            dispatch_batch = (
                runtime_session.prompt_queue_mutation_service.prepare_commit_to_run(
                    queue_item_id=queue_delivery.queue_item_id,
                    reservation_fingerprint=(queue_delivery.reservation_fingerprint),
                    command_id=(
                        f"queue-dispatch:{queue_delivery.queue_item_id}:"
                        f"{queue_delivery.reservation_generation}"
                    ),
                    event_context=event_context,
                    run_start_event=draft.run_start_event,
                    candidate_prefix=candidates,
                )
            )
            candidates = dispatch_batch.prepared_events
            transaction_companion = dispatch_batch.transaction_companion
        self._set_boundary_candidates(candidates)
        self._set_boundary_phase(HostRunBoundaryPhase.DURABLE_COMMIT)
        self._set_boundary_commit_state("commit_in_flight")
        try:
            stored = tuple(
                await runtime_session.commit_accepted_events(
                    candidates,
                    transaction_companion=transaction_companion,
                )
            )
        except BaseException as exc:
            if isinstance(
                exc,
                (HostIngressAdmissionStale, TerminalNotificationAdmissionStale),
            ):
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.NONE,
                )
                self._set_boundary_commit_state("not_started")
                runtime_session.transcript_projection_checkpoint_service.discard_prepared_run_seed(
                    prepared.identity.run_id
                )
                if isinstance(exc, HostIngressAdmissionStale):
                    raise
                raise HostIngressAdmissionStale(
                    "Host ingress notification authority changed before RunStart"
                ) from exc
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
                            prepared.identity.run_id
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
                publication_status=(
                    "failed_after_commit"
                    if isinstance(exc, EventPublicationAfterCommitError)
                    and exc.result.publication_errors
                    else "unavailable"
                    if isinstance(exc, EventPublicationAfterCommitError)
                    else "failed_after_commit"
                ),
            )
            await self._ingress_coordinator.mark_committed(
                prepared.ingress_owner,
                run_start_event_id=committed.run_start_event.id,
            )
            await self._adopt_committed_host_run(
                committed=committed,
                prepared=prepared,
            )
            if isinstance(exc, asyncio.CancelledError):
                _clear_current_task_cancellation()
            stop_request = service.pending_stop_request(prepared.identity.run_id)
            if stop_request is not None:
                await self._install_run_termination_intent_for_run(
                    prepared.identity.run_id, stop_request.reason
                )
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=prepared.identity.run_id,
                    abort_reason=stop_request.reason,
                )
            else:
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=prepared.identity.run_id,
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
        await self._ingress_coordinator.mark_committed(
            prepared.ingress_owner,
            run_start_event_id=committed.run_start_event.id,
        )
        await self._adopt_committed_host_run(
            committed=committed,
            prepared=prepared,
        )
        return draft, committed, stored

    def _run_start_notification_candidates(
        self,
        *,
        ingress_owner: HostIngressAttemptOwner,
        run_start: RunStartEvent,
    ) -> tuple[AgentEvent, ...]:
        selected = tuple(ingress_owner.selected_notifications)
        if not selected:
            return ()
        source_events = tuple(item.source_event for item in selected)
        source_references = tuple(
            sorted(
                (
                    event_reference_from_stored(
                        event,
                        runtime_session_id=self.runtime_session_id,
                    )
                    for event in source_events
                ),
                key=lambda item: (item.sequence, item.event_id),
            )
        )
        outcome = (
            "autonomous_dispatched"
            if ingress_owner.kind == "runtime"
            else "merged_into_human_run"
        )
        disposition = TerminalProcessObservationDeliveryDispositionEvent(
            id=context_fingerprint(
                "terminal-notification-run-start-disposition-id:v1",
                (
                    run_start.id,
                    outcome,
                    tuple(item.event_id for item in source_references),
                ),
            ).replace("sha256:", "terminal_notification_disposition:"),
            run_id=run_start.run_id,
            turn_id=run_start.turn_id,
            reply_id=run_start.reply_id,
            observation_source_references=source_references,
            outcome=outcome,
            run_start_event_identity=stable_event_identity(
                run_start,
                runtime_session_id=self.runtime_session_id,
            ),
        )
        release_ids = tuple(
            sorted(
                {
                    f"terminal_completion_head:{_notification_process_id(item.source_event)}"
                    for item in selected
                    if _notification_is_terminal(item.source_event)
                }
            )
        )
        releases = (
            self.wiring.runtime_wiring.runtime_session.terminal_notification_account_coordinator.freeze_released_events(
                reservation_ids=release_ids,
                cause_events=(disposition,),
            )
            if release_ids
            else ()
        )
        return disposition, *releases

    def _on_terminal_notification_committed(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        delivered_ids = {
            reference.event_id
            for event in events
            if isinstance(event, TerminalProcessObservationDeliveryDispositionEvent)
            and event.outcome == "active_run_safe_point"
            for reference in event.observation_source_references
        }
        lease = self._active_run_monitor_safe_point_lease
        if lease is not None and delivered_ids == {
            event.id for event in lease.source_events
        }:
            self._active_run_monitor_safe_point_lease = None
        if not any(
            isinstance(item, TerminalProcessMonitorObservationCommittedEvent)
            for item in events
        ):
            return
        loop = self._host_event_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._ensure_terminal_notification_dispatch)

    async def recover_terminal_monitor_owners_before_open(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Settle restart-owned monitor facts before publishing this HostSession."""

        self._host_event_loop = asyncio.get_running_loop()
        if deadline_monotonic != self._host_open_deadline_monotonic:
            raise ValueError("terminal recovery deadline diverged from Host open")
        if time.monotonic() >= deadline_monotonic:
            raise TimeoutError(
                "Host open deadline expired before terminal monitor recovery"
            )
        await asyncio.to_thread(
            self.wiring.runtime_wiring.runtime_session.terminal_monitor_coordinator.recover_after_restart,
            deadline_monotonic=deadline_monotonic,
        )

    async def recover_terminal_command_owners_before_open(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Terminalize orphaned durable command admissions without replaying them."""

        if deadline_monotonic != self._host_open_deadline_monotonic:
            raise ValueError(
                "terminal command recovery deadline diverged from Host open"
            )
        if time.monotonic() >= deadline_monotonic:
            raise TimeoutError(
                "Host open deadline expired before terminal command recovery"
            )
        await self._terminal_application_services.recover_pending_commands(
            deadline_monotonic=deadline_monotonic
        )

    def activate_terminal_notification_dispatch_after_open(self) -> None:
        """Enable autonomous ingress only after the HostSession is published."""

        self._host_event_loop = asyncio.get_running_loop()
        self._terminal_notification_dispatch_enabled = True
        store = self.wiring.runtime_wiring.runtime_session.terminal_notification_store
        if store.pending_notifications(
            include_unmonitored_completions=False,
            maximum_items=8,
            include_automatic_delivery_deferred=False,
        ):
            self._ensure_terminal_notification_dispatch()

    def _ensure_terminal_notification_dispatch(self) -> None:
        if (
            self._lifecycle is not HostSessionLifecycle.OPEN
            or not self._terminal_notification_dispatch_enabled
        ):
            return
        task = self._terminal_notification_dispatch_task
        if task is not None and not task.done():
            return
        self._terminal_notification_dispatch_task = asyncio.create_task(
            self._dispatch_terminal_notifications(),
            name=f"terminal-monitor-dispatch:{self.host_session_id}",
        )

    async def _dispatch_terminal_notifications(self) -> None:
        runtime_session = self.wiring.runtime_wiring.runtime_session
        store = runtime_session.terminal_notification_store
        try:
            while self._lifecycle is HostSessionLifecycle.OPEN:
                ingress_lifecycle = (
                    self._ingress_coordinator.state_fact().lifecycle_state
                )
                if ingress_lifecycle in {
                    "preparing",
                    "active",
                }:
                    # The active Agent loop owns the PRE_MODEL_STEP opportunity.
                    # Keep the dispatcher alive so a run that terminates without
                    # another sampling immediately falls back to Host ingress.
                    await asyncio.sleep(0.05)
                    continue
                pending = store.pending_notifications(
                    include_unmonitored_completions=False,
                    maximum_items=8,
                    include_automatic_delivery_deferred=False,
                )
                if not pending:
                    return
                wake_chain_id = pending[0].wake_chain_id
                if wake_chain_id is None:
                    return
                chain = store.autonomy_chain_snapshot(wake_chain_id)
                policy = chain.attribution.resolved_policy
                selected = tuple(
                    item for item in pending if item.wake_chain_id == wake_chain_id
                )[: policy.maximum_notifications_per_autonomous_ingress]
                if ingress_lifecycle == "waiting_user":
                    await self._defer_terminal_notifications(
                        selected,
                        reason="host_waiting_user",
                    )
                    return
                state = chain.state
                if not self._terminal_monitor_autonomy_allowed():
                    await self._defer_terminal_notifications(
                        selected,
                        reason="autonomy_permission_disabled",
                    )
                    continue
                if (
                    state.last_automatic_delivery_ordinal
                    >= policy.maximum_automatic_deliveries
                ):
                    await self._defer_terminal_notifications(
                        selected,
                        reason="wake_budget_exhausted",
                    )
                    continue
                if state.last_automatic_delivery_at_utc is not None:
                    last = datetime.fromisoformat(
                        state.last_automatic_delivery_at_utc.replace("Z", "+00:00")
                    )
                    remaining = (
                        policy.minimum_automatic_delivery_interval_seconds
                        - (datetime.now(timezone.utc) - last).total_seconds()
                    )
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        continue
                source_ids = tuple(item.source_event.id for item in selected)
                ingress_id = context_fingerprint(
                    "terminal-monitor-autonomous-ingress-id:v1",
                    (wake_chain_id, state.state_revision, source_ids),
                ).replace("sha256:", "host_ingress:")
                request_text = (
                    "Review the attached terminal process monitor observations and "
                    "continue the current task only when action is warranted."
                )
                await self._ingress_coordinator.submit(
                    kind="runtime",
                    payload=request_text,
                    ingress_id=ingress_id,
                    selected_notification_head_fingerprints=tuple(
                        item.process_head_fingerprint for item in selected
                    ),
                    selected_notifications=selected,
                    expected_autonomy_chain_state_fingerprint=(state.state_fingerprint),
                    proposed_automatic_delivery_ordinal=(
                        state.last_automatic_delivery_ordinal + 1
                    ),
                    selection_wake_chain_id=wake_chain_id,
                    runner=lambda owner: self._run_human_ingress(
                        owner,
                        user_input=request_text,
                        active_skill_names=frozenset(),
                    ),
                )
        except (HostIngressClosedError, asyncio.CancelledError):
            return
        except Exception as exc:
            self._terminal_notification_dispatch_error = f"{type(exc).__name__}: {exc}"
            if (
                self._lifecycle is HostSessionLifecycle.OPEN
                and not runtime_session.reconciliation_required
            ):
                await asyncio.sleep(0.2)
                self._terminal_notification_dispatch_task = None
                self._ensure_terminal_notification_dispatch()

    async def _borrow_active_run_monitor_safe_point(
        self,
        run_id: str,
        next_model_call_index: int,
    ) -> ActiveRunMonitorSafePointLease | None:
        """Borrow one confirmed notification set before context preparation."""

        if (
            next_model_call_index < 2
            or self._lifecycle is not HostSessionLifecycle.OPEN
        ):
            return None
        if not self._terminal_monitor_autonomy_allowed():
            return None
        runtime_session = self.wiring.runtime_wiring.runtime_session
        with (
            self._ingress_coordinator.authority_guard(),
            runtime_session.write_coordinator.lock,
        ):
            service = self.wiring.run_activation_service
            if service is None:
                raise RuntimeError("runtime composition lacks its activation service")
            active_state = service.active_safe_point_state(run_id)
            if (
                active_state is None
                or self.active_run_id != run_id
                or not self._ingress_coordinator.can_borrow_active_run_notifications()
                or active_state.has_pending_interaction
                or active_state.has_pending_tool_calls
            ):
                return None
            existing = self._active_run_monitor_safe_point_lease
            if existing is not None:
                if (
                    existing.run_id == run_id
                    and existing.next_model_call_index == next_model_call_index
                ):
                    return existing
                raise HostIngressAdmissionStale(
                    "another active-run monitor safe point still owns notifications"
                )
            if (
                not active_state.terminal_open
                or not active_state.termination_intent_absent
            ):
                return None
            selected_all = (
                runtime_session.terminal_notification_store.pending_notifications(
                    include_unmonitored_completions=False,
                    maximum_items=8,
                    include_automatic_delivery_deferred=False,
                )
            )
            if not selected_all:
                return None
            wake_chain_id = selected_all[0].wake_chain_id
            if wake_chain_id is None:
                return None
            chain = runtime_session.terminal_notification_store.autonomy_chain_snapshot(
                wake_chain_id
            )
            policy = chain.attribution.resolved_policy
            selected = tuple(
                item for item in selected_all if item.wake_chain_id == wake_chain_id
            )[: policy.maximum_notifications_per_autonomous_ingress]
            if not selected:
                return None
            if (
                chain.state.last_automatic_delivery_ordinal
                >= policy.maximum_automatic_deliveries
            ):
                return None
            if chain.state.last_automatic_delivery_at_utc is not None:
                previous = datetime.fromisoformat(
                    chain.state.last_automatic_delivery_at_utc.replace("Z", "+00:00")
                )
                if (
                    datetime.now(timezone.utc) - previous
                ).total_seconds() < policy.minimum_automatic_delivery_interval_seconds:
                    return None
            previous_index = next_model_call_index - 1
            disposition_event_id = (
                active_state.latest_model_control_disposition_event_id
            )
            disposition_index = (
                active_state.latest_model_control_disposition_model_call_index
            )
            disposition = (
                runtime_session.event_log.get_by_id(disposition_event_id)
                if isinstance(disposition_event_id, str)
                else None
            )
            if (
                not isinstance(disposition, ModelCallControlDispositionResolvedEvent)
                or disposition_index != previous_index
                or disposition.model_call_index != previous_index
                or disposition.run_id != run_id
                or disposition.disposition is not ModelCallControlDisposition.ACCEPTED
            ):
                return None
            previous_end = runtime_session.event_log.get_by_id(
                disposition.model_call_end_event_id
            )
            run_start_id = active_state.run_start_event_id
            run_start = runtime_session.event_log.get_by_id(run_start_id)
            if (
                not isinstance(previous_end, ModelCallEndEvent)
                or not isinstance(run_start, RunStartEvent)
                or previous_end.outcome != "completed"
            ):
                return None
            generation = runtime_session.provider_input_generation_store.latest_open_session_continuity_snapshot(
                call_lane="main_agent"
            )
            if generation is None or generation.core_state is None:
                return None
            host_state = self._ingress_coordinator.state_fact()
            notification_state = (
                runtime_session.terminal_notification_store.projection_snapshot()
            )
            lease_id = context_fingerprint(
                "active-run-monitor-safe-point-lease:v1",
                (
                    run_id,
                    next_model_call_index,
                    tuple(item.source_event.id for item in selected),
                    host_state.state_fingerprint,
                    notification_state.state_fingerprint,
                    chain.state.state_fingerprint,
                ),
            ).replace("sha256:", "active_monitor_lease:")
            lease = ActiveRunMonitorSafePointLease(
                lease_id=lease_id,
                runtime_session_id=self.runtime_session_id,
                run_id=run_id,
                next_model_call_index=next_model_call_index,
                source_events=tuple(item.source_event for item in selected),
                attachments=tuple(item.attachment for item in selected),
                selected_notification_head_fingerprints=tuple(
                    item.process_head_fingerprint for item in selected
                ),
                notification_state_fingerprint=notification_state.state_fingerprint,
                wake_chain_id=wake_chain_id,
                expected_autonomy_chain_state_fingerprint=(
                    chain.state.state_fingerprint
                ),
                proposed_automatic_delivery_ordinal=(
                    chain.state.last_automatic_delivery_ordinal + 1
                ),
                chain_policy_fingerprint=policy.policy_fingerprint,
                host_state_generation=host_state.state_generation,
                permission_policy_revision=host_state.permission_policy_revision,
                permission_policy_fingerprint=(
                    host_state.permission_policy_fingerprint
                ),
                close_intent_revision=host_state.close_intent_revision,
                stop_intent_revision=self._stop_intent_revision,
                termination_intent_revision=self._termination_intent_revision,
                active_segment_id=active_state.segment_id,
                active_segment_generation=active_state.segment_generation,
                llm_lifecycle_generation=(
                    runtime_session.model_stream_execution_registry.generation + 1
                ),
                run_start_event_reference=event_reference_from_stored(
                    run_start, runtime_session_id=self.runtime_session_id
                ),
                previous_model_call_end_event_reference=event_reference_from_stored(
                    previous_end, runtime_session_id=self.runtime_session_id
                ),
                prior_model_control_disposition_reference=event_reference_from_stored(
                    disposition, runtime_session_id=self.runtime_session_id
                ),
                pending_interaction_frontier_fingerprint=context_fingerprint(
                    "active-run-pending-interaction-frontier:v1", ()
                ),
                open_tool_pair_frontier_fingerprint=context_fingerprint(
                    "active-run-open-tool-pair-frontier:v1", ()
                ),
            )
            self._active_run_monitor_safe_point_lease = lease
            return lease

    async def _borrow_active_run_prompt_steer_safe_point(
        self,
        run_id: str,
        next_model_call_index: int,
    ) -> ActiveRunPromptSteerSafePointLease | None:
        """Reserve the oldest eligible steer for the next provider-input freeze."""

        if (
            next_model_call_index < 2
            or self._lifecycle is not HostSessionLifecycle.OPEN
        ):
            return None
        runtime_session = self.wiring.runtime_wiring.runtime_session
        existing = self._active_run_prompt_steer_safe_point_lease
        if existing is not None:
            if (
                existing.run_id == run_id
                and existing.next_model_call_index == next_model_call_index
            ):
                return existing
            raise HostIngressAdmissionStale(
                "another active-run prompt steer safe point remains installed"
            )
        candidates = tuple(
            item
            for item in runtime_session.prompt_queue_projection_store.pending_items(
                limit=256
            )
            if (
                item.delivery_state == "accepted_pending"
                and item.requested_delivery_mode in {"auto", "steer"}
                or item.delivery_state == "steer_reserved"
                and item.reservation is not None
                and item.resolved_delivery_mode == "steer"
                and item.reservation.target_run_id == run_id
            )
        )
        if not candidates:
            return None
        item = candidates[0]
        if item.delivery_state == "accepted_pending":
            item = await runtime_session.prompt_queue_mutation_service.reserve(
                queue_item_id=item.queue_item_id,
                reservation_kind="steer",
                target_run_id=run_id,
                target_safe_point=(
                    "after_tool_results_before_followup_model_input_freeze"
                ),
                command_id=(
                    f"queue-steer-reserve:{item.queue_item_id}:{item.item_revision + 1}"
                ),
                event_context=(
                    runtime_session.prompt_queue_mutation_service.source_event_context(
                        item.queue_item_id
                    )
                ),
            )
        reservation = item.reservation
        from pulsara_agent.runtime.terminal_application.prompt_queue import (
            user_steer_event_id,
            user_steer_message_id,
        )

        content = item.prepared_content
        if (
            reservation is None
            or content is None
            or content.canonical_byte_count > 64 * 1024
        ):
            return None
        try:
            canonical_text = await runtime_session.prompt_queue_mutation_service.materialize_content_text(
                item.queue_item_id
            )
        except (TimeoutError, ValueError):
            return None
        with (
            self._ingress_coordinator.authority_guard(),
            runtime_session.write_coordinator.lock,
        ):
            service = self.wiring.run_activation_service
            if service is None:
                raise RuntimeError("runtime composition lacks its activation service")
            active_state = service.active_safe_point_state(run_id)
            current_item = runtime_session.prompt_queue_projection_store.item(
                item.queue_item_id
            )
            if (
                active_state is None
                or self.active_run_id != run_id
                or not self._ingress_coordinator.can_borrow_active_run_notifications()
                or active_state.has_pending_interaction
                or active_state.has_pending_tool_calls
                or not active_state.terminal_open
                or not active_state.termination_intent_absent
                or current_item != item
            ):
                return None
            previous_index = (
                active_state.latest_model_control_disposition_model_call_index
            )
            disposition = runtime_session.event_log.get_by_id(
                active_state.latest_model_control_disposition_event_id or ""
            )
            if (
                previous_index is None
                or previous_index >= next_model_call_index
                or not isinstance(disposition, ModelCallControlDispositionResolvedEvent)
                or disposition.model_call_index != previous_index
                or disposition.run_id != run_id
                or disposition.disposition is not ModelCallControlDisposition.ACCEPTED
            ):
                return None
            previous_end = runtime_session.event_log.get_by_id(
                disposition.model_call_end_event_id
            )
            run_start = runtime_session.event_log.get_by_id(
                active_state.run_start_event_id
            )
            queue_head = runtime_session.event_log.get_by_id(item.head_event_id)
            if (
                not isinstance(previous_end, ModelCallEndEvent)
                or previous_end.outcome != "completed"
                or not isinstance(run_start, RunStartEvent)
                or not isinstance(queue_head, PromptQueueReservationInstalledEvent)
            ):
                return None
            host_state = self._ingress_coordinator.state_fact()
            message_id = user_steer_message_id(
                queue_item_id=item.queue_item_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
            )
            expected_event_id = user_steer_event_id(
                queue_item_id=item.queue_item_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
            )
            lease = ActiveRunPromptSteerSafePointLease(
                lease_id=context_fingerprint(
                    "active-run-prompt-steer-safe-point-lease:v1",
                    (
                        run_id,
                        next_model_call_index,
                        item.row_fingerprint,
                        host_state.state_fingerprint,
                    ),
                ).replace("sha256:", "active_steer_lease:"),
                runtime_session_id=self.runtime_session_id,
                run_id=run_id,
                next_model_call_index=next_model_call_index,
                queue_item_id=item.queue_item_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                command_id=(
                    f"queue-steer-commit:{item.queue_item_id}:"
                    f"{reservation.reservation_generation}"
                ),
                message_id=message_id,
                expected_user_steer_event_id=expected_event_id,
                text=canonical_text,
                content_semantic_fingerprint=content.content_semantic_fingerprint,
                queue_item_head_event_reference=event_reference_from_stored(
                    queue_head, runtime_session_id=self.runtime_session_id
                ),
                queue_item_head_candidate_payload_fingerprint=(
                    item.head_candidate_payload_fingerprint
                ),
                queue_item_revision=item.item_revision,
                queue_account_revision=item.account_revision,
                host_state_generation=host_state.state_generation,
                close_intent_revision=host_state.close_intent_revision,
                stop_intent_revision=self._stop_intent_revision,
                termination_intent_revision=self._termination_intent_revision,
                active_segment_id=active_state.segment_id,
                active_segment_generation=active_state.segment_generation,
                llm_lifecycle_generation=(
                    runtime_session.model_stream_execution_registry.generation + 1
                ),
                run_start_event_reference=event_reference_from_stored(
                    run_start, runtime_session_id=self.runtime_session_id
                ),
                previous_model_call_end_event_reference=event_reference_from_stored(
                    previous_end, runtime_session_id=self.runtime_session_id
                ),
                prior_model_control_disposition_reference=event_reference_from_stored(
                    disposition, runtime_session_id=self.runtime_session_id
                ),
            )
            if expected_event_id != user_steer_event_id(
                queue_item_id=lease.queue_item_id,
                reservation_fingerprint=lease.reservation_fingerprint,
            ):
                raise AssertionError("stable UserSteer identity construction drifted")
            self._active_run_prompt_steer_safe_point_lease = lease
            return lease

    @contextmanager
    def _active_run_prompt_steer_commit_guard(self, **_kwargs):
        with self._ingress_coordinator.authority_guard():
            yield

    def _release_active_run_prompt_steer_safe_point(
        self, lease: ActiveRunPromptSteerSafePointLease
    ) -> None:
        if self._active_run_prompt_steer_safe_point_lease == lease:
            self._active_run_prompt_steer_safe_point_lease = None

    def _validate_active_run_prompt_steer_safe_point(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard,
        run_id: str,
    ) -> None:
        """Revalidate queue, Host, segment, and same-batch joins under writer lock."""

        lease = self._active_run_prompt_steer_safe_point_lease
        if lease is None or lease.run_id != run_id:
            raise HostIngressAdmissionStale(
                "active-run prompt steer safe-point lease is unavailable"
            )
        runtime_session = self.wiring.runtime_wiring.runtime_session
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        active_state = service.active_safe_point_state(run_id)
        host_state = self._ingress_coordinator.state_fact()
        item = runtime_session.prompt_queue_projection_store.item(lease.queue_item_id)
        if (
            self._lifecycle is not HostSessionLifecycle.OPEN
            or active_state is None
            or self.active_run_id != run_id
            or active_state.has_pending_interaction
            or active_state.has_pending_tool_calls
            or not active_state.terminal_open
            or not active_state.termination_intent_absent
            or active_state.segment_id != lease.active_segment_id
            or active_state.segment_generation != lease.active_segment_generation
            or self._stop_intent_revision != lease.stop_intent_revision
            or self._termination_intent_revision != lease.termination_intent_revision
            or host_state.state_generation != lease.host_state_generation
            or host_state.close_intent_revision != lease.close_intent_revision
            or runtime_session.model_stream_execution_registry.generation
            != lease.llm_lifecycle_generation
            or item is None
            or item.delivery_state != "steer_reserved"
            or item.item_revision != lease.queue_item_revision
            or item.account_revision != lease.queue_account_revision
            or item.head_event_id != lease.queue_item_head_event_reference.event_id
            or item.head_candidate_payload_fingerprint
            != lease.queue_item_head_candidate_payload_fingerprint
            or item.reservation is None
            or item.reservation.reservation_fingerprint != lease.reservation_fingerprint
        ):
            raise HostIngressAdmissionStale(
                "active-run prompt steer authority became stale"
            )
        if (
            guard.runtime_session_id != lease.runtime_session_id
            or guard.run_start_event_reference != lease.run_start_event_reference
            or guard.active_segment_id != lease.active_segment_id
            or guard.active_segment_generation != lease.active_segment_generation
            or guard.expected_host_state_generation != lease.host_state_generation
            or guard.expected_next_model_call_index != lease.next_model_call_index
            or guard.expected_llm_lifecycle_generation != lease.llm_lifecycle_generation
            or guard.expected_termination_intent_revision
            != lease.termination_intent_revision
            or guard.expected_stop_intent_revision != lease.stop_intent_revision
            or guard.expected_close_intent_revision != lease.close_intent_revision
            or guard.prior_model_control_disposition_reference
            != lease.prior_model_control_disposition_reference
            or guard.previous_model_call_end_event_reference
            != lease.previous_model_call_end_event_reference
            or guard.queue_item_id != lease.queue_item_id
            or guard.queue_reservation_fingerprint != lease.reservation_fingerprint
            or guard.queue_item_head_event_reference
            != lease.queue_item_head_event_reference
            or guard.queue_item_head_candidate_payload_fingerprint
            != lease.queue_item_head_candidate_payload_fingerprint
            or guard.expected_queue_item_revision != lease.queue_item_revision
            or guard.expected_queue_account_revision != lease.queue_account_revision
            or start_event.model_call_index != lease.next_model_call_index
        ):
            raise HostIngressAdmissionStale(
                "active-run prompt steer commit guard drifted"
            )
        appends = tuple(
            event
            for event in candidate_events
            if isinstance(event, ProviderInputAppendCommittedEvent)
        )
        steers = tuple(
            event
            for event in candidate_events
            if isinstance(event, UserSteerCommittedEvent)
        )
        queue_commits = tuple(
            event
            for event in candidate_events
            if isinstance(event, PromptQueueCommittedToProviderInputEvent)
        )
        if (
            len(appends) != 1
            or len(steers) != 1
            or len(queue_commits) != 1
            or appends[0].prepared_provider_input_candidate_fingerprint
            != guard.prepared_provider_input_append_fingerprint
            or steers[0].id != guard.expected_user_steer_event_id
            or steers[0].queue_item_id != lease.queue_item_id
            or steers[0].source_reservation_fingerprint != lease.reservation_fingerprint
            or queue_commits[0].transition.queue_item_id != lease.queue_item_id
            or queue_commits[0].source_reservation_fingerprint
            != lease.reservation_fingerprint
            or queue_commits[0].provider_input_append_event_identity
            != stable_event_identity(
                appends[0], runtime_session_id=self.runtime_session_id
            )
            or queue_commits[0].user_steer_event_identity
            != stable_event_identity(
                steers[0], runtime_session_id=self.runtime_session_id
            )
        ):
            raise HostIngressAdmissionStale(
                "active-run prompt steer candidate batch drifted"
            )

    @contextmanager
    def _active_run_monitor_safe_point_commit_guard(
        self,
        **_kwargs,
    ):
        """Keep permission/close authority stable through ModelStart commit."""

        with self._ingress_coordinator.authority_guard():
            yield

    def _terminal_monitor_autonomy_allowed(self) -> bool:
        policy = self.current_permission_policy()
        return (
            policy.profile is not PermissionProfile.READ_ONLY
            and policy.terminal is not TerminalAccess.OFF
        )

    def _release_active_run_monitor_safe_point(
        self, lease: ActiveRunMonitorSafePointLease
    ) -> None:
        if self._active_run_monitor_safe_point_lease == lease:
            self._active_run_monitor_safe_point_lease = None

    def _validate_active_run_monitor_safe_point(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard,
        run_id: str,
    ) -> None:
        """Revalidate the borrowed authority under RuntimeSession's writer lock."""

        lease = self._active_run_monitor_safe_point_lease
        if lease is None or lease.run_id != run_id:
            raise HostIngressAdmissionStale(
                "active-run monitor safe-point lease is unavailable"
            )
        runtime_session = self.wiring.runtime_wiring.runtime_session
        host_state = self._ingress_coordinator.state_fact()
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        active_state = service.active_safe_point_state(run_id)
        if (
            self._lifecycle is not HostSessionLifecycle.OPEN
            or active_state is None
            or self.active_run_id != run_id
            or not self._ingress_coordinator.can_borrow_active_run_notifications()
            or active_state.has_pending_interaction
            or active_state.has_pending_tool_calls
            or not active_state.terminal_open
            or not active_state.termination_intent_absent
            or active_state.segment_id != lease.active_segment_id
            or active_state.segment_generation != lease.active_segment_generation
            or self._stop_intent_revision != lease.stop_intent_revision
            or self._termination_intent_revision != lease.termination_intent_revision
            or host_state.state_generation != lease.host_state_generation
            or host_state.permission_policy_revision != lease.permission_policy_revision
            or host_state.permission_policy_fingerprint
            != lease.permission_policy_fingerprint
            or host_state.close_intent_revision != lease.close_intent_revision
            or runtime_session.model_stream_execution_registry.generation
            != lease.llm_lifecycle_generation
        ):
            raise HostIngressAdmissionStale(
                "active-run monitor safe-point host authority became stale"
            )
        notification_state = (
            runtime_session.terminal_notification_store.projection_snapshot()
        )
        chain = runtime_session.terminal_notification_store.autonomy_chain_snapshot(
            lease.wake_chain_id
        )
        current = runtime_session.terminal_notification_store.pending_notifications(
            include_unmonitored_completions=False,
            maximum_items=8,
            wake_chain_id=lease.wake_chain_id,
            include_automatic_delivery_deferred=False,
        )
        source_ids = tuple(event.id for event in lease.source_events)
        current_by_id = {item.source_event.id: item for item in current}
        current_selected = tuple(current_by_id.get(item) for item in source_ids)
        if (
            notification_state.state_fingerprint != lease.notification_state_fingerprint
            or any(item is None for item in current_selected)
            or tuple(
                item.process_head_fingerprint
                for item in current_selected
                if item is not None
            )
            != lease.selected_notification_head_fingerprints
            or chain.state.state_fingerprint
            != lease.expected_autonomy_chain_state_fingerprint
            or chain.state.last_automatic_delivery_ordinal + 1
            != lease.proposed_automatic_delivery_ordinal
        ):
            raise HostIngressAdmissionStale(
                "active-run monitor notification authority became stale"
            )
        if (
            guard.runtime_session_id != lease.runtime_session_id
            or guard.active_segment_id != lease.active_segment_id
            or guard.active_segment_generation != lease.active_segment_generation
            or guard.expected_host_state_generation != lease.host_state_generation
            or guard.expected_next_model_call_index != lease.next_model_call_index
            or guard.expected_llm_lifecycle_generation != lease.llm_lifecycle_generation
            or guard.expected_termination_intent_revision
            != lease.termination_intent_revision
            or guard.expected_stop_intent_revision != lease.stop_intent_revision
            or guard.expected_close_intent_revision != lease.close_intent_revision
            or guard.expected_permission_policy_revision
            != lease.permission_policy_revision
            or guard.expected_permission_policy_fingerprint
            != lease.permission_policy_fingerprint
            or guard.expected_notification_state_fingerprint
            != lease.notification_state_fingerprint
            or guard.expected_selected_notification_head_fingerprints
            != lease.selected_notification_head_fingerprints
            or guard.expected_autonomy_chain_state_fingerprint
            != lease.expected_autonomy_chain_state_fingerprint
            or guard.run_start_event_reference != lease.run_start_event_reference
            or guard.previous_model_call_end_event_reference
            != lease.previous_model_call_end_event_reference
            or guard.prior_model_control_disposition_reference
            != lease.prior_model_control_disposition_reference
            or guard.expected_pending_interaction_frontier_fingerprint
            != lease.pending_interaction_frontier_fingerprint
            or guard.expected_open_tool_pair_frontier_fingerprint
            != lease.open_tool_pair_frontier_fingerprint
            or start_event.model_call_index != lease.next_model_call_index
        ):
            raise HostIngressAdmissionStale(
                "active-run monitor safe-point commit guard drifted"
            )
        appends = tuple(
            event
            for event in candidate_events
            if isinstance(event, ProviderInputAppendCommittedEvent)
        )
        if (
            len(appends) != 1
            or appends[0].prepared_provider_input_candidate_fingerprint
            != guard.prepared_provider_input_append_fingerprint
        ):
            raise HostIngressAdmissionStale(
                "active-run monitor prepared ProviderInput append drifted"
            )
        disposition = tuple(
            event
            for event in candidate_events
            if isinstance(event, TerminalProcessObservationDeliveryDispositionEvent)
            and event.outcome == "active_run_safe_point"
        )
        expected_refs = tuple(
            event_reference_from_stored(
                event, runtime_session_id=self.runtime_session_id
            )
            for event in lease.source_events
        )
        if (
            len(disposition) != 1
            or disposition[0].observation_source_references != expected_refs
            or disposition[0].model_call_start_event_identity
            != stable_event_identity(
                start_event, runtime_session_id=self.runtime_session_id
            )
            or disposition[0].autonomy_delivery
            != start_event.active_run_monitor_delivery.autonomy_delivery
        ):
            raise HostIngressAdmissionStale(
                "active-run monitor disposition/ModelStart join failed"
            )

    async def _defer_terminal_notifications(
        self,
        selected,
        *,
        reason: Literal[
            "wake_budget_exhausted",
            "autonomy_permission_disabled",
            "host_waiting_user",
        ],
    ) -> None:
        if not selected:
            return
        source_events = tuple(item.source_event for item in selected)
        source_references = tuple(
            sorted(
                (
                    event_reference_from_stored(
                        event,
                        runtime_session_id=self.runtime_session_id,
                    )
                    for event in source_events
                ),
                key=lambda item: (item.sequence, item.event_id),
            )
        )
        first = source_events[0]
        candidate = TerminalProcessObservationDeliveryDeferredEvent(
            id=context_fingerprint(
                "terminal-notification-delivery-deferred-id:v1",
                (reason, tuple(item.event_id for item in source_references)),
            ).replace("sha256:", "terminal_notification_deferred:"),
            run_id=first.run_id,
            turn_id=first.turn_id,
            reply_id=first.reply_id,
            observation_source_references=source_references,
            reason=reason,
        )
        await self.wiring.runtime_wiring.runtime_session.commit_accepted_event(
            candidate
        )

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
        owner: PreparedRunOwnerReservationKey,
        generation: int,
        frozen_execution_surface: FrozenCapabilityExecutionSurface,
        state: str = "boundary_owned",
    ) -> RunExecutionHandleSet:
        return RunExecutionHandleSet(
            handle_id=f"run_execution_handles:{uuid4().hex}",
            handle_generation=generation,
            owner=owner,
            state=state,  # type: ignore[arg-type]
            mcp_installation=self.wiring.runtime_wiring.mcp_installation,
            capability_runtime=self.wiring.agent_runtime.capability_runtime,
            tool_registry=self.wiring.agent_runtime.tool_executor.registry,
            frozen_execution_surface=frozen_execution_surface,
        )

    def _register_committed_host_run_owner(
        self,
        *,
        committed: CommittedHostRunEntry,
        prepared: PreparedNewRunBoundary,
    ) -> None:
        self.wiring.runtime_wiring.runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
            committed.run_start_event
        )
        attempt = self._boundary_attempt
        if attempt is None or attempt.prepared_activation is None:
            raise RuntimeError("committed run lost its prepared activation owner")
        prepared_activation = attempt.prepared_activation
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        service.initialize_committed_state(
            prepared_activation=prepared_activation,
            committed=committed,
            plan_snapshot=prepared.plan_snapshot,
            capability_resolve_basis=prepared.capability_basis,
            frozen_execution_surface=prepared.frozen_execution_surface,
        )
        if attempt is None or attempt.execution_handles is None:
            raise RuntimeError("committed run lost its attempt-owned execution handles")
        handles = attempt.execution_handles
        if handles.frozen_execution_surface is not prepared.frozen_execution_surface:
            raise RuntimeError("committed run execution surface drifted after freeze")
        reservation_key = attempt.run_owner_reservation_key
        if reservation_key is None:
            raise RuntimeError("committed run lost its prepared owner reservation")
        promoted_handles = self._run_activation_service.promote_committed_owner(
            reservation_key=reservation_key,
            committed=committed,
            prepared_activation=prepared_activation,
        )
        if promoted_handles is not handles:
            raise RuntimeError("committed run promotion changed execution handles")
        attempt.run_owner_reservation_key = None
        attempt.execution_handles = None
        attempt.prepared_activation = None
        service.install_committed_execution_handle(
            run_id=committed.run_start_event.run_id,
            handle_id=handles.handle_id,
            borrow_authority=handles.borrow_authority,
        )

    async def _adopt_committed_host_run(
        self,
        *,
        committed: CommittedHostRunEntry,
        prepared: PreparedNewRunBoundary,
    ) -> None:
        """Install the process owner or durably close the already-started run."""

        try:
            self._register_committed_host_run_owner(
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
                attempt = self._boundary_attempt
                if attempt is None or attempt.prepared_activation is None:
                    raise RuntimeError(
                        "committed RunStart owner recovery lost its prepared activation"
                    ) from ownership_error
                service = self.wiring.run_activation_service
                if service is None:
                    raise RuntimeError(
                        "runtime composition lacks its activation service"
                    ) from ownership_error
                prepared_activation = attempt.prepared_activation
                try:
                    service.initialize_committed_state(
                        prepared_activation=prepared_activation,
                        committed=committed,
                        plan_snapshot=prepared.plan_snapshot,
                        capability_resolve_basis=prepared.capability_basis,
                        frozen_execution_surface=prepared.frozen_execution_surface,
                    )
                except RuntimeError as initialization_error:
                    if "committed RunWorkingSet" not in str(initialization_error):
                        raise
                # RunStart is already durable. If the boundary-to-registry
                # handoff itself failed before promotion, recover that exact
                # prepared reservation into the stable owner before running
                # terminal maintenance. No terminal path may fall back to an
                # unowned working state.
                run_id = committed.run_start_event.run_id
                if not self._run_activation_service.has_run_owner(run_id):
                    if (
                        attempt is None
                        or attempt.run_owner_reservation_key is None
                        or attempt.execution_handles is None
                    ):
                        raise RuntimeError(
                            "committed RunStart owner recovery lost its prepared authority"
                        ) from ownership_error
                    handles = self._run_activation_service.promote_committed_owner(
                        reservation_key=attempt.run_owner_reservation_key,
                        committed=committed,
                        prepared_activation=prepared_activation,
                    )
                    attempt.run_owner_reservation_key = None
                    attempt.execution_handles = None
                    attempt.prepared_activation = None
                    service.install_committed_execution_handle(
                        run_id=run_id,
                        handle_id=handles.handle_id,
                        borrow_authority=handles.borrow_authority,
                    )
                stop_request = service.pending_stop_request(run_id)
                if stop_request is not None:
                    await service.abort_pending_run(
                        run_id,
                        reason=stop_request.reason,
                    )
                else:
                    await service.fail_pending_run(
                        run_id,
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
        run_id: str,
        stop_reason: RunStopReason | None = None,
        error_message: str | None = None,
        abort_reason: AbortKind | None = None,
    ) -> AgentRunResult:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        try:
            if abort_reason is not None:
                result = await service.abort_pending_run(run_id, reason=abort_reason)
            else:
                if stop_reason is None or error_message is None:
                    raise ValueError("failure terminalization requires typed reason")
                result = await service.fail_pending_run(
                    run_id,
                    stop_reason=stop_reason,
                    error_message=error_message,
                )
        except EventPublicationAfterCommitError:
            if not service.is_finalized(run_id):
                raise
            result = agent_run_result_from_terminal_outcome(
                await service.wait_run_completion(run_id)
            )
        return result

    async def _prepare_and_commit_new_run_boundary(
        self,
        *,
        ingress_owner: HostIngressAttemptOwner,
        user_input: str,
        active_skill_names: frozenset[str],
        identity: HostRunBoundaryIdentityFact,
        queued_prompt_delivery: QueuedPromptRunDelivery | None = None,
    ) -> tuple[AgentRunDraft, CommittedHostRunEntry, tuple[AgentEvent, ...]]:
        """The sole PRE_RUN coordinator, called with ``_run_lock`` held."""

        self._set_boundary_phase(HostRunBoundaryPhase.ADMISSION)
        self._require_new_run_admission("starting a new turn")
        if not self._subagent_dangling_repair_done:
            # Repair can append parent-graph facts, so it must complete before
            # transcript/watermark freeze rather than inside the draft builder.
            await self.repair_dangling_children_once()
        self._set_boundary_phase(HostRunBoundaryPhase.CONTRACT_RESOLUTION)
        run_model_target = self.wiring.agent_runtime.resolve_run_model_target()
        permission_snapshot = self._resolve_new_run_permission_snapshot(
            run_id=identity.run_id
        )
        await self._ingress_coordinator.update_permission_policy(
            context_fingerprint(
                "host-ingress-permission-snapshot:v1",
                permission_snapshot.to_event_fields(),
            )
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
        authority = self._freeze_new_run_boundary_inputs(
            ingress_owner=ingress_owner,
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
        plan_snapshot = authority.plan
        transcript_fact = authority.transcript
        capability_basis = authority.capability_basis
        pending_audits = tuple(
            self.wiring.runtime_wiring.runtime_session.pending_mcp_installation_audit_events(
                EventContext(
                    run_id=identity.run_id,
                    turn_id=identity.turn_id,
                    reply_id=identity.reply_id,
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
            run_id=identity.run_id,
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
            mcp_installation_fact=authority.mcp_installation,
            owned_transcript_messages=tuple(
                message.model_copy(deep=True) for message in prior_messages
            ),
            transcript_fact=transcript_fact,
            capability_basis=capability_basis,
            current_user_message=authority.current_user_message,
            host_run_ingress=authority.host_run_ingress,
            host_ingress_admission_proof=authority.host_ingress_admission_proof,
            ingress_owner=ingress_owner,
            run_start_event_id=run_start_event_id,
            terminal_run_end_event_id=authority.terminal_run_end_event_id,
            new_run_boundary=authority.new_run_boundary,
            frozen_execution_surface=frozen_surface,
            pending_mcp_audits=pending_audits,
            long_horizon=long_horizon,
            diagnostics=(),
            queued_prompt_delivery=queued_prompt_delivery,
        )
        attempt = self._boundary_attempt
        if attempt is None:
            raise RuntimeError("new-run boundary lost its process owner")
        attempt.execution_handles = self._new_execution_handles(
            owner=(
                reservation_key := build_prepared_run_owner_reservation_key(
                    runtime_session_id=self.runtime_session_id,
                    run_id=identity.run_id,
                    run_start_event_id=run_start_event_id,
                )
            ),
            generation=1,
            frozen_execution_surface=frozen_surface,
        )
        attempt.run_owner_reservation_key = reservation_key
        self._run_activation_service.reserve_prepared_owner(
            key=reservation_key,
            execution_handles=attempt.execution_handles,
            reservation_generation=1,
        )
        if attempt.prepared_activation is None:
            raise RuntimeError("new-run boundary lost its prepared activation owner")
        draft, committed, stored = await self._commit_new_run_entry(
            prepared_activation=attempt.prepared_activation,
            prepared=prepared,
        )
        self._set_boundary_phase(HostRunBoundaryPhase.ACTIVATION)
        return draft, committed, stored

    async def run_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> HostActivationResult:
        self._host_event_loop = asyncio.get_running_loop()
        self._terminal_notification_dispatch_enabled = True
        self._raise_if_not_open("starting a new turn")
        self._raise_if_pending_interaction("starting a new turn")
        ingress_id = f"host_ingress:{uuid4().hex}"
        try:
            return await self._ingress_coordinator.submit(
                kind="human",
                payload=user_input,
                ingress_id=ingress_id,
                reject_if_busy=True,
                runner=lambda owner: self._run_human_ingress(
                    owner,
                    user_input=user_input,
                    active_skill_names=active_skill_names or frozenset(),
                ),
            )
        except HostIngressCapacityError as exc:
            raise HostSessionBusyError(
                "host session already has an active run"
            ) from exc

    async def _run_human_ingress(
        self,
        ingress_owner: HostIngressAttemptOwner,
        *,
        user_input: str,
        active_skill_names: frozenset[str],
        queue_item_id: str | None = None,
    ) -> HostActivationResult:
        self._raise_if_not_open("starting an admitted turn")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("starting a new turn")
        self._raise_if_active_run()
        identity = self._new_run_boundary_identity(
            kind=(
                "pre_runtime_request" if ingress_owner.kind == "runtime" else "pre_run"
            ),
        )
        queued_prompt_delivery = None
        if queue_item_id is not None:
            queue_service = (
                self.wiring.runtime_wiring.runtime_session.prompt_queue_mutation_service
            )
            source_context = queue_service.source_event_context(queue_item_id)
            reserved = await queue_service.reserve(
                queue_item_id=queue_item_id,
                reservation_kind="follow_up",
                target_run_id=identity.run_id,
                target_safe_point="next_host_run_boundary",
                command_id=f"queue-reserve:{queue_item_id}:{identity.run_id}",
                event_context=source_context,
            )
            reservation = reserved.reservation
            content = reserved.prepared_content
            if reservation is None or content is None:
                raise RuntimeError("queued follow-up lost its reserved content")
            encoded_user_input = user_input.encode("utf-8")
            if (
                len(encoded_user_input) != content.canonical_byte_count
                or f"sha256:{sha256(encoded_user_input).hexdigest()}"
                != content.canonical_payload_sha256
            ):
                await queue_service.reject_delivery(
                    queue_item_id=queue_item_id,
                    command_id=f"queue-reject:{queue_item_id}:{identity.run_id}",
                    event_context=source_context,
                    reason="content_unavailable",
                    reservation_fingerprint=reservation.reservation_fingerprint,
                )
                raise RuntimeError("queued follow-up content cannot be materialized")
            queued_prompt_delivery = QueuedPromptRunDelivery(
                queue_item_id=queue_item_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                reservation_generation=reservation.reservation_generation,
                content_semantic_fingerprint=content.content_semantic_fingerprint,
            )
        boundary_input = NewRunBoundaryInput(
            identity=identity,
            user_input=user_input,
            active_skill_names=active_skill_names or frozenset(),
            host_session_id=self.host_session_id,
            conversation_id=self.conversation_id,
            ingress_owner=ingress_owner,
            queued_prompt_delivery=queued_prompt_delivery,
        )
        task = self._create_owned_boundary_task(
            lambda: self._run_turn_pipeline(
                boundary_input=boundary_input,
            ),
            preparing_identity=identity,
            prepare_initial_activation=True,
        )
        try:
            try:
                result = await asyncio.shield(task)
                if self.pending_interaction is not None:
                    await self._ingress_coordinator.mark_waiting_user(
                        resume_match_key=_pending_interaction_match_key(
                            self.pending_interaction
                        )
                    )
                return result
            except asyncio.CancelledError:
                if (
                    not task.cancelled()
                    or identity.run_id not in self._boundary_stop_requested_run_ids
                ):
                    raise
                service = self.wiring.run_activation_service
                if service is None:
                    raise RuntimeError(
                        "runtime composition lacks its activation service"
                    )
                if not self._run_activation_service.has_run_owner(identity.run_id):
                    raise
                terminal = await self._run_activation_service.wait_run_completion(
                    identity.run_id
                )
                return agent_run_result_from_terminal_outcome(terminal)
        except BaseException:
            if queued_prompt_delivery is not None:
                try:
                    await self._settle_failed_queued_prompt_delivery(
                        queued_prompt_delivery,
                    )
                except BaseException:
                    # Preserve the boundary failure as the primary outcome.  A
                    # reservation whose settlement cannot be durably confirmed
                    # remains visible to reopen/repair instead of replacing the
                    # causal failure with a second writer exception.
                    pass
            raise
        finally:
            self._boundary_stop_requested_run_ids.discard(identity.run_id)

    async def run_queued_follow_up(self, queue_item_id: str) -> HostActivationResult:
        """Consume one durable queue item through the ordinary Host ingress owner."""

        self._host_event_loop = asyncio.get_running_loop()
        runtime = self.wiring.runtime_wiring.runtime_session
        item = runtime.prompt_queue_projection_store.item(queue_item_id)
        if item is None:
            raise KeyError(queue_item_id)
        if item.delivery_state != "accepted_pending":
            raise RuntimeError("queued follow-up is no longer pending")
        content = item.prepared_content
        if content is None:
            await runtime.prompt_queue_mutation_service.reject_delivery(
                queue_item_id=queue_item_id,
                command_id=f"queue-reject:{queue_item_id}:content",
                event_context=(
                    runtime.prompt_queue_mutation_service.source_event_context(
                        queue_item_id
                    )
                ),
                reason="content_unavailable",
            )
            raise RuntimeError("queued follow-up content is unavailable")
        try:
            text = await runtime.prompt_queue_mutation_service.materialize_content_text(
                queue_item_id
            )
        except (TimeoutError, ValueError) as exc:
            await runtime.prompt_queue_mutation_service.reject_delivery(
                queue_item_id=queue_item_id,
                command_id=f"queue-reject:{queue_item_id}:hydrate",
                event_context=(
                    runtime.prompt_queue_mutation_service.source_event_context(
                        queue_item_id
                    )
                ),
                reason="content_unavailable",
            )
            raise RuntimeError("queued follow-up content is unavailable") from exc
        ingress_id = context_fingerprint(
            "queued-follow-up-host-ingress:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "queue_item_id": item.queue_item_id,
                "head_event_id": item.head_event_id,
                "content_semantic_fingerprint": (content.content_semantic_fingerprint),
            },
        ).replace("sha256:", "host_ingress:")
        return await self._ingress_coordinator.submit(
            kind="human",
            payload=(queue_item_id, content.content_semantic_fingerprint),
            ingress_id=ingress_id,
            reject_if_busy=False,
            allow_deferred_while_waiting=True,
            runner=lambda owner: self._run_human_ingress(
                owner,
                user_input=text,
                active_skill_names=frozenset(),
                queue_item_id=queue_item_id,
            ),
        )

    async def _settle_failed_queued_prompt_delivery(
        self,
        delivery: QueuedPromptRunDelivery,
    ) -> None:
        runtime = self.wiring.runtime_wiring.runtime_session
        if runtime.reconciliation_required:
            return
        item = runtime.prompt_queue_projection_store.item(delivery.queue_item_id)
        if (
            item is None
            or item.delivery_state != "follow_up_reserved"
            or item.reservation is None
            or item.reservation.reservation_fingerprint
            != delivery.reservation_fingerprint
        ):
            return
        event_context = runtime.prompt_queue_mutation_service.source_event_context(
            delivery.queue_item_id
        )
        if self._lifecycle is not HostSessionLifecycle.OPEN:
            await runtime.prompt_queue_mutation_service.reject_delivery(
                queue_item_id=delivery.queue_item_id,
                command_id=(
                    f"queue-reject:{delivery.queue_item_id}:"
                    f"{delivery.reservation_generation}"
                ),
                event_context=event_context,
                reason="session_closing",
                reservation_fingerprint=delivery.reservation_fingerprint,
            )
            return
        await runtime.prompt_queue_mutation_service.release_reservation(
            queue_item_id=delivery.queue_item_id,
            reservation_fingerprint=delivery.reservation_fingerprint,
            command_id=(
                f"queue-release:{delivery.queue_item_id}:"
                f"{delivery.reservation_generation}"
            ),
            event_context=event_context,
            reason="preflight_retryable",
        )

    async def _run_turn_pipeline(
        self,
        *,
        boundary_input: NewRunBoundaryInput,
    ) -> HostActivationResult:
        async with self._run_lock:
            self._reserve_presentation_run_growth(boundary_input.identity.run_id)
            try:
                for attempt_index in range(_MAX_RUN_SEED_REFREEZE_ATTEMPTS):
                    try:
                        (
                            draft,
                            committed,
                            _stored,
                        ) = await self._prepare_and_commit_new_run_boundary(
                            ingress_owner=boundary_input.ingress_owner,
                            user_input=boundary_input.user_input,
                            active_skill_names=boundary_input.active_skill_names,
                            identity=boundary_input.identity,
                            queued_prompt_delivery=(
                                boundary_input.queued_prompt_delivery
                            ),
                        )
                    except BaseException as exc:
                        if (
                            attempt_index + 1 >= _MAX_RUN_SEED_REFREEZE_ATTEMPTS
                            or not _caused_by(exc, RunSeedSourceStale)
                        ):
                            raise
                        self._reset_boundary_after_run_seed_source_stale()
                        continue
                    break
                else:  # pragma: no cover - bounded loop always breaks or raises.
                    raise AssertionError("run-seed re-freeze loop exhausted")
                self._prepare_committed_host_activation(
                    boundary_input.identity.run_id,
                    committed,
                )
                self._complete_boundary_attempt_after_activation()
                return await self._run_initial_owned(
                    run_id=boundary_input.identity.run_id,
                    draft=draft,
                    committed=committed,
                    active_skill_names=boundary_input.active_skill_names,
                )
            except BaseException as exc:
                await self._terminalize_post_commit_pipeline_failure(
                    boundary_input.identity.run_id, exc
                )
                self._release_uncommitted_presentation_run_growth(
                    boundary_input.identity.run_id
                )
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
        if attempt.run_owner_reservation_key is not None:
            self._run_activation_service.release_prepared_owner(
                attempt.run_owner_reservation_key,
                outcome="none",
            )
        authority = attempt.prepared_authority
        prepared_activation = attempt.prepared_activation
        service = self.wiring.run_activation_service
        if authority is None or prepared_activation is None or service is None:
            raise RuntimeError("run-seed source retry lost its prepared owner")
        next_generation = prepared_activation.generation + 1
        prepared_activation.release()
        attempt.prepared_activation = service.prepare_boundary_activation(
            identity=authority.identity,
            owner_task=attempt.owner_task,
            generation=next_generation,
        )
        attempt.phase = HostRunBoundaryPhase.ADMISSION
        attempt.prepared_authority = None
        attempt.run_owner_reservation_key = None
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
        self._host_event_loop = asyncio.get_running_loop()
        self._raise_if_not_open("starting a new turn")
        self._raise_if_pending_interaction("starting a new turn")
        observer = _StreamObserver()
        ingress_id = f"host_ingress:{uuid4().hex}"
        identity = self._new_run_boundary_identity()

        async def _submit() -> None:
            try:
                await self._ingress_coordinator.submit(
                    kind="human",
                    payload=user_input,
                    ingress_id=ingress_id,
                    reject_if_busy=True,
                    runner=lambda owner: self._run_human_stream_ingress(
                        owner,
                        observer=observer,
                        user_input=user_input,
                        active_skill_names=active_skill_names or frozenset(),
                        identity=identity,
                    ),
                )
            except HostIngressCapacityError as exc:
                raise HostSessionBusyError(
                    "host session already has an active run"
                ) from exc

        task = self._create_owned_boundary_task(
            _submit,
            preparing_identity=identity,
            prepare_initial_activation=True,
            observer=observer,
        )
        return _OwnedBoundaryStreamObserver(
            self._observe_owned_boundary_stream(observer=observer, task=task),
            observer,
        )

    async def _run_human_stream_ingress(
        self,
        ingress_owner: HostIngressAttemptOwner,
        *,
        observer: _StreamObserver,
        user_input: str,
        active_skill_names: frozenset[str],
        identity: HostRunBoundaryIdentityFact,
    ) -> None:
        attempt = self._take_boundary_execution_ownership()
        try:
            self._raise_if_not_open("starting an admitted stream turn")
            if self.stopping_run_id is not None:
                raise HostSessionBusyError("host session is stopping an active run")
            self._raise_if_pending_interaction("starting a new turn")
            active_view = self._run_activation_service.active_host_run_view()
            if (
                self._run_lock.locked()
                or self.active_run_id is not None
                or (active_view is not None and active_view.active_driver_running)
            ):
                raise HostSessionBusyError("host session already has an active run")
            boundary_input = NewRunBoundaryInput(
                identity=identity,
                user_input=user_input,
                active_skill_names=active_skill_names or frozenset(),
                host_session_id=self.host_session_id,
                conversation_id=self.conversation_id,
                ingress_owner=ingress_owner,
            )

            async for event in self._stream_turn_pipeline(
                boundary_input,
            ):
                await observer.emit(event)
            if self.pending_interaction is not None:
                await self._ingress_coordinator.mark_waiting_user(
                    resume_match_key=_pending_interaction_match_key(
                        self.pending_interaction
                    )
                )
        finally:
            self._finish_ingress_owned_boundary(attempt)

    async def _stream_turn_pipeline(
        self,
        boundary_input: NewRunBoundaryInput,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            self._reserve_presentation_run_growth(boundary_input.identity.run_id)
            try:
                (
                    draft,
                    committed,
                    stored,
                ) = await self._prepare_and_commit_new_run_boundary(
                    ingress_owner=boundary_input.ingress_owner,
                    user_input=boundary_input.user_input,
                    active_skill_names=boundary_input.active_skill_names,
                    identity=boundary_input.identity,
                )
                self._prepare_committed_host_activation(
                    boundary_input.identity.run_id,
                    committed,
                )
                self._complete_boundary_attempt_after_activation()
                for event in stored:
                    yield event
                async for event in self._stream_initial_owned(
                    run_id=boundary_input.identity.run_id,
                    draft=draft,
                    committed=committed,
                    active_skill_names=boundary_input.active_skill_names,
                ):
                    yield event
            except BaseException as exc:
                await self._terminalize_post_commit_pipeline_failure(
                    boundary_input.identity.run_id, exc
                )
                self._release_uncommitted_presentation_run_growth(
                    boundary_input.identity.run_id
                )
                raise

    async def _terminalize_post_commit_pipeline_failure(
        self,
        run_id: str,
        exc: BaseException,
    ) -> None:
        service = self._run_activation_service
        view = service.run_view(run_id)
        if view is None or service.is_finalized(run_id):
            return
        # The activation driver may already have frozen the stable RunEnd
        # candidate before surfacing its write error.  That candidate and its
        # finalization carrier remain the unique retry owner; a boundary-level
        # failure handler must not start a second terminalization attempt.
        if view.terminal_state != "open" or view.lifecycle in {
            "terminalizing",
            "reconciliation_required",
        }:
            return
        if isinstance(exc, asyncio.CancelledError):
            _clear_current_task_cancellation()
        await self._terminalize_committed_run_after_boundary_failure(
            run_id=run_id,
            stop_reason=(
                RunStopReason.RUNTIME_PUBLICATION_FAILURE
                if isinstance(exc, EventPublicationAfterCommitError)
                else RunStopReason.RUNTIME_EXECUTION_ERROR
            ),
            error_message=(f"committed Host run pipeline failed: {type(exc).__name__}"),
        )
        if service.is_finalized(run_id):
            self._finish_active_run(run_id)

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

    async def resolve_approval(
        self, resolution: ApprovalResolution
    ) -> HostActivationResult:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        prepared = self._prepare_interaction_resume_attempt(
            pending=pending,
            interaction_kind="approval",
            resolution=resolution,
        )
        return await self._ingress_coordinator.submit(
            kind="resume",
            payload=resolution,
            ingress_id=f"host_ingress_resume:{uuid4().hex}",
            resume_match_key=resolution.approval_id,
            runner=lambda owner: self._run_resume_ingress(
                owner,
                prepared=prepared,
            ),
        )

    async def _run_resume_ingress(
        self,
        _ingress_owner: HostIngressAttemptOwner,
        *,
        prepared: PreparedInteractionResumeAttempt,
        prepare_plan_state: bool = False,
        recover_pending_on_publication_failure: bool = False,
    ) -> HostActivationResult:
        task = self._create_owned_boundary_task(
            lambda: self._resolve_interaction_pipeline(
                prepared=prepared,
                prepare_plan_state=prepare_plan_state,
                recover_pending_on_publication_failure=(
                    recover_pending_on_publication_failure
                ),
            ),
            preparing_identity=prepared.boundary_identity,
        )
        return await asyncio.shield(task)

    async def _resolve_interaction_pipeline(
        self,
        *,
        prepared: PreparedInteractionResumeAttempt,
        prepare_plan_state: bool = False,
        recover_pending_on_publication_failure: bool = False,
    ) -> HostActivationResult:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=_pending_interaction_match_key(
                    prepared.pending_public_view  # type: ignore[arg-type]
                ),
                interaction_kind=prepared.interaction_kind,
            )
            transition = await self._interaction_transition_port.commit_resume(
                prepared.request,
                deadline_monotonic=time.monotonic() + 30.0,
            )
            if isinstance(transition.outcome, InteractionTransitionNone):
                raise InteractionTransitionNotCommitted(
                    "interaction resume candidate was not committed"
                )
            if isinstance(transition.outcome, InteractionTransitionUntrusted):
                raise InteractionTransitionReconciliationRequired(
                    "interaction resume requires durable reconciliation"
                )
            if not isinstance(transition.outcome, InteractionTransitionFull):
                raise RuntimeError("interaction transition returned an invalid outcome")
            self._interaction_transition_port.prepare_resume_activation(prepared)
            self._complete_boundary_attempt_after_activation()
            if prepare_plan_state:
                self._configure_pending_host_plan(
                    prepared.request.owner_identity.run_id
                )
            try:
                result = await self._run_resume_owned(prepared=prepared)
                await self._sync_ingress_waiting_state()
                return result
            except EventPublicationAfterCommitError:
                if recover_pending_on_publication_failure:
                    await self._sync_ingress_waiting_state()
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
        prepared = self._prepare_interaction_resume_attempt(
            pending=pending,
            interaction_kind="approval",
            resolution=resolution,
        )
        observer = _StreamObserver()

        async def _submit() -> None:
            await self._ingress_coordinator.submit(
                kind="resume",
                payload=resolution,
                ingress_id=f"host_ingress_resume:{uuid4().hex}",
                resume_match_key=resolution.approval_id,
                runner=lambda owner: self._run_stream_resume_ingress(
                    owner,
                    observer=observer,
                    pipeline=self._stream_approval_resolution_pipeline(
                        prepared=prepared,
                        resolution=resolution,
                    ),
                ),
            )

        task = self._create_owned_boundary_task(
            _submit,
            preparing_identity=prepared.boundary_identity,
            observer=observer,
        )
        return _OwnedBoundaryStreamObserver(
            self._observe_owned_boundary_stream(observer=observer, task=task),
            observer,
        )

    async def _run_stream_resume_ingress(
        self,
        _ingress_owner: HostIngressAttemptOwner,
        *,
        observer: _StreamObserver,
        pipeline: AsyncIterator[AgentEvent],
    ) -> None:
        attempt = self._take_boundary_execution_ownership()
        try:
            async for event in pipeline:
                await observer.emit(event)
            await self._sync_ingress_waiting_state()
        finally:
            self._finish_ingress_owned_boundary(attempt)

    async def _stream_approval_resolution_pipeline(
        self,
        *,
        prepared: PreparedInteractionResumeAttempt,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=resolution.approval_id,
                interaction_kind="approval",
            )
            transition = await self._interaction_transition_port.commit_resume(
                prepared.request,
                deadline_monotonic=time.monotonic() + 30.0,
            )
            boundary_events = self._require_full_interaction_transition(transition)
            self._interaction_transition_port.prepare_resume_activation(prepared)
            self._complete_boundary_attempt_after_activation()
            for event in boundary_events:
                yield event
            async for event in self._stream_resume_owned(prepared=prepared):
                yield event

    async def resolve_plan_interaction(
        self,
        resolution: PlanInteractionResolution,
    ) -> HostActivationResult:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        prepared = self._prepare_interaction_resume_attempt(
            pending=pending,
            interaction_kind="plan",
            resolution=resolution,
        )
        return await self._ingress_coordinator.submit(
            kind="resume",
            payload=resolution,
            ingress_id=f"host_ingress_resume:{uuid4().hex}",
            resume_match_key=resolution.interaction_id,
            runner=lambda owner: self._run_resume_ingress(
                owner,
                prepared=prepared,
                prepare_plan_state=True,
            ),
        )

    async def resolve_mcp_input_required(
        self,
        resolution: McpInputRequiredInteractionResolution,
    ) -> HostActivationResult:
        self._raise_if_not_open("resolving MCP input-required")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_mcp_input_required(resolution.interaction_id)
        self._raise_if_active_run()
        runtime_session = self.wiring.runtime_wiring.runtime_session
        mcp_port = runtime_session.mcp_tool_execution_port
        if mcp_port is None:
            raise RuntimeError("MCP resolution requires its execution port")
        pending_handle = mcp_port.handle_for_interaction(resolution.interaction_id)
        if pending_handle is None:
            raise RuntimeError("MCP resolution lost its process-local pending owner")
        batch_owner = pending_handle.elicitation_batch_owner
        expected_keys = tuple(slot.request.key for slot in batch_owner.item_slots)
        supplied_keys = tuple(sorted(resolution.responses))
        if not resolution.cancelled and supplied_keys != expected_keys:
            raise ValueError("MCP resolution response key set is not exact")
        if resolution.cancelled and resolution.responses:
            raise ValueError("cancelled MCP resolution cannot carry responses")
        for slot in batch_owner.item_slots:
            request_key = slot.request.key
            response = resolution.responses.get(request_key, {})
            if resolution.cancelled:
                action = McpElicitationAction.CANCEL
            else:
                try:
                    action = McpElicitationAction(response.get("action", "accept"))
                except ValueError as exc:
                    raise ValueError(
                        f"invalid MCP elicitation action for {request_key!r}"
                    ) from exc
            if slot.request.mode == "form":
                legacy_content = {
                    key: value for key, value in response.items() if key != "action"
                }
                explicit_content = response.get("content")
                if explicit_content is not None and not isinstance(
                    explicit_content, dict
                ):
                    raise TypeError("MCP form response content must be an object")
                content = (
                    explicit_content
                    if isinstance(explicit_content, dict)
                    else legacy_content
                )
                batch_owner.submit_form(
                    request_key=request_key,
                    action=action,
                    content_present=action is McpElicitationAction.ACCEPT,
                    content=(
                        content if action is McpElicitationAction.ACCEPT else None
                    ),
                )
            elif action is McpElicitationAction.ACCEPT:
                batch_owner.confirm_url_retry(request_key=request_key)
            else:
                batch_owner.decline_or_cancel_url(
                    request_key=request_key,
                    action=action,
                )
        lifecycle = runtime_session.mcp_input_required_lifecycle_store.record(
            resolution.interaction_id
        )
        if lifecycle is None:
            raise RuntimeError("MCP resolution lacks its durable lifecycle")
        attempt_ordinal = 1
        if lifecycle.latest_resume_failed_event_reference is not None:
            previous_reference = lifecycle.latest_resolution_submitted_event_reference
            if previous_reference is None:
                raise RuntimeError("MCP retry lifecycle lost its prior resolution")
            previous = runtime_session.event_log.get_by_id(previous_reference.event_id)
            if not isinstance(previous, McpInputRequiredResolutionSubmittedEvent):
                raise RuntimeError("MCP prior resolution authority is not exact")
            attempt_ordinal = previous.attempt.attempt_ordinal + 1
        prepared_resolution = mcp_port.prepare_resolution(
            pending_handle=pending_handle,
            source_suspension_event_reference=pending.source_suspension_event_reference,
            source_suspension=pending.suspension_fact,
            attempt_ordinal=attempt_ordinal,
            submitted_at_utc=(
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        )
        prepared = self._prepare_interaction_resume_attempt(
            pending=pending,
            interaction_kind="mcp_input_required",
            resolution=prepared_resolution,
        )
        return await self._ingress_coordinator.submit(
            kind="resume",
            payload=prepared_resolution,
            ingress_id=f"host_ingress_resume:{uuid4().hex}",
            resume_match_key=resolution.interaction_id,
            runner=lambda owner: self._run_resume_ingress(
                owner,
                prepared=prepared,
                recover_pending_on_publication_failure=True,
            ),
        )

    async def launch_mcp_elicitation_url(
        self,
        *,
        interaction_id: str,
        request_key: str,
        consent_receipt_fingerprint: str,
    ):
        """Launch the exact private URL owned by one pending MCP request."""

        self._raise_if_not_open("launching an MCP elicitation URL")
        pending = self._require_pending_mcp_input_required(interaction_id)
        if request_key not in {
            item.key
            for item in pending.suspension_fact.request_envelope.ordered_user_visible_input_requests
        }:
            raise KeyError("unknown MCP elicitation request key")
        mcp_port = self.wiring.runtime_wiring.runtime_session.mcp_tool_execution_port
        if mcp_port is None:
            raise RuntimeError("MCP URL launch requires its execution port")
        handle = mcp_port.handle_for_interaction(interaction_id)
        if handle is None:
            raise RuntimeError("MCP URL launch lost its pending owner")
        return await handle.elicitation_batch_owner.launch_url(
            request_key=request_key,
            consent_receipt_fingerprint=consent_receipt_fingerprint,
        )

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
            resolution_payload = {
                "source": source,
                "user_feedback": user_feedback,
            }
            prepared = self._prepare_interaction_resume_attempt(
                pending=pending,
                interaction_kind="plan",
                resolution=resolution_payload,
            )
            await self._ingress_coordinator.submit(
                kind="resume",
                payload=resolution_payload,
                ingress_id=f"host_ingress_resume:{uuid4().hex}",
                resume_match_key=pending.interaction_id,
                runner=lambda owner: self._run_exit_plan_resume_ingress(
                    owner,
                    pending=pending,
                    source=source,
                    user_feedback=user_feedback,
                    prepared=prepared,
                ),
            )
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
        prepared: PreparedInteractionResumeAttempt,
    ) -> None:
        async with self._run_lock:
            view = self._interaction_transition_port.suspended_boundary_view(prepared)
            self._require_resume_admission(
                interaction_id=pending.interaction_id,
                interaction_kind="plan",
            )
            transition = await self._interaction_transition_port.commit_resume(
                prepared.request,
                deadline_monotonic=time.monotonic() + 30.0,
            )
            self._require_full_interaction_transition(transition)
            self._interaction_transition_port.prepare_resume_activation(prepared)
            self._complete_boundary_attempt_after_activation()
            if pending.kind == "exit":
                await self.wiring.runtime_wiring.runtime_session.commit_accepted_event(
                    PlanExitResolvedEvent(
                        run_id=view.run_id,
                        turn_id=view.turn_id,
                        reply_id=view.reply_id,
                        exit_request_id=pending.exit_request_id or "",
                        tool_call_id=pending.tool_call_id,
                        decision="cancel",
                        user_feedback=user_feedback,
                    )
                )
            await self._emit_plan_mode_exited(
                source=source,
                exit_request_id=pending.exit_request_id,
                event_context=EventContext(
                    run_id=view.run_id,
                    turn_id=view.turn_id,
                    reply_id=view.reply_id,
                ),
                transition_owner="agent_run",
                host_workflow_operation_id=None,
            )
            await self._install_run_termination_intent_for_run(
                view.run_id, AbortKind.USER_STOP
            )
            service = self.wiring.run_activation_service
            if service is None:
                raise RuntimeError("runtime composition lacks its activation service")
            await service.abort_pending_run(view.run_id, reason=AbortKind.USER_STOP)
            self._finish_active_run(view.run_id)
            await self._ingress_coordinator.clear_waiting_user()

    async def _run_exit_plan_resume_ingress(
        self,
        _ingress_owner: HostIngressAttemptOwner,
        *,
        pending: PendingPlanInteraction,
        source: str,
        user_feedback: str,
        prepared: PreparedInteractionResumeAttempt,
    ) -> None:
        task = self._create_owned_boundary_task(
            lambda: self._exit_pending_plan_workflow_pipeline(
                pending=pending,
                source=source,
                user_feedback=user_feedback,
                prepared=prepared,
            ),
            preparing_identity=prepared.boundary_identity,
        )
        await asyncio.shield(task)

    def stream_plan_interaction_resolution(
        self,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        prepared = self._prepare_interaction_resume_attempt(
            pending=pending,
            interaction_kind="plan",
            resolution=resolution,
        )
        observer = _StreamObserver()

        async def _submit() -> None:
            await self._ingress_coordinator.submit(
                kind="resume",
                payload=resolution,
                ingress_id=f"host_ingress_resume:{uuid4().hex}",
                resume_match_key=resolution.interaction_id,
                runner=lambda owner: self._run_stream_resume_ingress(
                    owner,
                    observer=observer,
                    pipeline=self._stream_plan_interaction_resolution_pipeline(
                        prepared=prepared,
                        resolution=resolution,
                    ),
                ),
            )

        task = self._create_owned_boundary_task(
            _submit,
            preparing_identity=prepared.boundary_identity,
            observer=observer,
        )
        return _OwnedBoundaryStreamObserver(
            self._observe_owned_boundary_stream(observer=observer, task=task),
            observer,
        )

    async def _stream_plan_interaction_resolution_pipeline(
        self,
        *,
        prepared: PreparedInteractionResumeAttempt,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        async with self._run_lock:
            self._require_resume_admission(
                interaction_id=resolution.interaction_id,
                interaction_kind="plan",
            )
            transition = await self._interaction_transition_port.commit_resume(
                prepared.request,
                deadline_monotonic=time.monotonic() + 30.0,
            )
            boundary_events = self._require_full_interaction_transition(transition)
            self._interaction_transition_port.prepare_resume_activation(prepared)
            self._complete_boundary_attempt_after_activation()
            self._configure_pending_host_plan(prepared.request.owner_identity.run_id)
            for event in boundary_events:
                yield event
            async for event in self._stream_resume_owned(prepared=prepared):
                yield event

    async def stop_current_turn(
        self,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
        timeout: float = 2.0,
    ) -> AgentRunResult | HostBoundaryStopResult | None:
        self._raise_if_not_open("stopping the current turn")
        async with self._stop_lock:
            with self.wiring.runtime_wiring.runtime_session.write_coordinator.lock:
                self._stop_intent_revision += 1
            active_view = self._run_activation_service.run_view(
                self.active_run_id
                or self.stopping_run_id
                or self.suspended_run_id
                or ""
            )
            active_driver_running = bool(
                active_view is not None and active_view.active_driver_running
            )
            active_run_id = active_view.run_id if active_view is not None else None
            service = self._run_activation_service
            boundary_attempt = self._boundary_attempt
            boundary_task = (
                boundary_attempt.owner_task if boundary_attempt is not None else None
            )
            if (
                boundary_task is not None
                and not boundary_task.done()
                and not active_driver_running
            ):
                committed_run_id = (
                    boundary_attempt.draft_run_id
                    if boundary_attempt is not None
                    else None
                )
                observer = self._current_boundary_observer()
                if observer is not None:
                    observer.detach()
                prepared_activation = (
                    boundary_attempt.prepared_activation
                    if boundary_attempt is not None
                    else None
                )
                if prepared_activation is not None:
                    prepared_activation.request_stop(reason)
                    self._boundary_stop_requested_run_ids.add(
                        prepared_activation.run_id
                    )
                commit_started = boundary_attempt is not None and (
                    boundary_attempt.phase
                    in {
                        HostRunBoundaryPhase.DURABLE_COMMIT,
                        HostRunBoundaryPhase.ACTIVATION,
                        HostRunBoundaryPhase.POST_COMMIT_INITIALIZATION,
                    }
                )
                cancelled_owner = False
                if not commit_started:
                    cancelled_owner = (
                        await self._ingress_coordinator.cancel_active_preparation()
                    )
                if not commit_started and not cancelled_owner:
                    boundary_task.cancel()
                boundary_public_result: object | None = None
                try:
                    boundary_public_result = await asyncio.wait_for(
                        asyncio.shield(boundary_task), timeout=timeout
                    )
                except asyncio.CancelledError:
                    pass
                except TimeoutError:
                    return None
                except Exception:
                    pass
                boundary_outcome = (
                    await asyncio.shield(boundary_attempt.completion)
                    if boundary_attempt is not None
                    else None
                )
                if (
                    isinstance(boundary_public_result, AgentRunResult)
                    and boundary_public_result.finalized
                ):
                    return boundary_public_result
                if committed_run_id is not None and service.is_finalized(
                    committed_run_id
                ):
                    return agent_run_result_from_terminal_outcome(
                        await service.wait_run_completion(committed_run_id)
                    )
                if committed_run_id is not None and service.has_run_owner(
                    committed_run_id
                ):
                    await self._install_run_termination_intent_for_run(
                        committed_run_id, reason
                    )
                    result = await service.terminalize_resident_run(
                        committed_run_id, reason=reason
                    )
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
            if self.pending_interaction is not None and not active_driver_running:
                if self._run_lock.locked():
                    raise HostSessionBusyError("host session already has an active run")
                async with self._run_lock:
                    pending = self.pending_interaction
                    if pending is None:
                        return None
                    await self._install_run_termination_intent_for_run(
                        pending.run_id, reason
                    )
                    self.last_active_at = time.monotonic()
                    try:
                        result = await service.terminalize_resident_run(
                            pending.run_id, reason=reason
                        )
                        if result.finalized:
                            self._retire_confirmed_run_owner(pending.run_id)
                            await self._ingress_coordinator.clear_waiting_user()
                        return result
                    finally:
                        self.last_active_at = time.monotonic()

            if active_run_id is None:
                return None
            if not active_driver_running:
                view = service.run_view(active_run_id)
                if view is None or view.terminal_state == "confirmed":
                    return None
                result = await service.terminalize_resident_run(
                    active_run_id, reason=reason
                )
                if not result.finalized:
                    raise RuntimeError("RunEnd retry did not reach durable commit")
                self._finish_active_run(active_run_id)
                return result
            run_completion = service.capture_run_completion(active_run_id)
            self._boundary_stop_requested_run_ids.add(active_run_id)
            await self._install_run_termination_intent_for_run(active_run_id, reason)
            stop_status = await service.request_active_stop_and_wait(
                active_run_id,
                reason,
                timeout_seconds=timeout,
            )
            if stop_status == "timed_out":
                return None
            # The owned task (streaming drive or run coroutine) finalizes itself
            # on a stop_request; abort_run is idempotent and yields the result.
            result = agent_run_result_from_terminal_outcome(
                await asyncio.shield(run_completion)
            )
            if not result.finalized:
                result = await service.terminalize_resident_run(
                    active_run_id, reason=reason
                )
            return result

    async def _install_run_termination_intent_for_run(
        self,
        run_id: str,
        reason: AbortKind,
    ) -> RunTerminationIntent | None:
        with self.wiring.runtime_wiring.runtime_session.write_coordinator.lock:
            view = self._run_activation_service.run_view(run_id)
            if view is None:
                return None
            intent = RunTerminationIntent(
                intent_id=f"run_termination_intent:{uuid4().hex}",
                kind=reason.value,  # type: ignore[arg-type]
                requested_at_utc=utc_now(),
                requester_id=self.host_session_id,
                target_segment_id=view.active_segment_id,
                target_segment_generation=view.active_segment_generation,
            )
            status, installed = self._run_activation_service.install_termination_intent(
                run_id,
                intent,
            )
            if status == "installed":
                self._termination_intent_revision += 1
        if installed is not None:
            service = self.wiring.run_activation_service
            if service is None:
                raise RuntimeError("runtime composition lacks its activation service")
            await service.propagate_termination_intent(run_id, installed)
        return installed

    def replay_events(self, *, after_sequence: int | None = None) -> list[AgentEvent]:
        return self.wiring.runtime_wiring.event_log.iter(after_sequence=after_sequence)

    def subscribe_terminal_monitor_events(
        self,
        *,
        reconnect_cursor=None,
        after_projection_revision: int | None = None,
    ) -> TerminalMonitorUISubscription:
        """Subscribe to UI-only terminal events without changing model delivery."""

        channel = (
            self.wiring.runtime_wiring.runtime_session.terminal_monitor_event_channel
        )
        return channel.subscribe(
            reconnect_cursor=reconnect_cursor,
            after_projection_revision=after_projection_revision,
        )

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
            try:
                target = self.wiring.agent_runtime.resolve_run_model_target()
                attempt = await service.compact(
                    target_model_target=target,
                    trigger="manual",
                    reason="user_requested",
                    force=True,
                )
            except (
                ContextCompactionInvocationFailed,
                ContextCompactionPublicationFailedAfterCommit,
            ) as exc:
                if exc.result.terminal_event is not None:
                    self._notify_compaction_listeners(exc.result.terminal_event)
                if isinstance(
                    exc,
                    ContextCompactionPublicationFailedAfterCommit,
                ):
                    self.begin_close()
                raise
            event = attempt.terminal_event
            if event is not None:
                self._notify_compaction_listeners(event)
            return {
                "compacted": isinstance(event, ContextCompactionCompletedEvent),
                "compaction_id": event.compaction_id if event is not None else None,
                "summary_artifact_id": (
                    event.summary_artifact_id
                    if isinstance(event, ContextCompactionCompletedEvent)
                    else None
                ),
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
        """Synchronous post-drain runtime-local finalizer. Idempotent.

        Does NOT release the shared workspace terminal lease (HostCore/supervisor
        owns that) and does NOT delete the workspace root (HostCore does that
        last, after lease release). A published live session must first use
        ``await aclose()`` so async physical owners cannot be bypassed.
        """
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return
        attempt = self._boundary_attempt
        if attempt is not None and not attempt.owner_task.done():
            raise RuntimeError("cannot close HostSession with a live boundary owner")
        self._lifecycle = HostSessionLifecycle.CLOSED
        self._terminal_application_services.close()
        self._boundary_attempt = None
        self.wiring.runtime_wiring.runtime_session.bind_terminal_notification_listener(
            None
        )
        self.wiring.runtime_wiring.runtime_session.terminal_monitor_event_channel.close()
        self.wiring.close()

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
        close_deadline = time.monotonic() + drain_timeout_seconds
        runtime_session = self.wiring.runtime_wiring.runtime_session
        extraction_registration = self._compaction_memory_extraction_registration
        extraction_driver = self._compaction_memory_extraction_driver
        if extraction_driver is not None:
            extraction_driver.stop_admission()
        if extraction_registration is not None and extraction_registration.active:
            extraction_registration.revoke()
        await self._ingress_coordinator.begin_close()
        dispatch_task = self._terminal_notification_dispatch_task
        if dispatch_task is not None and not dispatch_task.done():
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)
        # Terminal completion facts are upstream of semantic repair and RunEnd.
        # Drain them before asking any finalization owner to cross the mutation
        # reconciliation gate.
        await asyncio.to_thread(
            runtime_session.terminal_monitor_coordinator.stop_admission_and_drain_workers,
            timeout_seconds=drain_timeout_seconds,
        )
        await asyncio.to_thread(
            runtime_session.terminal_sessions.kill_owned,
            self.host_session_id,
        )
        await asyncio.to_thread(
            runtime_session.terminal_sessions.drain_pending_completions,
            self.host_session_id,
            timeout_seconds=drain_timeout_seconds,
        )
        await runtime_session.drain_open_committed_reducer_barrier(
            deadline_monotonic=close_deadline
        )
        await asyncio.to_thread(
            runtime_session.terminal_monitor_coordinator.terminate_all_for_session_close,
            timeout_seconds=drain_timeout_seconds,
        )
        await runtime_session.drain_open_committed_reducer_barrier(
            deadline_monotonic=close_deadline
        )
        await self._close_terminal_notification_owners()
        await runtime_session.drain_open_committed_reducer_barrier(
            deadline_monotonic=close_deadline
        )
        await self.drain_active_run(
            reason=reason, timeout_seconds=drain_timeout_seconds
        )
        await self._terminal_application_services.stop_and_drain_commands(
            deadline_monotonic=close_deadline
        )
        await self._terminal_application_services.stop_and_drain_queue_deliveries(
            deadline_monotonic=close_deadline
        )
        await self._terminal_application_services.retire_terminal_queue_content(
            deadline_monotonic=close_deadline
        )
        await self._interaction_transition_port.aclose(
            deadline_monotonic=close_deadline
        )
        # A suspended run has no live activation to finalize itself. Install
        # its stable terminal owner before closing finalization admission.
        await self._finalize_suspended_run(reason)
        activation_service = self.wiring.run_activation_service
        if activation_service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        await activation_service.drain_reconciliations(
            deadline_monotonic=close_deadline
        )
        await self.wiring.agent_runtime.drain_run_finalizations(
            deadline_monotonic=close_deadline
        )
        await runtime_session.model_stream_execution_registry.drain_all(
            deadline_monotonic=close_deadline
        )
        if extraction_driver is not None:
            await extraction_driver.close(deadline_monotonic=close_deadline)
        await runtime_session.mandatory_runtime_audit_owner.drain(
            deadline_monotonic=close_deadline
        )
        await runtime_session.tool_execution_terminal_registry.drain_pending(
            deadline_monotonic=close_deadline
        )
        governance_engine = self.wiring.runtime_wiring.memory_governance_engine
        if governance_engine is not None:
            await governance_engine.stop_admission_and_drain(
                deadline_monotonic=time.monotonic() + drain_timeout_seconds
            )
        await self.wiring.runtime_wiring.memory_governance_executor.flush_pending_event_outbox_async(
            deadline_monotonic=time.monotonic() + drain_timeout_seconds
        )
        compaction_service = self.wiring.runtime_wiring.compaction_service
        if compaction_service is not None:
            await compaction_service.stop_post_completion_extension_admission_and_drain(
                deadline_monotonic=close_deadline
            )
        candidate_projection_port = (
            self.wiring.runtime_wiring.candidate_projection_commit_port
        )
        if candidate_projection_port is not None:
            await candidate_projection_port.stop_admission_and_drain(
                deadline_monotonic=time.monotonic() + drain_timeout_seconds
            )
            await candidate_projection_port.flush_pending(
                deadline_monotonic=time.monotonic() + drain_timeout_seconds
            )
        # These close paths can themselves append preparation-abandonment and
        # generation-terminal events.  They are therefore part of the producer
        # barrier, not a synchronous afterthought in RuntimeSession.close().
        await runtime_session.quiesce_provider_input_event_producers_for_close(
            deadline_monotonic=close_deadline
        )
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=close_deadline
        )
        window_compaction_service = runtime_session.window_compaction_service
        if window_compaction_service is not None:
            await window_compaction_service.drain_pending(
                deadline_monotonic=time.monotonic() + drain_timeout_seconds
            )
        if compaction_service is not None:
            await compaction_service.drain_pending_terminalizations(
                timeout_seconds=drain_timeout_seconds
            )
        subagent_runtime = self.wiring.subagent_runtime
        if subagent_runtime is not None:
            await subagent_runtime.cancel_active_children(
                reason_code="subagent_host_session_close",
                reason_message="HostSession is closing; active child runtimes are cancelled.",
                cancelled_by="host_shutdown",
                timeout_seconds=drain_timeout_seconds,
            )
        mcp_tool_execution_port = runtime_session.mcp_tool_execution_port
        if mcp_tool_execution_port is not None:
            await mcp_tool_execution_port.stop_admission_and_drain(
                deadline_monotonic=close_deadline,
            )
        if self.mcp_supervisor is not None:
            await self.mcp_supervisor.aclose(timeout_seconds=drain_timeout_seconds)

        # All EventLog producers are now quiescent.  Only at this fixed point
        # may close retire the physical writer and the semantic repair owners.
        # A late durable FULL from tool/governance/compaction/subagent/MCP close
        # can still install an exact reducer repair until this boundary.
        await runtime_session.drain_open_committed_reducer_barrier(
            deadline_monotonic=close_deadline
        )
        # Repair completion can hand off checkpoint work and process-local
        # adoption.  Rejoin the writer/reducer chain once more before closing
        # admission so the observed fixed point covers those successors.
        await runtime_session.event_write_service.drain_pending(
            deadline_monotonic=close_deadline
        )
        await runtime_session.committed_reducer_repair_service.stop_admission_and_drain(
            deadline_monotonic=close_deadline
        )
        await runtime_session.committed_reducer_post_fold_service.stop_admission_and_drain(
            deadline_monotonic=close_deadline
        )
        runtime_session.require_mutation_allowed()

        # Checkpoint and presentation acceleration owners are downstream of
        # the final semantic authority and therefore drain last.
        await runtime_session.transcript_projection_checkpoint_service.request_close_cancellation()
        await runtime_session.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=close_deadline
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=close_deadline
        )
        await runtime_session.transcript_projection_checkpoint_service.drain_pending(
            deadline_monotonic=close_deadline
        )
        await runtime_session.prompt_queue_checkpoint_service.drain_pending(
            deadline_monotonic=close_deadline
        )
        await runtime_session.terminal_presentation_foundation_service.stop_admission_and_drain(
            deadline_monotonic=close_deadline
        )
        await self._ingress_coordinator.finish_close()
        self.close()

    async def _close_terminal_notification_owners(self) -> None:
        runtime_session = self.wiring.runtime_wiring.runtime_session
        store = runtime_session.terminal_notification_store
        pending = store.pending_notifications(
            include_unmonitored_completions=True,
            maximum_items=8,
        )
        candidates: list[AgentEvent] = []
        disposition = None
        if pending:
            source_events = tuple(item.source_event for item in pending)
            references = tuple(
                sorted(
                    (
                        event_reference_from_stored(
                            event,
                            runtime_session_id=self.runtime_session_id,
                        )
                        for event in source_events
                    ),
                    key=lambda item: (item.sequence, item.event_id),
                )
            )
            first = source_events[0]
            disposition = TerminalProcessObservationDeliveryDispositionEvent(
                id=context_fingerprint(
                    "terminal-notification-session-close-disposition-id:v1",
                    tuple(item.event_id for item in references),
                ).replace("sha256:", "terminal_notification_disposition:"),
                run_id=first.run_id,
                turn_id=first.turn_id,
                reply_id=first.reply_id,
                observation_source_references=references,
                outcome="session_closed",
            )
            candidates.append(disposition)
        account = store.account_snapshot()
        release_ids = tuple(
            item.reservation_id for item in account.active_completion_reservations
        )
        if release_ids:
            causes: tuple[AgentEvent, ...]
            if disposition is not None:
                causes = (disposition,)
            else:
                completion_events = store.completion_events_for_reservations(
                    release_ids
                )
                if not completion_events:
                    raise RuntimeError(
                        "terminal completion reservations lack close authority"
                    )
                causes = completion_events
            candidates.extend(
                runtime_session.terminal_notification_account_coordinator.freeze_released_events(
                    reservation_ids=release_ids,
                    cause_events=causes,
                )
            )
        if candidates:
            await runtime_session.commit_accepted_events(tuple(candidates))

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
        boundary_attempt = self._boundary_attempt
        boundary_task = (
            boundary_attempt.owner_task if boundary_attempt is not None else None
        )
        boundary_observer = self._current_boundary_observer()
        if boundary_observer is not None:
            boundary_observer.detach()
        view = self._run_activation_service.run_view(
            self.active_run_id or self.stopping_run_id or self.suspended_run_id or ""
        )
        run_id = view.run_id if view is not None else None
        service = self._run_activation_service
        if view is not None and view.active_driver_running:
            if reason is not None and run_id is not None:
                await self._install_run_termination_intent_for_run(run_id, reason)
                drain_status = await service.request_active_stop_and_wait(
                    run_id,
                    reason,
                    timeout_seconds=timeout_seconds,
                )
            else:
                drain_status = await service.cancel_active_driver_and_wait(
                    run_id,
                    timeout_seconds=timeout_seconds,
                )
            if drain_status == "timed_out":
                raise HostSessionBusyError(
                    "active run did not drain before close deadline"
                )
        if (
            boundary_task is not None
            and not boundary_task.done()
            and boundary_task is not asyncio.current_task()
        ):
            if (
                reason is not None
                and boundary_attempt is not None
                and boundary_attempt.prepared_activation is not None
            ):
                boundary_attempt.prepared_activation.request_stop(reason)
                self._boundary_stop_requested_run_ids.add(boundary_attempt.draft_run_id)
            commit_started = boundary_attempt is not None and (
                boundary_attempt.phase
                in {
                    HostRunBoundaryPhase.DURABLE_COMMIT,
                    HostRunBoundaryPhase.ACTIVATION,
                    HostRunBoundaryPhase.POST_COMMIT_INITIALIZATION,
                }
            )
            cancelled_owner = False
            if not commit_started:
                cancelled_owner = (
                    await self._ingress_coordinator.cancel_active_preparation()
                )
            if not commit_started and not cancelled_owner:
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
        run_id = (
            self.active_run_id
            or self.stopping_run_id
            or self.suspended_run_id
            or (
                boundary_attempt.draft_run_id
                if boundary_attempt is not None
                and service.has_run_owner(boundary_attempt.draft_run_id)
                else None
            )
        )
        if run_id is not None:
            view = service.run_view(run_id)
            suspended_without_terminal_candidate = (
                view is not None
                and view.lifecycle == "suspended"
                and view.terminal_state == "open"
            )
            if (
                view is not None
                and view.terminal_state != "confirmed"
                and not suspended_without_terminal_candidate
            ):
                try:
                    if reason is None:
                        return
                    result = await asyncio.wait_for(
                        service.terminalize_resident_run(run_id, reason=reason),
                        timeout=timeout_seconds,
                    )
                except BaseException:
                    # A durable RunStart cannot lose its only retry owner during
                    # close.  Propagate so HostCore preserves the session/lease.
                    raise
                if not result.finalized:
                    raise RuntimeError(
                        "active run terminalization drain ended without RunEnd"
                    )
                self._finish_active_run(run_id)
                view = service.run_view(run_id)
            if view is not None and view.terminal_state == "confirmed":
                service.retire_confirmed(run_id)
                if service.has_run_owner(run_id):
                    try:
                        await service.wait_until_retired(
                            run_id,
                            timeout_seconds=timeout_seconds,
                        )
                    except TimeoutError as exc:
                        raise HostSessionBusyError(
                            "run execution resources did not drain before close deadline"
                        ) from exc

    async def _finalize_suspended_run(self, reason: AbortKind) -> None:
        run_id = self.suspended_run_id
        if run_id is None:
            return
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        if not service.has_run_owner(run_id):
            # The closed activation outcome may already have retired the
            # owner while Host's pending-interaction projection is clearing.
            self._finish_active_run(run_id)
            return
        await self._install_run_termination_intent_for_run(run_id, reason)
        result = await service.terminalize_resident_run(run_id, reason=reason)
        if result.finalized:
            self._retire_confirmed_run_owner(run_id)

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
        run_id = self.active_run_id or self.suspended_run_id or self.stopping_run_id
        view = (
            self._run_activation_service.run_view(run_id)
            if run_id is not None
            else None
        )
        if view is None:
            return None
        runtime_session = self.wiring.runtime_wiring.runtime_session
        contract = view.genesis_long_horizon
        store = runtime_session.long_horizon_state_store
        account = store.rollout_account(contract.rollout_account_id)
        rollout = store.rollout_state(contract.rollout_account_id)
        window_chain = store.window_state(view.run_id)
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
            basis_id=view.run_start_event_id,
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
            run_ids=(view.run_id,),
            max_events=512,
            max_payload_bytes=8 * 1024 * 1024,
        )
        status_events = tuple(
            decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
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
            else getattr(
                view.effective_model_target,
                "context_budget",
            ).input_budget_tokens
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
            "run_id": view.run_id,
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
        boundary_attempt = self._boundary_attempt
        run_id = self.active_run_id or self.suspended_run_id or self.stopping_run_id
        run_view = (
            self._run_activation_service.run_view(run_id)
            if run_id is not None
            else None
        )
        latest_boundary = (
            run_view.latest_resume_boundary if run_view is not None else None
        )
        boundary_task_live = (
            boundary_attempt is not None and not boundary_attempt.owner_task.done()
        )
        initial_identity = (
            run_view.initial_boundary_identity
            if run_view is not None
            else boundary_attempt.prepared_authority.identity
            if boundary_attempt is not None
            and boundary_attempt.prepared_authority is not None
            else None
        )
        identity = (
            boundary_attempt.prepared_authority.identity
            if boundary_task_live
            and boundary_attempt is not None
            and boundary_attempt.prepared_authority is not None
            else latest_boundary.identity
            if isinstance(latest_boundary, InteractionResumeBoundaryFact)
            else initial_identity
            if isinstance(initial_identity, HostRunBoundaryIdentityFact)
            else None
        )
        if run_view is not None:
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
        observer = self._current_boundary_observer()
        if observer is None:
            observer_state = "detached"
        elif not observer.attached:
            observer_state = "detached"
        elif observer.queue.full():
            observer_state = "backpressured"
        else:
            observer_state = "attached"
        compaction_snapshot = (
            self.wiring.runtime_wiring.event_log.read_raw_events_by_types(
                (
                    EventType.CONTEXT_COMPACTION_STARTED.value,
                    EventType.CONTEXT_COMPACTION_COMPLETED.value,
                    EventType.CONTEXT_COMPACTION_FAILED.value,
                ),
                max_events=4_096,
                max_payload_bytes=8 * 1024 * 1024,
                deadline_monotonic=time.monotonic() + 5.0,
            )
        )
        compaction_events = tuple(
            decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
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
        runtime_session = self.wiring.runtime_wiring.runtime_session
        reducer_diagnostics = (
            runtime_session.committed_reducer_operational_diagnostics()
        )
        finalization_diagnostics = (
            self._run_activation_service.run_finalization_diagnostics(run_id)
            if run_id is not None
            else None
        )
        if run_view is None:
            run_control_state = "idle"
        elif run_view.run_completion_done or (
            finalization_diagnostics is not None
            and finalization_diagnostics["state"] == "completed"
        ):
            run_control_state = "terminal"
        elif (
            reducer_diagnostics["reconciliation_required"]
            or finalization_diagnostics is not None
            and finalization_diagnostics["state"]
            in {"waiting_reducer_repair", "reconciliation_required"}
        ):
            run_control_state = "blocked"
        else:
            run_control_state = "active"
        return {
            "state": live_state,
            "boundary_id": (
                identity.boundary_id
                if identity is not None
                else boundary_attempt.boundary_id
                if boundary_task_live and boundary_attempt is not None
                else None
            ),
            "kind": (
                identity.kind.value
                if identity is not None
                else boundary_attempt.kind.value
                if boundary_task_live and boundary_attempt is not None
                else None
            ),
            "phase": (
                boundary_attempt.phase.value
                if boundary_task_live and boundary_attempt is not None
                else "activation"
                if run_view is not None and run_view.active_segment_id is not None
                else "durable_commit"
                if run_view is not None
                else "ingress"
                if boundary_task_live
                else None
            ),
            "draft_run_id": (
                run_view.run_id
                if run_view is not None
                else boundary_attempt.draft_run_id
                if boundary_attempt is not None
                else None
            ),
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
            "active_segment_id": (
                run_view.active_segment_id if run_view is not None else None
            ),
            "active_segment_generation": (
                run_view.active_segment_generation if run_view is not None else None
            ),
            "active_segment_owner_kind": (
                run_view.active_segment_owner_kind if run_view is not None else None
            ),
            "active_segment_owner_id": (
                run_view.active_segment_owner_id if run_view is not None else None
            ),
            "current_execution_handle_id": (
                run_view.current_execution_handle_id if run_view is not None else None
            ),
            "retiring_execution_handle_count": (
                run_view.retiring_execution_handle_count if run_view is not None else 0
            ),
            "controller_state": "closed" if self.closed else "ready",
            "run_control_state": run_control_state,
            "finalization": finalization_diagnostics,
            "committed_reducers": reducer_diagnostics,
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
            event.sequence for event in committed_events if event.sequence is not None
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
        run_view = self._run_activation_service.run_view(attempt.draft_run_id)
        confirmation = (
            self._boundary_batch_confirmation(attempt)
            if run_view is None
            and attempt.candidate_events
            and attempt.commit_state != "not_started"
            else None
        )
        if run_view is None and attempt.run_owner_reservation_key is not None:
            self._run_activation_service.release_prepared_owner(
                attempt.run_owner_reservation_key,
                outcome=(
                    "unknown"
                    if confirmation is not None
                    and confirmation.status is not BoundaryBatchCommitStatus.NONE
                    else "none"
                ),
            )
            attempt.run_owner_reservation_key = None
            attempt.execution_handles = None
        elif run_view is None and attempt.execution_handles is not None:
            handles = attempt.execution_handles
            if handles.state == "boundary_owned":
                handles.mark_retiring()
            if handles.state == "retiring" and handles.borrow_tracker.can_retire():
                handles.mark_closed()
        prepared_activation = attempt.prepared_activation
        if run_view is None and prepared_activation is not None:
            if prepared_activation.state == "prepared":
                prepared_activation.release()
            attempt.prepared_activation = None
        elif (
            run_view is not None
            and prepared_activation is not None
            and prepared_activation.state == "promoted"
        ):
            attempt.prepared_activation = None
        if run_view is not None:
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
            run_view is not None
            and run_view.run_completion_done
            and run_view.run_completion_failed
        ):
            disposition = HostRunBoundaryDisposition.COMMITTED_EXECUTION_FAILED
        else:
            disposition = HostRunBoundaryDisposition.PROCEED
        terminal_event_id = (
            run_view.terminal_candidate_id
            if run_view is not None and run_view.terminal_candidate_id is not None
            else run_view.terminal_event_id
            if run_view is not None and run_view.terminal_state == "confirmed"
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
            run_view is not None
            and run_view.terminal_state == "confirmed"
            and run_view.active_segment_id is None
        ):
            self._run_activation_service.retire_confirmed(attempt.draft_run_id)
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

    def _create_owned_boundary_task(
        self,
        make_awaitable: Callable[[], Awaitable[Any]],
        *,
        preparing_identity: HostRunBoundaryIdentityFact,
        prepare_initial_activation: bool = False,
        observer: _StreamObserver | None = None,
    ) -> asyncio.Task[Any]:
        """Install PREPARING ownership before boundary code can execute."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "HostSession boundary APIs require a running event loop"
            ) from exc
        current_attempt = self._boundary_attempt
        if current_attempt is not None and not current_attempt.owner_task.done():
            raise HostSessionBusyError("host session already has a preparing boundary")
        activation_gate = asyncio.Event()

        async def _drive() -> Any:
            await activation_gate.wait()
            try:
                return await make_awaitable()
            finally:
                current = asyncio.current_task()
                attempt = self._boundary_attempt
                if attempt is not None and attempt.owner_task is current:
                    self._finish_boundary_attempt_safely(attempt)
                    self._boundary_attempt = None

        coroutine = _drive()
        try:
            task = loop.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        prepared_activation = None
        if prepare_initial_activation:
            if self._run_activation_service.has_run_owner(preparing_identity.run_id):
                task.cancel()
                raise RuntimeError("boundary run is already committed")
            prepared_activation = (
                self._run_activation_service.prepare_boundary_activation(
                    identity=preparing_identity,
                    owner_task=task,
                )
            )
        attempt = HostRunBoundaryAttempt(
            boundary_id=preparing_identity.boundary_id,
            kind=preparing_identity.kind,
            phase=HostRunBoundaryPhase.INGRESS,
            owner_task=task,
            draft_run_id=preparing_identity.run_id,
            prepared_authority=None,
            run_owner_reservation_key=None,
            execution_handles=None,
            candidate_events=(),
            candidate_event_ids=(),
            candidate_payload_fingerprints=(),
            commit_state="not_started",
            completion=loop.create_future(),
            prepared_activation=prepared_activation,
            observer=observer,
        )
        self._boundary_attempt = attempt
        activation_gate.set()
        return task

    def _start_owned_boundary_stream(
        self,
        make_stream: Callable[[], AsyncIterator[AgentEvent]],
        *,
        preparing_identity: HostRunBoundaryIdentityFact,
        prepare_initial_activation: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Start a Host-owned stream driver before returning its observer."""
        observer = _StreamObserver()

        async def _drive() -> None:
            async for event in make_stream():
                await observer.emit(event)

        task = self._create_owned_boundary_task(
            _drive,
            preparing_identity=preparing_identity,
            prepare_initial_activation=prepare_initial_activation,
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

    def _prepare_committed_host_activation(
        self,
        run_id: str,
        committed: CommittedHostRunEntry,
    ) -> None:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        service.prepare_pending_host_activation(
            run_id=run_id,
            committed=committed,
            host_session_id=self.host_session_id,
            workflow_state=self.plan_state,
            pending_entry_audit=self.plan_state.pending_entry_audit,
            previous_permission_mode=self.plan_state.pre_plan_permission_mode,
            previous_permission_policy=(
                self.plan_state.pre_plan_permission_policy or {}
            ),
            entry_reason=self.plan_state.entry_reason,
        )
        self.last_active_at = time.monotonic()

    def _configure_pending_host_plan(self, run_id: str) -> None:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        service.configure_pending_host_plan(
            run_id=run_id,
            host_session_id=self.host_session_id,
            workflow_state=self.plan_state,
            pending_entry_audit=self.plan_state.pending_entry_audit,
            previous_permission_mode=self.plan_state.pre_plan_permission_mode,
            previous_permission_policy=(
                self.plan_state.pre_plan_permission_policy or {}
            ),
            entry_reason=self.plan_state.entry_reason,
        )
        self.last_active_at = time.monotonic()

    async def _prepare_and_commit_resume_boundary(
        self,
        *,
        prepared_attempt: PreparedInteractionResumeAttempt,
        pending: PendingInteraction,
        interaction_kind: str,
        identity: HostRunBoundaryIdentityFact,
        resolution: object | None = None,
    ) -> tuple[CommittedInteractionResumeBoundary, tuple[AgentEvent, ...]]:
        self._set_boundary_phase(HostRunBoundaryPhase.ADMISSION)
        deadline_budget = build_runtime_event_deadline_budget(
            admitted_at_monotonic=time.monotonic(),
            total_timeout_seconds=30.0,
            terminal_reserve_seconds=10.0,
        )
        view = self._interaction_transition_port.suspended_boundary_view(
            prepared_attempt
        )
        self._set_boundary_phase(HostRunBoundaryPhase.CONTRACT_RESOLUTION)
        started = view.original_run_start_event
        rebound_target = self.wiring.agent_runtime.rebind_run_model_target(
            started.model_target
        )
        permission_snapshot = snapshot_from_run_start_event(
            started,
            runtime_session_id=self.runtime_session_id,
        )
        if view.resident_permission_snapshot != permission_snapshot:
            raise RuntimeError(
                "suspended permission snapshot differs from durable RunStart"
            )
        if (
            view.resident_model_target_fingerprint is not None
            and view.resident_model_target_fingerprint
            != rebound_target.fact.target_fingerprint
        ):
            raise RuntimeError("suspended model target differs from durable RunStart")

        original_basis = view.capability_resolve_basis
        source_plan = view.source_exposure_plan
        source_fact = view.source_exposure_fact
        source_event_ref = view.source_exposure_event_reference
        suspended_token = view.suspended_state_token

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
            run_id=view.run_id,
            turn_id=view.turn_id,
            reply_id=view.reply_id,
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
        prepared_mcp_resolution: PreparedMcpInputRequiredResolution | None = None
        mcp_resolution_event: McpInputRequiredResolutionSubmittedEvent | None = None
        if isinstance(pending, PendingMcpInputRequired):
            if interaction_kind != "mcp_input_required" or not isinstance(
                resolution,
                PreparedMcpInputRequiredResolution,
            ):
                raise RuntimeError(
                    "MCP resume boundary requires its prepared resolution owner"
                )
            source_suspension = self.wiring.runtime_wiring.event_log.get_by_id(
                pending.source_suspension_event_reference.event_id
            )
            if (
                not isinstance(source_suspension, ToolExecutionSuspendedEvent)
                or source_suspension.sequence is None
                or source_suspension.suspension != pending.suspension_fact
                or event_reference_from_stored(
                    source_suspension,
                    runtime_session_id=self.runtime_session_id,
                )
                != pending.source_suspension_event_reference
            ):
                raise RuntimeError(
                    "MCP resume source suspension is not exact durable authority"
                )
            if (
                resolution.source_suspension_event_reference
                != pending.source_suspension_event_reference
                or resolution.source_suspension_fact_fingerprint
                != pending.suspension_fact.suspension_fact_fingerprint
                or resolution.interaction_id != pending.interaction_id
            ):
                raise RuntimeError("prepared MCP resolution source authority drifted")
            source_fact = pending.suspension_fact
            durable_continuation = source_fact.durable_continuation
            source_authority = build_frozen_fact(
                McpInputRequiredSourceAuthorityFact,
                schema_version="mcp_input_required_source_authority.v2",
                interaction=source_fact.interaction,
                binding_identity=source_fact.binding_identity,
                pending_lease_reservation=source_fact.pending_lease_reservation,
                request_envelope_semantic_fingerprint=(
                    source_fact.request_envelope.request_envelope_semantic_fingerprint
                ),
                request_set_fingerprint=(
                    source_fact.request_envelope.request_set_fingerprint
                ),
                continuation_carrier_id=(durable_continuation.continuation_carrier_id),
                continuation_fact_fingerprint=(
                    durable_continuation.continuation_fact_fingerprint
                ),
                operation_expires_at_utc=(
                    durable_continuation.expiry.operation_expires_at_utc
                ),
                expiry_fingerprint=durable_continuation.expiry.expiry_fingerprint,
                rollout_reservation_id=source_fact.rollout_reservation_id,
                rollout_reservation_fingerprint=(
                    source_fact.rollout_reservation_fingerprint
                ),
                source_mcp_installation_id=(source_fact.source_mcp_installation_id),
                predecessor_resolution_submitted_event_reference=(
                    source_fact.predecessor_resolution_submitted_event_reference
                ),
                source_suspension_fact_fingerprint=(
                    source_fact.suspension_fact_fingerprint
                ),
                source_suspension_event_reference=(
                    pending.source_suspension_event_reference
                ),
                original_run_start_event_reference=event_reference_from_stored(
                    started,
                    runtime_session_id=self.runtime_session_id,
                ),
            )
            predecessor_resolution = view.latest_mcp_resolution_reference
            predecessor_failure = view.latest_mcp_resume_failure_reference
            if predecessor_failure is None:
                attempt = build_frozen_fact(
                    McpInputRequiredResolutionAttemptFact,
                    schema_version="mcp_input_required_resolution_attempt.v1",
                    round_count=source_fact.interaction.round_count,
                    attempt_ordinal=1,
                    predecessor_resolution_submitted_event_reference=None,
                    predecessor_resume_failed_event_reference=None,
                )
            else:
                if predecessor_resolution is None:
                    raise RuntimeError(
                        "MCP resume-failed state lost its predecessor resolution"
                    )
                previous_resolution = self.wiring.runtime_wiring.event_log.get_by_id(
                    predecessor_resolution.event_id
                )
                previous_failure = self.wiring.runtime_wiring.event_log.get_by_id(
                    predecessor_failure.event_id
                )
                if (
                    not isinstance(
                        previous_resolution,
                        McpInputRequiredResolutionSubmittedEvent,
                    )
                    or not isinstance(
                        previous_failure,
                        McpInputRequiredResumeFailedEvent,
                    )
                    or previous_resolution.source.source_suspension_event_reference
                    != pending.source_suspension_event_reference
                    or previous_failure.resolution_submitted_event_reference
                    != predecessor_resolution
                ):
                    raise RuntimeError("MCP resolution retry chain is not exact")
                attempt = build_frozen_fact(
                    McpInputRequiredResolutionAttemptFact,
                    schema_version="mcp_input_required_resolution_attempt.v1",
                    round_count=source_fact.interaction.round_count,
                    attempt_ordinal=previous_resolution.attempt.attempt_ordinal + 1,
                    predecessor_resolution_submitted_event_reference=(
                        predecessor_resolution
                    ),
                    predecessor_resume_failed_event_reference=predecessor_failure,
                )
            prepared_mcp_resolution = resolution
            mcp_resolution_event = McpInputRequiredResolutionSubmittedEvent(
                id=resolution.resolution_carrier.resolution_event_id,
                **event_context.event_fields(),
                source=source_authority,
                resolution=resolution.resolution_semantic,
                continuation=resolution.resolution_carrier,
                attempt=attempt,
                resume_boundary_event_identity=stable_event_identity(
                    boundary_event,
                    runtime_session_id=self.runtime_session_id,
                ),
            )
        elif interaction_kind == "mcp_input_required":
            raise RuntimeError("MCP resume boundary lost its pending interaction")
        predecessor_fingerprint = view.predecessor_authority_fingerprint
        incoming_execution_handles = self._new_execution_handles(
            owner=build_prepared_run_owner_reservation_key(
                runtime_session_id=self.runtime_session_id,
                run_id=view.run_id,
                run_start_event_id=started.id,
            ),
            generation=view.next_execution_handle_generation,
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
            deadline_budget=deadline_budget,
            gate_policy=resume_gate_policy_for(interaction_kind),  # type: ignore[arg-type]
            diagnostics=(),
            predecessor_authority_fingerprint=predecessor_fingerprint,
            expected_termination_revision=view.expected_termination_revision,
            expected_current_handle_id=view.current_execution_handle_id,
            prepared_mcp_input_required_resolution=prepared_mcp_resolution,
            mcp_input_required_resolution_event_id=(
                mcp_resolution_event.id if mcp_resolution_event is not None else None
            ),
        )
        attempt = self._boundary_attempt
        if attempt is not None:
            attempt.execution_handles = incoming_execution_handles
        candidates = (
            *pending_audits,
            exposure_event,
            boundary_event,
            *((mcp_resolution_event,) if mcp_resolution_event is not None else ()),
        )
        self._set_boundary_candidates(candidates)
        self._set_boundary_phase(HostRunBoundaryPhase.DURABLE_COMMIT)
        self._set_boundary_commit_state("commit_in_flight")
        mcp_resolution_confirmed = False

        def confirm_mcp_resolution(outcome: str) -> None:
            nonlocal mcp_resolution_confirmed
            if prepared_mcp_resolution is None or mcp_resolution_confirmed:
                return
            execution_port = runtime_session.mcp_tool_execution_port
            if execution_port is None:
                raise RuntimeError("MCP resolution lost its execution port")
            execution_port.confirm_resolution_commit(
                prepared_resolution=prepared_mcp_resolution,
                outcome=outcome,
            )
            mcp_resolution_confirmed = True

        try:
            stored = tuple(
                await runtime_session.commit_accepted_events(
                    candidates,
                    transaction_companion=(
                        prepared_mcp_resolution.transaction_companion
                        if prepared_mcp_resolution is not None
                        else None
                    ),
                )
            )
        except BaseException as exc:
            if isinstance(
                exc,
                (HostIngressAdmissionStale, TerminalNotificationAdmissionStale),
            ):
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.NONE,
                )
                self._set_boundary_commit_state("not_started")
                confirm_mcp_resolution("none")
                if isinstance(exc, HostIngressAdmissionStale):
                    raise
                raise HostIngressAdmissionStale(
                    "Host ingress notification authority changed before resume"
                ) from exc
            if isinstance(exc, EventPublicationAfterCommitError):
                self._set_boundary_commit_state("publication_failed")
                committed_events = tuple(exc.result.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=committed_events,
                )
                confirm_mcp_resolution("full")
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
                    confirm_mcp_resolution(
                        "unknown" if outcome.status == "unknown" else "none"
                    )
                    raise
                self._set_boundary_commit_state("committed")
                committed_events = tuple(outcome.committed_events)
                self._set_boundary_commit_confirmation(
                    BoundaryBatchCommitStatus.FULL,
                    committed_events=committed_events,
                )
                confirm_mcp_resolution("full")
            runtime_session.acknowledge_committed_mcp_installation_audits(
                committed_events
            )
            await self._fold_resume_boundary_or_terminalize(
                prepared_attempt=prepared_attempt,
                prepared=prepared,
                stored=committed_events,
                publication_status=(
                    "failed_after_commit"
                    if isinstance(exc, EventPublicationAfterCommitError)
                    and exc.result.publication_errors
                    else "unavailable"
                    if isinstance(exc, EventPublicationAfterCommitError)
                    else "failed_after_commit"
                ),
            )
            mcp_boundary_publication_failed = (
                isinstance(exc, EventPublicationAfterCommitError)
                and interaction_kind == "mcp_input_required"
            )
            if mcp_boundary_publication_failed:
                service = self.wiring.run_activation_service
                if service is None:
                    raise RuntimeError(
                        "runtime composition lacks its activation service"
                    )
                service.configure_mcp_publication_closure(
                    run_id=view.run_id,
                    reason="resume_boundary_publication_unavailable",
                    deadline_budget=prepared.deadline_budget,
                )
            if isinstance(exc, asyncio.CancelledError):
                _clear_current_task_cancellation()
            service = self.wiring.run_activation_service
            if service is None:
                raise RuntimeError("runtime composition lacks its activation service")
            stop_request = service.pending_stop_request(view.run_id)
            if stop_request is not None:
                await self._install_run_termination_intent_for_run(
                    view.run_id, stop_request.reason
                )
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=view.run_id,
                    abort_reason=stop_request.reason,
                )
            elif mcp_boundary_publication_failed:
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=view.run_id,
                    abort_reason=AbortKind.HOST_TEARDOWN,
                )
            else:
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=view.run_id,
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
            raise exc
        confirm_mcp_resolution("full")
        self._set_boundary_commit_confirmation(
            BoundaryBatchCommitStatus.FULL,
            committed_events=stored,
        )
        runtime_session.acknowledge_committed_mcp_installation_audits(stored)
        self._set_boundary_commit_state("committed")
        committed = await self._fold_resume_boundary_or_terminalize(
            prepared_attempt=prepared_attempt,
            prepared=prepared,
            stored=stored,
            publication_status="completed",
        )
        self._set_boundary_phase(HostRunBoundaryPhase.ACTIVATION)
        return committed, stored

    async def _fold_resume_boundary_or_terminalize(
        self,
        *,
        prepared_attempt: PreparedInteractionResumeAttempt,
        prepared: PreparedInteractionResumeBoundary,
        stored: tuple[AgentEvent, ...],
        publication_status: Literal["completed", "failed_after_commit", "unavailable"],
    ) -> CommittedInteractionResumeBoundary:
        try:
            return self._interaction_transition_port.fold_committed_resume_boundary(
                prepared_attempt=prepared_attempt,
                prepared_boundary=prepared,
                stored=stored,
                publication_status=publication_status,
            )
        except BaseException as fold_error:
            if isinstance(fold_error, asyncio.CancelledError):
                _clear_current_task_cancellation()
            try:
                await self._terminalize_committed_run_after_boundary_failure(
                    run_id=prepared_attempt.request.owner_identity.run_id,
                    stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                    error_message=(
                        f"committed resume fold failed: {type(fold_error).__name__}"
                    ),
                )
            except BaseException:
                self.wiring.runtime_wiring.runtime_session.latch_event_commit_outcome_unknown()
                raise
            raise fold_error

    async def _run_initial_owned(
        self,
        *,
        run_id: str,
        draft: AgentRunDraft,
        committed: CommittedHostRunEntry,
        active_skill_names: frozenset[str],
    ) -> HostActivationResult:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        dispatch = service.start_initial_result_activation(
            run_id=run_id,
            host_session_id=self.host_session_id,
            draft=draft,
            committed=committed,
            active_skill_names=active_skill_names,
            on_plan_entry_audit_emitted=self._mark_plan_entry_audit_emitted,
            on_activation_settled=self._on_activation_settled,
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {dispatch.reason}"
            )
        return await self._await_dispatch_outcome(dispatch)

    async def _stream_initial_owned(
        self,
        *,
        run_id: str,
        draft: AgentRunDraft,
        committed: CommittedHostRunEntry,
        active_skill_names: frozenset[str],
    ) -> AsyncIterator[AgentEvent]:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        dispatch = service.start_initial_stream_activation(
            run_id=run_id,
            host_session_id=self.host_session_id,
            draft=draft,
            committed=committed,
            active_skill_names=active_skill_names,
            on_plan_entry_audit_emitted=self._mark_plan_entry_audit_emitted,
            on_activation_settled=self._on_activation_settled,
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {dispatch.reason}"
            )
        if not isinstance(dispatch, RunActivationDispatch) or dispatch.observer is None:
            raise RuntimeError("stream activation did not install its observer")
        observer = dispatch.observer
        try:
            async for event in observer:
                if not isinstance(event, AgentEvent):
                    raise RuntimeError("run observer emitted a non-event payload")
                yield event
            await self._await_dispatch_outcome(dispatch)
        finally:
            await observer.aclose()

    async def _run_resume_owned(
        self,
        *,
        prepared: PreparedInteractionResumeAttempt,
    ) -> HostActivationResult:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        dispatch = service.start_resume_result_activation(
            run_id=prepared.request.owner_identity.run_id,
            host_session_id=self.host_session_id,
            interaction_kind=prepared.interaction_kind,
            resolution=prepared.resolution,
            on_plan_entry_audit_emitted=self._mark_plan_entry_audit_emitted,
            on_activation_settled=self._on_activation_settled,
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {dispatch.reason}"
            )
        return await self._await_dispatch_outcome(dispatch)

    async def _stream_resume_owned(
        self,
        *,
        prepared: PreparedInteractionResumeAttempt,
    ) -> AsyncIterator[AgentEvent]:
        service = self.wiring.run_activation_service
        if service is None:
            raise RuntimeError("runtime composition lacks its activation service")
        dispatch = service.start_resume_stream_activation(
            run_id=prepared.request.owner_identity.run_id,
            host_session_id=self.host_session_id,
            interaction_kind=prepared.interaction_kind,
            resolution=prepared.resolution,
            on_plan_entry_audit_emitted=self._mark_plan_entry_audit_emitted,
            on_activation_settled=self._on_activation_settled,
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise HostSessionBusyError(
                f"run segment activation blocked: {dispatch.reason}"
            )
        if not isinstance(dispatch, RunActivationDispatch) or dispatch.observer is None:
            raise RuntimeError("stream activation did not install its observer")
        observer = dispatch.observer
        try:
            async for event in observer:
                if not isinstance(event, AgentEvent):
                    raise RuntimeError("run observer emitted a non-event payload")
                yield event
            await self._await_dispatch_outcome(dispatch)
        finally:
            await observer.aclose()

    async def _await_dispatch_outcome(
        self,
        dispatch: RunActivationDispatch,
    ) -> HostActivationResult:
        outcome = await dispatch.wait_activation()
        if isinstance(outcome, RunTerminalOutcome):
            return agent_run_result_from_terminal_outcome(outcome)
        return outcome

    def _on_activation_settled(self, outcome: RunActivationOutcome) -> None:
        if isinstance(outcome, (RunTerminalOutcome, RunSuspendedOutcome)):
            self._finish_active_run(outcome.owner_identity.run_id)

    def _finish_active_run(self, settled_run_id: str | None = None) -> None:
        run_id = settled_run_id or self.active_run_id or self.stopping_run_id
        if run_id is not None:
            self._retire_confirmed_run_owner(run_id)
        self._notify_governance()
        self.last_active_at = time.monotonic()
        self._ensure_terminal_notification_dispatch()

    def _retire_confirmed_run_owner(self, run_id: str) -> None:
        view = self._run_activation_service.run_view(run_id)
        if view is not None and view.terminal_state == "confirmed":
            if view.terminal_event_sequence is None:
                self._terminalize_presentation_run_growth(
                    run_id, outcome="reconciliation_required"
                )
            else:
                self._terminalize_presentation_run_growth_after_sequence(
                    run_id,
                    through_sequence=view.terminal_event_sequence,
                    outcome="settled",
                )
            self._run_activation_service.retire_confirmed(run_id)

    def _reserve_presentation_run_growth(self, run_id: str) -> None:
        if run_id in self._presentation_history_run_reservations:
            return
        foundation = self.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        source = presentation_run_growth_source_fingerprint(
            runtime_session_id=self.runtime_session_id,
            run_id=run_id,
        )
        reservation = foundation.reserve_ordinary_growth(
            admission_kind="run_activation",
            source_authority_fingerprint=source,
            owner_kind="host_run",
            owner_id=run_id,
            owner_generation=1,
        )
        self._presentation_history_run_reservations[run_id] = (
            reservation.growth_reservation_id
        )

    def _release_uncommitted_presentation_run_growth(self, run_id: str) -> None:
        view = self._run_activation_service.run_view(run_id)
        if view is not None:
            if view.terminal_state == "confirmed":
                self._terminalize_presentation_run_growth(run_id, outcome="settled")
            return
        self._terminalize_presentation_run_growth(run_id, outcome="released")

    def _terminalize_presentation_run_growth(
        self,
        run_id: str,
        *,
        outcome: Literal["settled", "released", "reconciliation_required"],
    ) -> None:
        reservation_id = self._presentation_history_run_reservations.pop(run_id, None)
        if reservation_id is None:
            return
        foundation = self.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.terminalize_ordinary_growth(reservation_id, outcome=outcome)

    def _terminalize_presentation_run_growth_after_sequence(
        self,
        run_id: str,
        *,
        through_sequence: int,
        outcome: Literal["settled", "released", "reconciliation_required"],
    ) -> None:
        reservation_id = self._presentation_history_run_reservations.pop(run_id, None)
        if reservation_id is None:
            return
        foundation = self.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.terminalize_ordinary_growth_after_sequence(
            reservation_id,
            through_sequence=through_sequence,
            outcome=outcome,
        )

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
        try:
            attempt = await service.compact_if_needed(**kwargs)
        except (
            ContextCompactionInvocationFailed,
            ContextCompactionPublicationFailedAfterCommit,
        ) as exc:
            terminal_event = exc.result.terminal_event
            if terminal_event is not None:
                self._notify_compaction_listeners(terminal_event)
            if isinstance(
                exc,
                ContextCompactionPublicationFailedAfterCommit,
            ):
                self.begin_close()
            raise
        terminal_event = attempt.terminal_event
        if terminal_event is not None:
            self._notify_compaction_listeners(terminal_event)
        return bool(attempt), terminal_event

    def _notify_compaction_listeners(self, event: AgentEvent) -> None:
        for listener in list(self._compaction_listeners):
            try:
                listener(event)
            except Exception:
                continue

    async def _sync_ingress_waiting_state(self) -> None:
        pending = self.pending_interaction
        if pending is None:
            await self._ingress_coordinator.clear_waiting_user()
            return
        await self._ingress_coordinator.mark_waiting_user(
            resume_match_key=_pending_interaction_match_key(pending)
        )

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
        settlement = self.wiring.runtime_wiring.runtime_session.settle_event_from_thread(
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
        stored = settlement.committed_event
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

    def _mark_plan_entry_audit_emitted(self) -> None:
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
        if event_context is None:
            raise RuntimeError("plan mode exit requires event attribution")
        stored_exit = (
            await self.wiring.runtime_wiring.runtime_session.commit_accepted_event(
                PlanModeExitedEvent(
                    **event_context.event_fields(),
                    source=source,  # type: ignore[arg-type]
                    exit_request_id=exit_request_id,
                    restored_permission_mode=restored_mode_value,
                    restored_permission_policy=restored_policy.to_dict(),
                    transition_owner=transition_owner,  # type: ignore[arg-type]
                    host_workflow_operation_id=host_workflow_operation_id,
                )
            )
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
        active_view = self._run_activation_service.active_host_run_view()
        if self.active_run_id is not None or (
            active_view is not None and active_view.active_driver_running
        ):
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
        active_view = self._run_activation_service.active_host_run_view()
        if self.active_run_id is not None or (
            active_view is not None and active_view.active_driver_running
        ):
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
        active_view = self._run_activation_service.active_host_run_view()
        boundary_task = self._current_boundary_task()
        if (
            self._run_lock.locked()
            or (active_view is not None and active_view.active_driver_running)
            or (boundary_task is not None and not boundary_task.done())
        ):
            raise HostSessionBusyError("host session already has an active run")


def _notification_process_id(
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
            raise RuntimeError("terminal monitor observation lacks an output cursor")
    return cursor.stream_identity.process_id


def _notification_is_terminal(
    event: TerminalProcessCompletedEvent
    | TerminalProcessMonitorObservationCommittedEvent,
) -> bool:
    return isinstance(event, TerminalProcessCompletedEvent) or (
        event.observation.observation_kind == "process_completed"
    )


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()


def _pending_interaction_match_key(pending: PendingInteraction) -> str:
    approval_id = getattr(pending, "approval_id", None)
    interaction_id = getattr(pending, "interaction_id", None)
    value = approval_id if isinstance(approval_id, str) else interaction_id
    if not isinstance(value, str) or not value:
        raise RuntimeError("pending Host interaction lacks a stable resume identity")
    return value


def _model_stream_cancel_reason(
    reason: AbortKind,
) -> Literal["user_stop", "host_teardown"]:
    if reason is AbortKind.USER_STOP:
        return "user_stop"
    return "host_teardown"
