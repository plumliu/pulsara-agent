"""Host run-boundary process-local ownership contracts.

The event-safe identities live in ``pulsara_agent.primitives``.  This module
owns only process-local attempts, live execution handles, and the stable
RunStart-to-RunEnd owner / replaceable execution-segment state machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface
from pulsara_agent.event import AgentEvent, RunEndEvent, RunStartEvent
from pulsara_agent.message import Msg
from pulsara_agent.primitives.capability import (
    CapabilityExposureSnapshotFact,
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.primitives.run_boundary import (
    BoundaryBatchConfirmation,
    BoundaryTranscriptSnapshotFact,
    HostRunBoundaryDiagnostic,
    HostRunBoundaryDisposition,
    HostRunBoundaryPhase,
    NewRunBoundaryFact,
    PlanWorkflowStateFact,
    ResumeGatePolicy,
)
from pulsara_agent.primitives.run_entry import (
    CurrentUserMessageFact,
    DurableRunExistence,
    HostRunBoundaryIdentityFact,
    HostRunBoundaryKind,
    canonical_utc_timestamp,
)
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot
from pulsara_agent.runtime.execution_handles import (
    BoundaryExecutionHandles,
    CapabilityExecutionBorrowAuthority,
    CapabilityExecutionBorrowTracker,
    CapabilityExecutionBorrowUnavailable,
)
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CapabilityResolveBasis,
    CommittedHostRunEntry,
    CommittedRunEntry,
    CommittedSubagentRunEntry,
    PreparedSubagentRunEntry,
    RunWorkingSet,
)


@dataclass(frozen=True, slots=True)
class NewRunBoundaryInput:
    identity: HostRunBoundaryIdentityFact
    user_input: str
    active_skill_names: frozenset[str]
    host_session_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class InteractionResumeBoundaryInput:
    identity: HostRunBoundaryIdentityFact
    interaction_id: str
    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    resolution: object
    suspended_state_token: str


def derive_continuation_basis(
    original: CapabilityResolveBasis,
    *,
    continuation_owner: Any,
    current_execution_surface: Any,
    basis_id: str,
) -> CapabilityResolveBasis:
    """Preserve the initial raw basis while replacing continuation attribution."""

    original_fact = original.fact
    fact = build_capability_resolve_basis(
        basis_id=basis_id,
        basis_kind="continuation",
        source_basis_id=original_fact.basis_id,
        source_basis_fingerprint=original_fact.basis_fingerprint,
        owner=continuation_owner,
        workspace_identity_fingerprint=(
            original_fact.workspace_identity_fingerprint
        ),
        memory_domain_id=original_fact.memory_domain_id,
        permission_snapshot_id=original_fact.permission_snapshot_id,
        plan_active=original_fact.plan_active,
        active_skill_names=original_fact.active_skill_names,
        user_intent_fingerprint=original_fact.user_intent_fingerprint,
        prior_transcript_fingerprint=original_fact.prior_transcript_fingerprint,
        mcp_installation_id=current_execution_surface.identity.mcp_installation_id,
        execution_surface_identity=current_execution_surface.identity,
    )
    return CapabilityResolveBasis(
        fact=fact,
        user_input=original.user_input,
        prior_messages=tuple(
            message.model_copy(deep=True) for message in original.prior_messages
        ),
        active_skill_names=original.active_skill_names,
        workspace_root=original.workspace_root,
        memory_domain_id=original.memory_domain_id,
    )


@dataclass(frozen=True, slots=True)
class PreparedNewRunBoundary:
    identity: HostRunBoundaryIdentityFact
    run_model_target: Any
    permission_snapshot: RunPermissionSnapshot
    plan_snapshot: PlanWorkflowStateFact
    mcp_installation_fact: McpInstallationReferenceFact
    owned_transcript_messages: tuple[Msg, ...]
    transcript_fact: BoundaryTranscriptSnapshotFact
    capability_basis: CapabilityResolveBasis
    current_user_message: CurrentUserMessageFact
    run_start_event_id: str
    terminal_run_end_event_id: str
    new_run_boundary: NewRunBoundaryFact
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    pending_mcp_audits: tuple[AgentEvent, ...]
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class HostRunBoundaryBlocked:
    identity: HostRunBoundaryIdentityFact
    phase: HostRunBoundaryPhase
    disposition: HostRunBoundaryDisposition
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]
    retry_after_utc: str | None

    def __post_init__(self) -> None:
        allowed = {
            HostRunBoundaryDisposition.RETRYABLE_BLOCK,
            HostRunBoundaryDisposition.TERMINAL_BLOCK,
            HostRunBoundaryDisposition.SESSION_LATCHED,
            HostRunBoundaryDisposition.COMMIT_OUTCOME_UNKNOWN,
        }
        if self.disposition not in allowed:
            raise ValueError("blocked boundary has a non-blocked disposition")
        if self.retry_after_utc is not None:
            canonical_utc_timestamp(self.retry_after_utc)


PrepareNewRunBoundaryResult: TypeAlias = PreparedNewRunBoundary | HostRunBoundaryBlocked


@dataclass(frozen=True, slots=True)
class CommittedNewRunBoundary:
    prepared: PreparedNewRunBoundary
    run_start_event_id: str
    run_start_sequence: int
    committed_audit_event_ids: tuple[str, ...]
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]


@dataclass(frozen=True, slots=True)
class PreparedInteractionResumeBoundary:
    identity: HostRunBoundaryIdentityFact
    interaction_id: str
    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    suspended_state_token: str
    original_run_start_event: RunStartEvent
    rebound_model_target: Any
    permission_snapshot: RunPermissionSnapshot
    mcp_installation_fact: McpInstallationReferenceFact
    owned_continuation_exposure_plan: CapabilityExposurePlan
    continuation_exposure_fact: CapabilityExposureSnapshotFact
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    incoming_execution_handles: BoundaryExecutionHandles
    pending_mcp_audits: tuple[AgentEvent, ...]
    gate_policy: ResumeGatePolicy
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class CommittedInteractionResumeBoundary:
    prepared: PreparedInteractionResumeBoundary
    exposure_event_id: str
    exposure_event_sequence: int
    boundary_event_id: str
    boundary_event_sequence: int
    committed_audit_event_ids: tuple[str, ...]
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]


PrepareInteractionResumeBoundaryResult: TypeAlias = (
    PreparedInteractionResumeBoundary | HostRunBoundaryBlocked
)


@dataclass(frozen=True, slots=True)
class HostRunBoundaryAttemptOutcome:
    boundary_id: str
    disposition: HostRunBoundaryDisposition
    commit_confirmation: BoundaryBatchConfirmation | None
    durable_run_existence: DurableRunExistence
    terminal_event_id: str | None
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(slots=True)
class HostRunBoundaryAttempt:
    boundary_id: str
    kind: HostRunBoundaryKind
    phase: HostRunBoundaryPhase
    owner_task: asyncio.Task[object]
    draft_run_id: str
    execution_handles: BoundaryExecutionHandles | None
    candidate_events: tuple[AgentEvent, ...]
    candidate_event_ids: tuple[str, ...]
    candidate_payload_fingerprints: tuple[str, ...]
    commit_state: Literal[
        "not_started",
        "commit_in_flight",
        "committed",
        "publication_failed",
        "commit_outcome_unknown",
        "ledger_latched",
    ]
    completion: asyncio.Future[HostRunBoundaryAttemptOutcome]


@dataclass(frozen=True, slots=True)
class HostBoundaryStoppedBeforeCommit:
    status: Literal["cancelled_before_run_start"]
    boundary_id: str
    draft_run_id: str
    durable_run_existence: Literal[DurableRunExistence.NONE]
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class HostBoundaryStopUncertain:
    status: Literal["commit_outcome_unknown", "ledger_latched"]
    boundary_id: str
    draft_run_id: str
    durable_run_existence: Literal[
        DurableRunExistence.UNKNOWN, DurableRunExistence.PARTIAL_UNTRUSTED
    ]
    commit_confirmation: BoundaryBatchConfirmation
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


HostBoundaryStopResult: TypeAlias = (
    HostBoundaryStoppedBeforeCommit | HostBoundaryStopUncertain
)


@dataclass(frozen=True, slots=True)
class RunTerminationIntent:
    intent_id: str
    kind: Literal["user_stop", "host_teardown"]
    requested_at_utc: str
    requester_id: str
    target_segment_id: str | None
    target_segment_generation: int | None

    def __post_init__(self) -> None:
        canonical_utc_timestamp(self.requested_at_utc)
        if (self.target_segment_id is None) != (
            self.target_segment_generation is None
        ):
            raise ValueError("termination target segment identity is all-or-none")
        if self.target_segment_generation is not None and self.target_segment_generation < 1:
            raise ValueError("termination target segment generation must be positive")


@dataclass(slots=True)
class StreamObserverHandle:
    observer_id: str
    queue: asyncio.Queue[Any]
    state: Literal["attached", "backpressured", "detached"]
    detached_reason: str | None
    detached: asyncio.Future[None]

    def detach(self, reason: str) -> None:
        if self.state == "detached":
            return
        self.state = "detached"
        self.detached_reason = reason
        if not self.detached.done():
            self.detached.set_result(None)


@dataclass(slots=True)
class RunExecutionSegmentResult:
    segment_id: str
    segment_generation: int
    disposition: Literal[
        "waiting_user", "run_terminal", "terminalization_pending"
    ]
    run_result: AgentRunResult


@dataclass(frozen=True, slots=True)
class RunSegmentInstallBlocked:
    reason: Literal[
        "termination_intent_present", "terminalization_started", "stale_activation_owner"
    ]
    current_terminal_state: str
    termination_intent_id: str | None


@dataclass(slots=True)
class RunExecutionSegmentOwner:
    segment_id: str
    segment_generation: int
    segment_state: Literal["reserved", "active", "completed"]
    activation_kind: Literal["initial", "interaction_resume"]
    activation_owner_kind: Literal[
        "host_run_boundary", "host_resume_boundary", "subagent_run_start"
    ]
    activation_owner_id: str
    driver_task: asyncio.Task[object] | None
    completion: asyncio.Future[RunExecutionSegmentResult]
    observer: StreamObserverHandle | None


@dataclass(slots=True)
class CommittedRunExecutionOwner:
    entry: CommittedRunEntry
    execution_handles: BoundaryExecutionHandles
    retiring_execution_handles: dict[str, BoundaryExecutionHandles]
    terminal_event_id: str
    terminal_candidate: RunEndEvent | None
    terminal_state: Literal[
        "open",
        "candidate_frozen",
        "committing",
        "confirmed",
        "commit_outcome_unknown",
        "ledger_latched",
    ]
    terminalization_task: asyncio.Task[BoundaryBatchConfirmation] | None
    termination_intent: RunTerminationIntent | None
    run_completion: asyncio.Future[AgentRunResult]
    next_segment_generation: int
    active_segment: RunExecutionSegmentOwner | None
    latest_activation_owner_kind: Literal[
        "host_run_boundary", "host_resume_boundary", "subagent_run_start"
    ]
    latest_activation_owner_id: str


@dataclass(frozen=True, slots=True)
class ExecutionHandleSwapResult:
    status: Literal["swapped", "swap_skipped_terminating"]
    current_handle_id: str
    retiring_handle_id: str | None
    termination_intent_id: str | None


class RunExecutionOwnerRegistry:
    """Synchronous event-loop registry for stable run and segment ownership."""

    def __init__(self) -> None:
        self._owners: dict[str, CommittedRunExecutionOwner] = {}
        self._retirement_events: dict[str, asyncio.Event] = {}

    def register(self, run_id: str, owner: CommittedRunExecutionOwner) -> None:
        if run_id in self._owners:
            raise RuntimeError(f"committed run owner already exists: {run_id}")
        self._owners[run_id] = owner
        self._retirement_events[run_id] = asyncio.Event()
        owner.execution_handles.borrow_tracker.on_change = (
            lambda: self._sweep_retired_owner(run_id, owner)
        )

    def get(self, run_id: str) -> CommittedRunExecutionOwner | None:
        return self._owners.get(run_id)

    @property
    def owner_count(self) -> int:
        return len(self._owners)

    async def wait_until_retired(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        if run_id not in self._owners:
            return
        event = self._retirement_events[run_id]
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)

    def require(self, run_id: str) -> CommittedRunExecutionOwner:
        owner = self.get(run_id)
        if owner is None:
            raise KeyError(run_id)
        return owner

    def install_segment(
        self,
        run_id: str,
        *,
        activation_kind: Literal["initial", "interaction_resume"],
        activation_owner_kind: Literal[
            "host_run_boundary", "host_resume_boundary", "subagent_run_start"
        ],
        activation_owner_id: str,
        driver_factory: Callable[[], Coroutine[object, object, object]],
        observer: StreamObserverHandle | None,
    ) -> RunExecutionSegmentOwner | RunSegmentInstallBlocked:
        owner = self.require(run_id)
        if owner.terminal_state != "open":
            return RunSegmentInstallBlocked(
                reason="terminalization_started",
                current_terminal_state=owner.terminal_state,
                termination_intent_id=(
                    owner.termination_intent.intent_id
                    if owner.termination_intent is not None
                    else None
                ),
            )
        if owner.termination_intent is not None:
            return RunSegmentInstallBlocked(
                reason="termination_intent_present",
                current_terminal_state=owner.terminal_state,
                termination_intent_id=owner.termination_intent.intent_id,
            )
        if (
            owner.latest_activation_owner_kind != activation_owner_kind
            or owner.latest_activation_owner_id != activation_owner_id
        ):
            return RunSegmentInstallBlocked(
                reason="stale_activation_owner",
                current_terminal_state=owner.terminal_state,
                termination_intent_id=None,
            )
        if owner.active_segment is not None:
            raise RuntimeError("committed run already has an active segment")

        generation = owner.next_segment_generation + 1
        segment = RunExecutionSegmentOwner(
            segment_id=f"run_segment:{uuid4().hex}",
            segment_generation=generation,
            segment_state="reserved",
            activation_kind=activation_kind,
            activation_owner_kind=activation_owner_kind,
            activation_owner_id=activation_owner_id,
            driver_task=None,
            completion=asyncio.get_running_loop().create_future(),
            observer=observer,
        )
        owner.next_segment_generation = generation
        owner.active_segment = segment
        coroutine: Coroutine[object, object, object] | None = None
        try:
            coroutine = driver_factory()
            task = asyncio.create_task(coroutine)
            segment.driver_task = task
            segment.segment_state = "active"
            return segment
        except BaseException:
            if owner.active_segment is segment:
                owner.active_segment = None
            if coroutine is not None:
                coroutine.close()
            raise

    def complete_segment(
        self,
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
        result: RunExecutionSegmentResult,
    ) -> Literal["completed", "stale_segment"]:
        owner = self.require(run_id)
        segment = owner.active_segment
        if (
            segment is None
            or segment.segment_id != segment_id
            or segment.segment_generation != segment_generation
        ):
            return "stale_segment"
        if (
            result.segment_id != segment_id
            or result.segment_generation != segment_generation
        ):
            raise ValueError("segment result identity mismatch")
        segment.segment_state = "completed"
        if not segment.completion.done():
            segment.completion.set_result(result)
        owner.active_segment = None
        return "completed"

    def install_termination_intent(
        self,
        run_id: str,
        intent: RunTerminationIntent,
    ) -> tuple[Literal["installed", "joined", "already_terminalizing"], RunTerminationIntent | None]:
        owner = self.require(run_id)
        if owner.terminal_state != "open":
            return "already_terminalizing", owner.termination_intent
        if owner.termination_intent is not None:
            return "joined", owner.termination_intent
        segment = owner.active_segment
        if segment is None:
            if intent.target_segment_id is not None:
                raise ValueError("suspended termination intent cannot target a segment")
        elif (
            intent.target_segment_id != segment.segment_id
            or intent.target_segment_generation != segment.segment_generation
        ):
            raise ValueError("termination intent targets a stale segment")
        owner.termination_intent = intent
        return "installed", intent

    def set_latest_activation_owner(
        self,
        run_id: str,
        *,
        owner_kind: Literal[
            "host_run_boundary", "host_resume_boundary", "subagent_run_start"
        ],
        owner_id: str,
    ) -> None:
        owner = self.require(run_id)
        if owner.terminal_state != "open":
            raise RuntimeError("cannot update activation owner after terminalization")
        owner.latest_activation_owner_kind = owner_kind
        owner.latest_activation_owner_id = owner_id

    def swap_execution_handles_after_continuation_commit(
        self,
        run_id: str,
        *,
        expected_current_handle_id: str,
        incoming: BoundaryExecutionHandles,
        committed_continuation_event_id: str,
    ) -> ExecutionHandleSwapResult:
        owner = self.require(run_id)
        current = owner.execution_handles
        if current.handle_id != expected_current_handle_id:
            raise RuntimeError("execution handle CAS mismatch")
        if incoming.state != "attempt_owned":
            raise RuntimeError("incoming execution handles must be attempt-owned")
        if owner.terminal_state != "open" or owner.termination_intent is not None:
            return ExecutionHandleSwapResult(
                status="swap_skipped_terminating",
                current_handle_id=current.handle_id,
                retiring_handle_id=None,
                termination_intent_id=(
                    owner.termination_intent.intent_id
                    if owner.termination_intent is not None
                    else None
                ),
            )
        current.mark_retiring()
        incoming.transfer_to_run(run_id)
        incoming.borrow_tracker.on_change = (
            lambda: self._sweep_retired_owner(run_id, owner)
        )
        owner.execution_handles = incoming
        owner.retiring_execution_handles[current.handle_id] = current
        self._close_retirable_handles(owner)
        owner.latest_activation_owner_kind = "host_resume_boundary"
        owner.latest_activation_owner_id = committed_continuation_event_id
        return ExecutionHandleSwapResult(
            status="swapped",
            current_handle_id=incoming.handle_id,
            retiring_handle_id=current.handle_id,
            termination_intent_id=None,
        )

    def retire_confirmed(self, run_id: str) -> bool:
        """Release one terminal owner once every process-local borrow is gone."""

        owner = self.require(run_id)
        if owner.terminal_state != "confirmed" or owner.active_segment is not None:
            return False
        current = owner.execution_handles
        if current.state == "run_owned":
            current.mark_retiring()
        owner.retiring_execution_handles[current.handle_id] = current
        self._sweep_retired_owner(run_id, owner)
        return self._owners.get(run_id) is not owner

    def _sweep_retired_owner(
        self,
        run_id: str,
        owner: CommittedRunExecutionOwner,
    ) -> None:
        """Close eligible handles and remove the exact confirmed owner.

        Borrow release callbacks can run well after ``retire_confirmed`` first
        observed a live borrow.  The callback therefore owns the final registry
        removal too; merely closing the last handle would retain the complete
        working set forever.  Identity comparison prevents an old callback from
        removing a hypothetical replacement owner for the same run id.
        """

        self._close_retirable_handles(owner)
        if (
            self._owners.get(run_id) is owner
            and owner.terminal_state == "confirmed"
            and owner.active_segment is None
            and not owner.retiring_execution_handles
        ):
            self._owners.pop(run_id, None)
            event = self._retirement_events.pop(run_id, None)
            if event is not None:
                event.set()

    def _close_retirable_handles(
        self,
        owner: CommittedRunExecutionOwner,
    ) -> None:
        for handle_id, handles in tuple(owner.retiring_execution_handles.items()):
            if handles.state == "retiring" and handles.borrow_tracker.can_retire():
                handles.mark_closed()
            if handles.state == "closed":
                # Break owner -> handles -> tracker -> callback -> owner before
                # releasing the registry reference.  A completed owner retains
                # the full LoopState through run_completion, so relying on
                # cyclic GC here causes long-session/test-suite memory spikes.
                handles.borrow_tracker.on_change = None
                owner.retiring_execution_handles.pop(handle_id, None)


__all__ = [
    "AgentRunDraft",
    "BoundaryExecutionHandles",
    "CapabilityExecutionBorrowAuthority",
    "CapabilityExecutionBorrowTracker",
    "CapabilityExecutionBorrowUnavailable",
    "CapabilityResolveBasis",
    "CommittedHostRunEntry",
    "CommittedInteractionResumeBoundary",
    "CommittedNewRunBoundary",
    "CommittedRunExecutionOwner",
    "CommittedRunEntry",
    "CommittedSubagentRunEntry",
    "ExecutionHandleSwapResult",
    "HostBoundaryStopResult",
    "HostBoundaryStopUncertain",
    "HostBoundaryStoppedBeforeCommit",
    "HostRunBoundaryAttempt",
    "HostRunBoundaryAttemptOutcome",
    "HostRunBoundaryBlocked",
    "InteractionResumeBoundaryInput",
    "NewRunBoundaryInput",
    "PrepareInteractionResumeBoundaryResult",
    "PrepareNewRunBoundaryResult",
    "PreparedInteractionResumeBoundary",
    "PreparedNewRunBoundary",
    "PreparedSubagentRunEntry",
    "RunExecutionOwnerRegistry",
    "RunExecutionSegmentOwner",
    "RunExecutionSegmentResult",
    "RunSegmentInstallBlocked",
    "RunTerminationIntent",
    "RunWorkingSet",
    "StreamObserverHandle",
    "derive_continuation_basis",
]
