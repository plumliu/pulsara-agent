"""Session-scoped process-local ownership for committed runs.

The registry is the only process owner allowed to promote a prepared RunStart
reservation, install an activation driver, rotate execution handles, or retire
a committed run.  It deliberately lives outside HostSession so Host and child
runs share the same ownership protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pulsara_agent.ports.run_execution import (
    PendingInteractionAuthority,
    PreparedRunOwnerReservationKey,
    RunProgressSnapshot,
    RunSuspendedOutcome,
    RunTerminalOutputPending,
    RunTerminalOutcome,
    RunTerminalizationPending,
    RunReconciliationRequired,
    RunSegmentInstallBlocked,
    RunTerminationIntent,
)
from pulsara_agent.ports.run_terminalization import TerminalRunReceipt
from pulsara_agent.ports.interaction_transition import (
    build_interaction_resume_link_receipt,
)
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.run_entry import CommittedRunEntry
from pulsara_agent.runtime.run_execution.authority import (
    awaiting_initial_revision,
    installed_authority_head,
    materialize_continuation_revision,
    materialize_initial_revision,
    materialize_run_genesis,
)
from pulsara_agent.event import CapabilityExposureResolvedEvent
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.ports.run_authority import AwaitingInitialRevision
from pulsara_agent.ports.run_authority import InstalledRunAuthorityRevision
from pulsara_agent.runtime.run_execution.owner import (
    BoundRunResources,
    ClosedBoundRunResources,
    ActiveRunSuspension,
    ContinuationActivationCommitResult,
    NoActiveActivation,
    NoActiveSuspension,
    PendingInteractionResumeLink,
    RunActivationCoordinator,
    RunActivationCoordinatorResult,
    RunFinalizationOwner,
    RunFinalizationSlot,
    RunObserverRegistry,
    RunOwner,
    RunProgressState,
    RunRetiringResourceSet,
    RunReconciliationOwner,
    RetiringRunResources,
    StreamObserverHandle,
    RunSuspensionResources,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.runtime.run_execution.activation import (
    materialize_activation_identity,
)
from pulsara_agent.runtime.run_execution.handle import RegistryRunHandle
from pulsara_agent.runtime.run_execution.snapshot import build_progress_snapshot
from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.run_execution.commit_gateway import event_candidate_identity


@dataclass(slots=True)
class PreparedRunOwnerReservation:
    key: PreparedRunOwnerReservationKey
    reservation_generation: int
    execution_handles: RunExecutionHandleSet
    state: Literal["prepared", "promoted", "released", "unknown"] = "prepared"


class RunExecutionRegistry:
    """Synchronous event-loop registry for stable run ownership."""

    def __init__(self) -> None:
        self._prepared: dict[str, PreparedRunOwnerReservation] = {}
        self._owners: dict[str, RunOwner] = {}
        self._retirement_events: dict[str, asyncio.Event] = {}

    def reserve_prepared(
        self,
        *,
        key: PreparedRunOwnerReservationKey,
        execution_handles: RunExecutionHandleSet,
        reservation_generation: int,
    ) -> PreparedRunOwnerReservation:
        if key.reservation_key_fingerprint in self._prepared:
            raise RuntimeError("prepared run owner reservation already exists")
        if key.run_id in self._owners:
            raise RuntimeError(f"committed run owner already exists: {key.run_id}")
        if (
            execution_handles.owner != key
            or execution_handles.state != "boundary_owned"
        ):
            raise RuntimeError("prepared execution handles do not match reservation")
        reservation = PreparedRunOwnerReservation(
            key=key,
            reservation_generation=reservation_generation,
            execution_handles=execution_handles,
        )
        self._prepared[key.reservation_key_fingerprint] = reservation
        return reservation

    def release_prepared(
        self,
        key: PreparedRunOwnerReservationKey,
        *,
        outcome: Literal["none", "unknown"] = "none",
    ) -> None:
        reservation = self._prepared.get(key.reservation_key_fingerprint)
        if reservation is None:
            return
        if reservation.key != key or reservation.state != "prepared":
            raise RuntimeError("prepared reservation release identity mismatch")
        if outcome == "unknown":
            reservation.state = "unknown"
            return
        handles = reservation.execution_handles
        if handles.state == "boundary_owned":
            handles.mark_retiring()
        if handles.state == "retiring" and handles.borrow_tracker.can_retire():
            handles.mark_closed()
        reservation.state = "released"
        self._prepared.pop(key.reservation_key_fingerprint, None)

    def promote_committed_entry(
        self,
        *,
        reservation_key: PreparedRunOwnerReservationKey,
        committed: CommittedRunEntry,
        run_start_envelope: RawStoredEventEnvelope,
        prepared_activation: PreparedRunActivationOwner | None = None,
    ) -> RunOwner:
        reservation = self._prepared.get(reservation_key.reservation_key_fingerprint)
        if reservation is None or reservation.state != "prepared":
            raise RuntimeError("committed run lost its prepared owner reservation")
        if reservation.key != reservation_key:
            raise RuntimeError("prepared owner reservation key mismatch")
        run_start = committed.run_start_event
        if (
            run_start.sequence is None
            or run_start.id != reservation_key.run_start_event_id
            or run_start.run_id != reservation_key.run_id
        ):
            raise RuntimeError("committed RunStart does not match reservation")
        if reservation_key.run_id in self._owners:
            raise RuntimeError("committed run owner already exists")

        genesis = materialize_run_genesis(
            run_start,
            stored_envelope=run_start_envelope,
        )
        if (
            genesis.owner_identity.runtime_session_id
            != reservation_key.runtime_session_id
        ):
            raise RuntimeError(
                "committed RunStart ledger owner does not match reservation"
            )
        identity = genesis.owner_identity
        handles = reservation.execution_handles
        if handles.owner != reservation_key or handles.state != "boundary_owned":
            raise RuntimeError("prepared handles changed before promotion")
        pending_activation_owner_token = None
        if prepared_activation is not None:
            working_state = prepared_activation.peek_for_registry(
                boundary_id=(
                    run_start.new_run_boundary.identity.boundary_id
                    if run_start.new_run_boundary is not None
                    else run_start.id
                )
            )
            if working_state.run_id != identity.run_id:
                raise RuntimeError("prepared activation belongs to another run")
            pending_activation_owner_token = (
                f"run-pending:{identity.owner_fingerprint}:initial"
            )

        # Everything above this point is validation.  Handle and activation-state
        # ownership move only after both candidates have been proven compatible.
        handles.transfer_to_run(identity)
        latest_owner_kind: Literal[
            "host_run_boundary", "host_resume_boundary", "subagent_run_start"
        ]
        latest_owner_id: str
        if run_start.new_run_boundary is not None:
            latest_owner_kind = "host_run_boundary"
            latest_owner_id = run_start.new_run_boundary.identity.boundary_id
        else:
            latest_owner_kind = "subagent_run_start"
            latest_owner_id = run_start.id
        finalization_owner = RunFinalizationOwner(
            owner_identity=identity,
            terminal_event_id=run_start.terminal_run_end_event_id,
        )
        owner = RunOwner(
            identity=identity,
            genesis=genesis,
            authority_head=awaiting_initial_revision(genesis),
            progress=RunProgressState(owner_identity=identity),
            lifecycle="initializing",
            resource_slot=BoundRunResources(handle_set=handles),
            retiring_resources=RunRetiringResourceSet(owner_identity=identity),
            activation_slot=NoActiveActivation(),
            suspension_slot=NoActiveSuspension(),
            finalization_slot=RunFinalizationSlot(
                state="empty",
                owner=finalization_owner,
            ),
            observer_registry=RunObserverRegistry(),
            activation_completion_history={},
            run_completion=asyncio.get_running_loop().create_future(),
            entry=committed,
            termination_intent=None,
            next_segment_generation=0,
            latest_activation_owner_kind=latest_owner_kind,
            latest_activation_owner_id=latest_owner_id,
            interaction_resume_attempts={},
            pending_activation_state=None,
            pending_activation_owner_token=None,
        )
        self._owners[identity.run_id] = owner
        self._retirement_events[identity.run_id] = asyncio.Event()
        handles.borrow_tracker.on_change = lambda: self._sweep_retired_owner(
            identity.run_id, owner
        )
        if prepared_activation is not None:
            pending_activation_state = prepared_activation.confirm_promoted(
                boundary_id=prepared_activation.boundary_id,
                run_owner_token=pending_activation_owner_token,
            )
            owner.pending_activation_state = pending_activation_state
            owner.pending_activation_owner_token = pending_activation_owner_token
        reservation.state = "promoted"
        self._prepared.pop(reservation_key.reservation_key_fingerprint, None)
        return owner

    def register_recovered(self, owner: RunOwner) -> None:
        run_id = owner.identity.run_id
        if run_id in self._owners:
            raise RuntimeError(f"committed run owner already exists: {run_id}")
        self._owners[run_id] = owner
        self._retirement_events[run_id] = asyncio.Event()
        try:
            handles = owner.execution_handles
        except RuntimeError:
            return
        handles.borrow_tracker.on_change = lambda: self._sweep_retired_owner(
            run_id, owner
        )

    def install_initial_authority_full(
        self,
        *,
        run_id: str,
        stored_exposure: CapabilityExposureResolvedEvent,
    ) -> None:
        owner = self.require(run_id)
        if not isinstance(owner.authority_head, AwaitingInitialRevision):
            installed = owner.authority_head.revision
            if installed.source_exposure_event_reference.event_id == stored_exposure.id:
                return
            raise RuntimeError("initial run authority is already installed")
        revision = materialize_initial_revision(
            genesis=owner.genesis,
            stored_exposure=stored_exposure,
        )
        owner.authority_head = installed_authority_head(revision)
        owner.progress.progress_generation += 1

    def get(self, run_id: str) -> RunOwner | None:
        return self._owners.get(run_id)

    def owners(self) -> tuple[RunOwner, ...]:
        return tuple(self._owners.values())

    def require(self, run_id: str) -> RunOwner:
        owner = self.get(run_id)
        if owner is None:
            raise KeyError(run_id)
        return owner

    def issue_handle(self, run_id: str) -> RegistryRunHandle:
        return RegistryRunHandle(_owner=self.require(run_id))

    def progress_snapshot(self, run_id: str) -> RunProgressSnapshot:
        return build_progress_snapshot(self.require(run_id))

    def host_owners(self) -> tuple[RunOwner, ...]:
        """Return resident Host run owners in committed RunStart order.

        HostSession deliberately derives its public lifecycle view from this
        registry projection.  Keeping the filter here prevents Host from
        maintaining a second set of active/suspended run identifiers while the
        same registry also owns child runs.
        """

        return tuple(
            sorted(
                (
                    owner
                    for owner in self._owners.values()
                    if owner.genesis.entry.entry_kind == "host"
                ),
                key=lambda owner: owner.identity.run_start_sequence,
            )
        )

    def current_host_owner(self) -> RunOwner | None:
        owners = self.host_owners()
        live = tuple(
            owner
            for owner in owners
            if owner.lifecycle != "terminal"
            or owner.finalization_slot.state != "completed"
        )
        if len(live) > 1:
            raise RuntimeError("Host session has multiple resident live run owners")
        return live[0] if live else None

    def active_host_owner(self) -> RunOwner | None:
        owner = self.current_host_owner()
        if owner is None or owner.active_segment is None:
            return None
        return owner

    def suspended_host_owner(self) -> RunOwner | None:
        owner = self.current_host_owner()
        if (
            owner is None
            or owner.lifecycle != "suspended"
            or not isinstance(owner.suspension_slot, ActiveRunSuspension)
        ):
            return None
        return owner

    def stopping_host_owner(self) -> RunOwner | None:
        owner = self.current_host_owner()
        if owner is None:
            return None
        if owner.termination_intent is not None or owner.lifecycle in {
            "terminalizing",
            "reconciliation_required",
        }:
            return owner
        return None

    def pending_host_interaction_view(self):
        owner = self.suspended_host_owner()
        if owner is None:
            return None
        slot = owner.suspension_slot
        assert isinstance(slot, ActiveRunSuspension)
        return slot.resources.public_view

    def install_suspension(
        self,
        run_id: str,
        *,
        authority: PendingInteractionAuthority,
        resources: RunSuspensionResources,
    ) -> None:
        owner = self.require(run_id)
        segment = owner.active_segment
        if segment is None or segment.activation_identity is None:
            raise RuntimeError("suspension requires an active typed activation")
        if authority.identity.owner_identity != owner.identity:
            raise RuntimeError("pending interaction belongs to another run owner")
        if (
            resources.pending_interaction_fingerprint
            != authority.identity.interaction_fingerprint
        ):
            raise RuntimeError("suspension resource authority mismatch")
        if resources.resource_generation != segment.segment_generation:
            raise RuntimeError("suspension resource generation mismatch")
        carrier = segment.state_carrier
        source_token = segment.state_owner_token
        if carrier is None or source_token is None:
            raise RuntimeError("suspension lost its active state owner")
        if carrier is not resources.state_carrier:
            raise RuntimeError("suspension state carrier identity drifted")
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("run cannot suspend before initial authority is FULL")
        existing = owner.suspension_slot
        if isinstance(existing, ActiveRunSuspension):
            if existing.authority != authority or existing.resources != resources:
                raise RuntimeError("run already has a different suspension owner")
        else:
            carrier.transfer(
                expected_owner_token=source_token,
                new_owner_token=resources.state_owner_token,
            )
            segment.state_carrier = None
            segment.state_owner_token = None
            owner.suspension_slot = ActiveRunSuspension(
                authority=authority,
                resources=resources,
            )
        owner.progress.progress_generation += 1

    def complete_waiting_segment(
        self,
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
    ) -> RunSuspendedOutcome:
        owner = self.require(run_id)
        segment = owner.active_segment
        suspension = owner.suspension_slot
        if (
            segment is None
            or segment.segment_id != segment_id
            or segment.segment_generation != segment_generation
            or segment.activation_identity is None
        ):
            raise RuntimeError("waiting completion targets a stale activation")
        if not isinstance(suspension, ActiveRunSuspension):
            raise RuntimeError("waiting completion lacks a suspension owner")
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("waiting completion lacks installed run authority")
        segment.segment_state = "completed"
        segment.phase = "completed"
        if segment.execution_handle_borrow is not None:
            segment.execution_handle_borrow.release()
            segment.execution_handle_borrow = None
        owner.active_segment = None
        owner.lifecycle = "suspended"
        owner.progress.progress_generation += 1
        outcome = RunSuspendedOutcome(
            owner_identity=owner.identity,
            activation_identity=segment.activation_identity,
            authority_revision_fingerprint=(
                owner.authority_head.revision.authority_fingerprint
            ),
            source_interaction_event_reference=(
                suspension.authority.identity.source_interaction_event_reference
            ),
            pending_interaction=suspension.authority,
            progress=build_progress_snapshot(owner),
        )
        result = RunActivationCoordinatorResult(
            segment_id=segment_id,
            segment_generation=segment_generation,
            disposition="waiting_user",
            outcome=outcome,
        )
        if not segment.completion.done():
            segment.completion.set_result(result)
        owner.activation_completion_history[segment_generation] = result
        return outcome

    def complete_terminal_segment(
        self,
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
    ):
        owner = self.require(run_id)
        segment = owner.active_segment
        if (
            segment is None
            or segment.segment_id != segment_id
            or segment.segment_generation != segment_generation
        ):
            raise RuntimeError("terminal completion targets a stale activation")
        segment.segment_state = "completed"
        segment.phase = "completed"
        if segment.execution_handle_borrow is not None:
            segment.execution_handle_borrow.release()
            segment.execution_handle_borrow = None
        owner.active_segment = None
        owner.suspension_slot = NoActiveSuspension()
        reconciliation = owner.reconciliation_owner
        if reconciliation is not None:
            owner.lifecycle = "reconciliation_required"
            snapshot = reconciliation.snapshot
            outcome = RunReconciliationRequired(
                fault_domain=_reconciliation_fault_domain(snapshot.active_attempt_kind),
                owner_identity=owner.identity,
                stable_owner_fingerprint=snapshot.snapshot_fingerprint,
                ledger_horizon=snapshot.expected_ledger_horizon,
                diagnostic_code="ledger_confirmation_unavailable",
            )
            result = RunActivationCoordinatorResult(
                segment_id=segment_id,
                segment_generation=segment_generation,
                disposition="reconciliation_required",
                outcome=outcome,
            )
            if not segment.completion.done():
                segment.completion.set_result(result)
            owner.activation_completion_history[segment_generation] = result
            owner.progress.progress_generation += 1
            return outcome
        finalization = owner.finalization_owner
        owner.lifecycle = (
            "terminal" if finalization.commit_state == "confirmed" else "terminalizing"
        )
        if finalization.commit_state == "confirmed":
            self._begin_execution_retirement(owner)
        owner.progress.progress_generation += 1
        if owner.run_completion.done():
            outcome = owner.run_completion.result()
            disposition = "run_terminal"
        else:
            if (
                finalization.commit_state == "confirmed"
                and finalization.materialization_owner is not None
            ):
                outcome = RunTerminalOutputPending(
                    owner_identity=owner.identity,
                    run_end_event_reference=(
                        finalization.materialization_owner.run_end_event_reference
                    ),
                    materialization_owner_fingerprint=(
                        finalization.materialization_owner.owner_fingerprint
                    ),
                )
            else:
                candidate = finalization.run_end_candidate
                if candidate is None:
                    raise RuntimeError(
                        "terminal activation lacks stable RunEnd candidate"
                    )
                candidate_id, candidate_fingerprint = event_candidate_identity(
                    candidate
                )
                finalization_fingerprint = context_fingerprint(
                    "run-finalization-owner:v1",
                    (
                        owner.identity.owner_fingerprint,
                        finalization.terminal_event_id,
                        candidate_fingerprint,
                    ),
                )
                outcome = RunTerminalizationPending(
                    owner_identity=owner.identity,
                    finalization_owner_fingerprint=finalization_fingerprint,
                    stable_terminal_candidate_id=candidate_id,
                    stable_terminal_candidate_fingerprint=candidate_fingerprint,
                    attempt_state=(
                        "unknown"
                        if finalization.state == "reconciliation_required"
                        else "prepared"
                    ),
                )
            disposition = "terminalization_pending"
        result = RunActivationCoordinatorResult(
            segment_id=segment_id,
            segment_generation=segment_generation,
            disposition=disposition,
            outcome=outcome,
        )
        if not segment.completion.done():
            segment.completion.set_result(result)
        owner.activation_completion_history[segment_generation] = result
        return outcome

    def consume_suspension(
        self,
        run_id: str,
        *,
        expected_interaction_fingerprint: str,
    ) -> RunSuspensionResources:
        owner = self.require(run_id)
        slot = owner.suspension_slot
        if not isinstance(slot, ActiveRunSuspension):
            raise RuntimeError("run has no active suspension to consume")
        if (
            slot.authority.identity.interaction_fingerprint
            != expected_interaction_fingerprint
        ):
            raise RuntimeError("pending interaction suspension CAS mismatch")
        owner.suspension_slot = NoActiveSuspension()
        owner.progress.progress_generation += 1
        owner.lifecycle = "initializing"
        return slot.resources

    def transfer_pending_state_to_finalization(self, run_id: str) -> None:
        owner = self.require(run_id)
        carrier = owner.pending_activation_state
        source_token = owner.pending_activation_owner_token
        finalization = owner.finalization_slot.owner
        if carrier is None or source_token is None:
            if getattr(finalization, "state_carrier", None) is not None:
                return
            raise RuntimeError("run has no pending state to terminalize")
        if finalization is None:
            raise RuntimeError("run lost its finalization owner")
        target_token = f"finalization:{owner.identity.owner_fingerprint}"
        carrier.transfer(
            expected_owner_token=source_token,
            new_owner_token=target_token,
        )
        finalization.state_carrier = carrier
        finalization.state_owner_token = target_token
        owner.pending_activation_state = None
        owner.pending_activation_owner_token = None
        owner.lifecycle = "terminalizing"
        owner.progress.progress_generation += 1

    def transfer_suspension_state_to_finalization(self, run_id: str) -> None:
        owner = self.require(run_id)
        suspension = owner.suspension_slot
        finalization = owner.finalization_slot.owner
        if not isinstance(suspension, ActiveRunSuspension):
            if getattr(finalization, "state_carrier", None) is not None:
                return
            raise RuntimeError("run has no suspended state to terminalize")
        if finalization is None:
            raise RuntimeError("run lost its finalization owner")
        resources = suspension.resources
        carrier = resources.state_carrier
        source_token = resources.state_owner_token
        target_token = f"finalization:{owner.identity.owner_fingerprint}"
        carrier.transfer(
            expected_owner_token=source_token,
            new_owner_token=target_token,
        )
        finalization.state_carrier = carrier
        finalization.state_owner_token = target_token
        owner.suspension_slot = NoActiveSuspension()
        owner.lifecycle = "terminalizing"
        owner.progress.progress_generation += 1

    def transfer_resident_state_to_finalization(self, run_id: str) -> None:
        """Move whichever non-active resident cache owns the run into finalization.

        A committed resume boundary can fail while its FULL batch is being folded,
        before the suspension carrier has become the next pending activation.  The
        terminalization owner must cover both sides of that atomic fold boundary.
        """

        owner = self.require(run_id)
        finalization = owner.finalization_slot.owner
        if getattr(finalization, "state_carrier", None) is not None:
            return
        if (
            owner.pending_activation_state is not None
            and owner.pending_activation_owner_token is not None
        ):
            self.transfer_pending_state_to_finalization(run_id)
            return
        if isinstance(owner.suspension_slot, ActiveRunSuspension):
            self.transfer_suspension_state_to_finalization(run_id)
            return
        raise RuntimeError("run has no resident activation state to terminalize")

    @property
    def owner_count(self) -> int:
        return len(self._owners)

    @property
    def prepared_count(self) -> int:
        return len(self._prepared)

    async def wait_until_retired(self, run_id: str, *, timeout_seconds: float) -> None:
        if run_id not in self._owners:
            return
        await asyncio.wait_for(
            self._retirement_events[run_id].wait(), timeout=timeout_seconds
        )

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
        event_log: EventLog | None = None,
    ) -> RunActivationCoordinator | RunSegmentInstallBlocked:
        owner = self.require(run_id)
        if owner.lifecycle in {"terminalizing", "terminal", "reconciliation_required"}:
            return RunSegmentInstallBlocked(
                reason="terminalization_started",
                current_terminal_state=owner.finalization_owner.commit_state,
                termination_intent_id=(
                    owner.termination_intent.intent_id
                    if owner.termination_intent is not None
                    else None
                ),
            )
        if owner.termination_intent is not None:
            return RunSegmentInstallBlocked(
                reason="termination_intent_present",
                current_terminal_state=owner.finalization_owner.commit_state,
                termination_intent_id=owner.termination_intent.intent_id,
            )
        if (
            owner.latest_activation_owner_kind != activation_owner_kind
            or owner.latest_activation_owner_id != activation_owner_id
        ):
            return RunSegmentInstallBlocked(
                reason="stale_activation_owner",
                current_terminal_state=owner.finalization_owner.commit_state,
                termination_intent_id=None,
            )
        if owner.active_segment is not None:
            raise RuntimeError("committed run already has an active activation")
        try:
            handles = owner.execution_handles
        except RuntimeError:
            return RunSegmentInstallBlocked(
                reason="resources_unbound",
                current_terminal_state=owner.finalization_owner.commit_state,
                termination_intent_id=None,
            )
        generation = owner.next_segment_generation + 1
        state_carrier = owner.pending_activation_state
        pending_state_token = owner.pending_activation_owner_token
        if state_carrier is None or pending_state_token is None:
            return RunSegmentInstallBlocked(
                reason="authority_not_ready",
                current_terminal_state=owner.finalization_owner.commit_state,
                termination_intent_id=None,
            )
        activation_identity = (
            materialize_activation_identity(
                owner=owner,
                segment_generation=generation,
                event_log=event_log,
            )
            if event_log is not None
            else None
        )
        resume_link_receipt = None
        pending_resume_link = owner.pending_interaction_resume_link
        if activation_kind == "interaction_resume":
            if activation_identity is None:
                raise RuntimeError("resume activation requires durable attribution")
            if pending_resume_link is None:
                raise RuntimeError("resume activation lost its predecessor link owner")
            resume_link_receipt = build_interaction_resume_link_receipt(
                owner_identity=owner.identity,
                previous_activation_identity=(
                    pending_resume_link.previous_activation_identity
                ),
                pending_interaction_identity=(
                    pending_resume_link.pending_interaction_identity
                ),
                resume_boundary_event_reference=(
                    pending_resume_link.resume_boundary_event_reference
                ),
                installed_authority_revision_fingerprint=(
                    pending_resume_link.installed_authority_revision_fingerprint
                ),
                resumed_by_activation_identity=activation_identity,
            )
            previous_generation = pending_resume_link.previous_activation_identity.durable_activation.segment_generation
            existing_link = owner.interaction_resume_links.get(previous_generation)
            if existing_link is not None and existing_link != resume_link_receipt:
                raise RuntimeError(
                    "resume activation predecessor already has another link"
                )
        activation_key = (
            activation_identity.activation_fingerprint
            if activation_identity is not None
            else (
                f"{owner.identity.owner_fingerprint}:{activation_owner_kind}:"
                f"{activation_owner_id}:{generation}"
            )
        )
        handle_borrow = handles.borrow_for_activation(
            activation_fingerprint=activation_key
        )
        segment = RunActivationCoordinator(
            segment_id=f"run_activation:{uuid4().hex}",
            segment_generation=generation,
            segment_state="reserved",
            activation_kind=activation_kind,
            activation_owner_kind=activation_owner_kind,
            activation_owner_id=activation_owner_id,
            driver_task=None,
            completion=asyncio.get_running_loop().create_future(),
            observer=observer,
            activation_identity=activation_identity,
            execution_handle_borrow=handle_borrow,
        )
        segment_state_token = (
            f"activation:{owner.identity.owner_fingerprint}:{generation}:"
            f"{segment.segment_id}"
        )
        state_carrier.transfer(
            expected_owner_token=pending_state_token,
            new_owner_token=segment_state_token,
        )
        segment.state_carrier = state_carrier
        segment.state_owner_token = segment_state_token
        owner.pending_activation_state = None
        owner.pending_activation_owner_token = None
        owner.next_segment_generation = generation
        owner.active_segment = segment
        coroutine: Coroutine[object, object, object] | None = None
        try:
            coroutine = driver_factory()
            segment.driver_task = asyncio.create_task(coroutine)
            segment.segment_state = "active"
            if resume_link_receipt is not None:
                previous_generation = resume_link_receipt.previous_activation_identity.durable_activation.segment_generation
                owner.interaction_resume_links[previous_generation] = (
                    resume_link_receipt
                )
                owner.pending_interaction_resume_link = None
            owner.lifecycle = "open"
            return segment
        except BaseException:
            owner.active_segment = None
            handle_borrow.release()
            state_carrier.transfer(
                expected_owner_token=segment_state_token,
                new_owner_token=pending_state_token,
            )
            owner.pending_activation_state = state_carrier
            owner.pending_activation_owner_token = pending_state_token
            segment.state_carrier = None
            segment.state_owner_token = None
            if coroutine is not None:
                coroutine.close()
            raise

    def complete_segment(
        self,
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
        result: RunActivationCoordinatorResult,
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
            raise ValueError("activation result identity mismatch")
        segment.segment_state = "completed"
        if segment.execution_handle_borrow is not None:
            segment.execution_handle_borrow.release()
            segment.execution_handle_borrow = None
        if not segment.completion.done():
            segment.completion.set_result(result)
        owner.activation_completion_history[segment_generation] = result
        owner.active_segment = None
        if result.disposition == "waiting_user":
            owner.lifecycle = "suspended"
        elif result.disposition == "run_terminal":
            owner.lifecycle = "terminalizing"
        else:
            owner.lifecycle = "terminalizing"
        return "completed"

    def abandon_segment_for_reconciliation(
        self,
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
    ) -> None:
        """Release a physically-exited driver without inventing a terminal fact."""

        owner = self.require(run_id)
        segment = owner.active_segment
        if (
            segment is None
            or segment.segment_id != segment_id
            or segment.segment_generation != segment_generation
        ):
            return
        reconciliation = owner.reconciliation_owner
        if reconciliation is None:
            raise RuntimeError("activation reconciliation lacks its stable owner")
        if segment.state_carrier is not None and segment.state_owner_token is not None:
            token = (
                f"reconciliation:{owner.identity.owner_fingerprint}:"
                f"{reconciliation.snapshot.snapshot_fingerprint}"
            )
            segment.state_carrier.transfer(
                expected_owner_token=segment.state_owner_token,
                new_owner_token=token,
            )
            reconciliation.state_carrier = segment.state_carrier
            reconciliation.state_owner_token = token
            segment.state_carrier = None
            segment.state_owner_token = None
        segment.segment_state = "completed"
        segment.phase = "completed"
        if segment.execution_handle_borrow is not None:
            segment.execution_handle_borrow.release()
            segment.execution_handle_borrow = None
        snapshot = reconciliation.snapshot
        outcome = RunReconciliationRequired(
            fault_domain=_reconciliation_fault_domain(snapshot.active_attempt_kind),
            owner_identity=owner.identity,
            stable_owner_fingerprint=snapshot.snapshot_fingerprint,
            ledger_horizon=snapshot.expected_ledger_horizon,
            diagnostic_code="ledger_confirmation_unavailable",
        )
        result = RunActivationCoordinatorResult(
            segment_id=segment_id,
            segment_generation=segment_generation,
            disposition="reconciliation_required",
            outcome=outcome,
        )
        if not segment.completion.done():
            segment.completion.set_result(result)
        owner.activation_completion_history[segment_generation] = result
        owner.active_segment = None
        owner.lifecycle = "reconciliation_required"
        owner.progress.progress_generation += 1

    def install_termination_intent(
        self, run_id: str, intent: RunTerminationIntent
    ) -> tuple[
        Literal["installed", "joined", "already_terminalizing"],
        RunTerminationIntent | None,
    ]:
        owner = self.require(run_id)
        if owner.lifecycle in {"terminalizing", "terminal", "reconciliation_required"}:
            return "already_terminalizing", owner.termination_intent
        if owner.termination_intent is not None:
            return "joined", owner.termination_intent
        segment = owner.active_segment
        if segment is None:
            if intent.target_segment_id is not None:
                raise ValueError(
                    "suspended termination intent cannot target activation"
                )
        elif (
            intent.target_segment_id != segment.segment_id
            or intent.target_segment_generation != segment.segment_generation
        ):
            raise ValueError("termination intent targets a stale activation")
        owner.termination_intent = intent
        owner.termination_revision += 1
        return "installed", intent

    def commit_continuation_activation_full(
        self,
        *,
        run_id: str,
        stored_boundary,
        stored_exposure,
        effective_model_target,
        effective_permission,
        expected_predecessor_fingerprint: str,
        expected_termination_revision: int,
        expected_current_handle_id: str,
        incoming: RunExecutionHandleSet,
        reuse_current_handles: bool,
        expected_interaction_fingerprint: str,
    ) -> ContinuationActivationCommitResult:
        """Atomically install one FULL continuation and its process ownership.

        Every fallible authority check happens before a mutable slot changes.
        Once this method starts applying state, no caller-visible exception can
        leave authority, execution resources, and suspension ownership split.
        """

        owner = self.require(run_id)
        if not isinstance(owner.authority_head, InstalledRunAuthorityRevision):
            raise RuntimeError("continuation requires an installed predecessor")
        predecessor = owner.authority_head.revision
        if predecessor.authority_fingerprint != expected_predecessor_fingerprint:
            raise RuntimeError("continuation predecessor authority CAS mismatch")
        if owner.termination_revision != expected_termination_revision:
            raise RuntimeError("continuation termination revision became stale")
        current = owner.execution_handles
        if current.handle_id != expected_current_handle_id:
            raise RuntimeError("continuation execution handle CAS mismatch")
        if incoming.state != "boundary_owned":
            raise RuntimeError("incoming execution handles must be boundary-owned")
        suspension = owner.suspension_slot
        if not isinstance(suspension, ActiveRunSuspension):
            raise RuntimeError("continuation requires one active suspension owner")
        if (
            suspension.authority.identity.interaction_fingerprint
            != expected_interaction_fingerprint
        ):
            raise RuntimeError("continuation suspension authority CAS mismatch")
        revision = materialize_continuation_revision(
            predecessor=predecessor,
            stored_boundary=stored_boundary,
            stored_exposure=stored_exposure,
            effective_model_target=effective_model_target,
            effective_permission=effective_permission,
            runtime_session_id=owner.identity.runtime_session_id,
        )
        suspended_outcome = next(
            (
                result.outcome
                for _generation, result in sorted(
                    owner.activation_completion_history.items(), reverse=True
                )
                if isinstance(result.outcome, RunSuspendedOutcome)
                and result.outcome.pending_interaction.identity.interaction_fingerprint
                == expected_interaction_fingerprint
            ),
            None,
        )
        if suspended_outcome is None:
            raise RuntimeError("continuation lacks its immutable suspended receipt")
        pending_resume_link = PendingInteractionResumeLink(
            previous_activation_identity=suspended_outcome.activation_identity,
            pending_interaction_identity=suspension.authority.identity,
            resume_boundary_event_reference=event_reference_from_stored(
                stored_boundary,
                runtime_session_id=owner.identity.runtime_session_id,
            ),
            installed_authority_revision_fingerprint=revision.authority_fingerprint,
        )

        activation_blocked = (
            owner.lifecycle
            in {
                "terminalizing",
                "terminal",
                "reconciliation_required",
            }
            or owner.termination_intent is not None
        )
        suspension_state = suspension.resources.state_carrier
        suspension_state_token = suspension.resources.state_owner_token
        pending_state_token = (
            f"run-pending:{owner.identity.owner_fingerprint}:"
            f"continuation:{revision.revision}"
        )
        retiring_handle_id: str | None = None
        if activation_blocked or reuse_current_handles:
            incoming.mark_retiring()
            if incoming.borrow_tracker.can_retire():
                incoming.mark_closed()
            disposition = (
                "activation_blocked" if activation_blocked else "reused_current"
            )
        else:
            current.mark_retiring()
            incoming.transfer_to_run(owner.identity)
            incoming.borrow_tracker.on_change = lambda: self._sweep_retired_owner(
                run_id, owner
            )
            owner.execution_handles = incoming
            owner.retiring_execution_handles[current.handle_id] = current
            retiring_handle_id = current.handle_id
            disposition = "swapped"

        owner.authority_head = installed_authority_head(revision)
        suspension_state.transfer(
            expected_owner_token=suspension_state_token,
            new_owner_token=pending_state_token,
        )
        owner.pending_activation_state = suspension_state
        owner.pending_activation_owner_token = pending_state_token
        owner.suspension_slot = NoActiveSuspension()
        owner.pending_interaction_resume_link = pending_resume_link
        owner.progress.progress_generation += 1
        owner.latest_activation_owner_kind = "host_resume_boundary"
        owner.latest_activation_owner_id = stored_boundary.id
        self._close_retirable_handles(owner)
        owner.lifecycle = "terminalizing" if activation_blocked else "initializing"
        return ContinuationActivationCommitResult(
            authority_revision_fingerprint=revision.authority_fingerprint,
            resource_disposition=disposition,
            current_handle_id=(
                current.handle_id if disposition != "swapped" else incoming.handle_id
            ),
            retiring_handle_id=retiring_handle_id,
            consumed_interaction_fingerprint=expected_interaction_fingerprint,
            termination_intent_id=(
                owner.termination_intent.intent_id
                if owner.termination_intent is not None
                else None
            ),
        )

    def retire_confirmed(self, run_id: str) -> bool:
        owner = self.require(run_id)
        if (
            owner.finalization_owner.commit_state != "confirmed"
            or owner.active_segment is not None
        ):
            return False
        owner.lifecycle = "terminal"
        self._begin_execution_retirement(owner)
        self._sweep_retired_owner(run_id, owner)
        return self._owners.get(run_id) is not owner

    def complete_terminal_output(
        self,
        run_id: str,
        *,
        receipt: TerminalRunReceipt,
    ) -> RunTerminalOutcome:
        owner = self.require(run_id)
        if receipt.owner_identity != owner.identity:
            raise RuntimeError("terminal receipt belongs to another run owner")
        if owner.finalization_owner.commit_state != "confirmed":
            raise RuntimeError("terminal output cannot precede RunEnd confirmation")
        finalization = owner.finalization_owner
        if (
            finalization.confirmed_run_end_event_reference is None
            or receipt.run_end_event_reference
            != finalization.confirmed_run_end_event_reference
        ):
            raise RuntimeError("terminal output does not join confirmed RunEnd")
        existing = finalization.terminal_receipt
        if existing is not None:
            if existing != receipt:
                raise RuntimeError("terminal output exact confirmation conflict")
        else:
            finalization.terminal_receipt = receipt
        finalization.state = "completed"
        owner.finalization_slot.state = "completed"
        owner.finalization_slot.receipt = receipt
        owner.lifecycle = "terminal"
        outcome = RunTerminalOutcome(
            owner_identity=owner.identity,
            run_end_event_reference=receipt.run_end_event_reference,
            output=receipt.output,
            finalization_receipt_fingerprint=(receipt.finalization_receipt_fingerprint),
        )
        if owner.run_completion.done():
            prior = owner.run_completion.result()
            if prior != outcome:
                raise RuntimeError("run completion was resolved by another outcome")
        else:
            owner.run_completion.set_result(outcome)
        self._sweep_retired_owner(run_id, owner)
        return outcome

    @staticmethod
    def _begin_execution_retirement(owner: RunOwner) -> None:
        slot = owner.resource_slot
        if isinstance(slot, BoundRunResources):
            handles = slot.handle_set
            if handles.state == "run_owned":
                handles.mark_retiring()
            owner.resource_slot = RetiringRunResources(handle_set=handles)
            owner.retiring_execution_handles[handles.handle_id] = handles
        elif isinstance(slot, RetiringRunResources):
            owner.retiring_execution_handles[slot.handle_set.handle_id] = (
                slot.handle_set
            )

    def _sweep_retired_owner(self, run_id: str, owner: RunOwner) -> None:
        self._close_retirable_handles(owner)
        if (
            self._owners.get(run_id) is owner
            and owner.finalization_owner.commit_state == "confirmed"
            and owner.active_segment is None
            and owner.finalization_slot.state == "completed"
            and not owner.retiring_execution_handles
        ):
            self._retire_state_carriers(owner)
            self._owners.pop(run_id, None)
            event = self._retirement_events.pop(run_id, None)
            if event is not None:
                event.set()

    @staticmethod
    def _close_retirable_handles(owner: RunOwner) -> None:
        for handle_id, handles in tuple(owner.retiring_execution_handles.items()):
            if handles.state == "retiring" and handles.borrow_tracker.can_retire():
                handles.mark_closed()
            if handles.state == "closed":
                handles.borrow_tracker.on_change = None
                owner.retiring_execution_handles.pop(handle_id, None)
                slot = owner.resource_slot
                if (
                    isinstance(slot, RetiringRunResources)
                    and slot.handle_set is handles
                ):
                    owner.resource_slot = ClosedBoundRunResources(
                        closed_handle_id=handles.handle_id,
                        closed_handle_generation=handles.handle_generation,
                    )

    @staticmethod
    def _retire_state_carriers(owner: RunOwner) -> None:
        """Revoke every residual process-state borrow before owner retirement."""

        retired: set[int] = set()

        def retire(carrier, token: str | None) -> None:
            if carrier is None or token is None or id(carrier) in retired:
                return
            carrier.retire(owner_token=token)
            retired.add(id(carrier))

        finalization = owner.finalization_slot.owner
        if isinstance(finalization, RunFinalizationOwner):
            retire(finalization.state_carrier, finalization.state_owner_token)
            finalization.state_carrier = None
            finalization.state_owner_token = None

        reconciliation = owner.reconciliation_owner
        if isinstance(reconciliation, RunReconciliationOwner):
            retire(reconciliation.state_carrier, reconciliation.state_owner_token)
            reconciliation.state_carrier = None
            reconciliation.state_owner_token = None

        retire(
            owner.pending_activation_state,
            owner.pending_activation_owner_token,
        )
        owner.pending_activation_state = None
        owner.pending_activation_owner_token = None

        suspension = owner.suspension_slot
        if isinstance(suspension, ActiveRunSuspension):
            retire(
                suspension.resources.state_carrier,
                suspension.resources.state_owner_token,
            )


__all__ = [
    "PreparedRunOwnerReservation",
    "RunExecutionRegistry",
]


def _reconciliation_fault_domain(attempt_kind: str):
    if attempt_kind in {"initial_authority_commit", "continuation_authority_commit"}:
        return "authority"
    if attempt_kind in {"suspension_commit", "interaction_resolution_commit"}:
        return "interaction"
    if attempt_kind in {"run_end_commit", "publication_terminal_maintenance"}:
        return "terminalization"
    if attempt_kind == "final_output_materialization":
        return "output"
    return "activation"
