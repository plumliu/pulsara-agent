"""Atomic durable commit boundary for admitted tool execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import TYPE_CHECKING, Sequence

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    EventContext,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    ToolExecutionSuspendedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.runtime.long_horizon.accounting import (
    resolve_run_rollout_binding,
)
from pulsara_agent.primitives.long_horizon import RolloutReservationFact
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.authority_materialization import (
    PhysicalDispatchReservationRequest,
    PhysicalOneShotReservationRequest,
)
from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import EventWriteResult, RuntimeSession
    from pulsara_agent.runtime.state import LoopState
    from pulsara_agent.tools.base import PreparedToolTerminalResult


class ToolExecutionCommitContractError(RuntimeError):
    """A tool event batch does not match the frozen rollout state."""


class ToolExecutionTerminalDrainBlocked(RuntimeError):
    """A tool call still owns physical or unconfirmed durable terminal work."""


class ToolExecutionTerminalOwnerState(StrEnum):
    ADMITTED = "admitted"
    SUSPENSION_CANDIDATE_FROZEN = "suspension_candidate_frozen"
    SUSPENDED = "suspended"
    TERMINAL_CANDIDATE_FROZEN = "terminal_candidate_frozen"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"


@dataclass(slots=True)
class ToolExecutionTerminalOwner:
    owner_id: str
    run_id: str
    tool_call_id: str
    reservation_id: str
    reservation_fingerprint: str
    state: ToolExecutionTerminalOwnerState
    stable_candidates: tuple[AgentEvent, ...] = ()
    attempt_generation: int = 0


class ToolExecutionTerminalRegistry:
    """Process owner for admitted tool calls until durable terminal settlement."""

    def __init__(self, runtime_session: RuntimeSession) -> None:
        self._runtime_session = runtime_session
        self._lock = RLock()
        self._owners: dict[str, ToolExecutionTerminalOwner] = {}
        self._owner_id_by_call: dict[tuple[str, str], str] = {}
        self._drain_in_flight: set[str] = set()

    def install_admitted_batch(
        self,
        *,
        run_id: str,
        reservations: Sequence[RolloutReservationFact],
    ) -> tuple[ToolExecutionTerminalOwner, ...]:
        if not reservations:
            raise ValueError("tool terminal owner batch cannot be empty")
        with self._lock:
            keys = tuple((run_id, item.owner_id) for item in reservations)
            if len(keys) != len(set(keys)) or any(
                key in self._owner_id_by_call for key in keys
            ):
                raise ToolExecutionCommitContractError(
                    "tool terminal owner identity already exists"
                )
            owners: list[ToolExecutionTerminalOwner] = []
            for reservation in reservations:
                if reservation.owner_kind != "tool_call":
                    raise ToolExecutionCommitContractError(
                        "tool terminal owner requires a tool-call reservation"
                    )
                owner_id = (
                    f"tool_terminal_owner:{run_id}:"
                    f"{reservation.owner_id}:{reservation.reservation_id}"
                )
                owner = ToolExecutionTerminalOwner(
                    owner_id=owner_id,
                    run_id=run_id,
                    tool_call_id=reservation.owner_id,
                    reservation_id=reservation.reservation_id,
                    reservation_fingerprint=reservation.semantic_fingerprint,
                    state=ToolExecutionTerminalOwnerState.ADMITTED,
                )
                self._owners[owner_id] = owner
                self._owner_id_by_call[(run_id, reservation.owner_id)] = owner_id
                owners.append(owner)
            return tuple(owners)

    def restore_suspended(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
    ) -> ToolExecutionTerminalOwner:
        with self._lock:
            existing = self._owner_for_call_locked(run_id, reservation.owner_id)
            if existing is not None:
                self._require_reservation(existing, reservation)
                if existing.state not in {
                    ToolExecutionTerminalOwnerState.SUSPENDED,
                    ToolExecutionTerminalOwnerState.SUSPENSION_CANDIDATE_FROZEN,
                    ToolExecutionTerminalOwnerState.COMMIT_OUTCOME_UNKNOWN,
                }:
                    raise ToolExecutionCommitContractError(
                        "pending tool owner is not suspended"
                    )
                return existing
        owner = self.install_admitted_batch(
            run_id=run_id,
            reservations=(reservation,),
        )[0]
        with self._lock:
            owner.state = ToolExecutionTerminalOwnerState.SUSPENDED
        return owner

    def freeze_suspension(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
        candidates: Sequence[AgentEvent],
    ) -> ToolExecutionTerminalOwner:
        return self._freeze(
            run_id=run_id,
            reservation=reservation,
            candidates=candidates,
            target=ToolExecutionTerminalOwnerState.SUSPENSION_CANDIDATE_FROZEN,
            allowed=(ToolExecutionTerminalOwnerState.ADMITTED,),
        )

    def mark_suspended(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
    ) -> None:
        with self._lock:
            owner = self._require_owner_locked(run_id, reservation.owner_id)
            self._require_reservation(owner, reservation)
            if owner.state not in {
                ToolExecutionTerminalOwnerState.SUSPENSION_CANDIDATE_FROZEN,
                ToolExecutionTerminalOwnerState.COMMIT_OUTCOME_UNKNOWN,
            }:
                raise ToolExecutionCommitContractError(
                    "tool suspension was not frozen before confirmation"
                )
            owner.state = ToolExecutionTerminalOwnerState.SUSPENDED
            owner.stable_candidates = ()

    def freeze_terminal(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
        candidates: Sequence[AgentEvent],
    ) -> ToolExecutionTerminalOwner:
        return self._freeze(
            run_id=run_id,
            reservation=reservation,
            candidates=candidates,
            target=ToolExecutionTerminalOwnerState.TERMINAL_CANDIDATE_FROZEN,
            allowed=(
                ToolExecutionTerminalOwnerState.ADMITTED,
                ToolExecutionTerminalOwnerState.SUSPENDED,
                ToolExecutionTerminalOwnerState.SUSPENSION_CANDIDATE_FROZEN,
                ToolExecutionTerminalOwnerState.COMMIT_OUTCOME_UNKNOWN,
            ),
        )

    def mark_commit_outcome_unknown(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
    ) -> None:
        with self._lock:
            owner = self._require_owner_locked(run_id, reservation.owner_id)
            self._require_reservation(owner, reservation)
            if not owner.stable_candidates:
                raise ToolExecutionCommitContractError(
                    "unknown tool commit lacks stable candidates"
                )
            owner.state = ToolExecutionTerminalOwnerState.COMMIT_OUTCOME_UNKNOWN

    def complete_terminal(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
    ) -> None:
        with self._lock:
            owner = self._require_owner_locked(run_id, reservation.owner_id)
            self._require_reservation(owner, reservation)
            self._remove_locked(owner)

    def owner_for_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
    ) -> ToolExecutionTerminalOwner | None:
        with self._lock:
            return self._owner_for_call_locked(run_id, tool_call_id)

    def active_owner_count(self) -> int:
        with self._lock:
            return len(self._owners)

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        """Confirm/retry frozen terminal batches without overrunning close."""

        with self._lock:
            owners = tuple(self._owners.values())
        tasks: list[asyncio.Task[None]] = []
        for owner in owners:
            if owner.state not in {
                ToolExecutionTerminalOwnerState.TERMINAL_CANDIDATE_FROZEN,
                ToolExecutionTerminalOwnerState.COMMIT_OUTCOME_UNKNOWN,
            }:
                continue
            with self._lock:
                if owner.owner_id in self._drain_in_flight:
                    continue
                if self._owners.get(owner.owner_id) is not owner:
                    continue
                self._drain_in_flight.add(owner.owner_id)
            try:
                task = asyncio.create_task(
                    self._reconcile_terminal_owner(
                        owner,
                        deadline_monotonic=deadline_monotonic,
                    ),
                    name=f"tool-terminal-drain:{owner.owner_id}",
                )
            except BaseException:
                with self._lock:
                    self._drain_in_flight.discard(owner.owner_id)
                continue
            task.add_done_callback(_observe_background_task)
            tasks.append(task)
        for task in tasks:
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if self.active_owner_count():
            raise ToolExecutionTerminalDrainBlocked(
                "tool execution terminal owners remain active or untrusted"
            )

    async def _reconcile_terminal_owner(
        self,
        owner: ToolExecutionTerminalOwner,
        *,
        deadline_monotonic: float,
    ) -> None:
        try:
            candidates = owner.stable_candidates
            if not candidates:
                return
            terminals = tuple(
                event for event in candidates if isinstance(event, ToolResultEndEvent)
            )
            if len(terminals) != 1:
                self._runtime_session.latch_event_commit_outcome_unknown()
                return
            terminal = terminals[0]
            physical_reservation = (
                self._runtime_session.physical_reservation_for_owner(
                    operation_kind=PhysicalOperationKind.TOOL_CALL,
                    owner_id=owner.tool_call_id,
                )
            )
            if physical_reservation is not None:
                terminal_outcome = {
                    "success": "completed",
                    "denied": "denied",
                    "interrupted": "cancelled",
                    "error": "runtime_error",
                }[terminal.state.value]
                result = await _execute_runtime_event_write(
                    self._runtime_session,
                    lambda: (
                        self._runtime_session.settle_physical_operation_from_thread(
                            candidates,
                            reservation=physical_reservation,
                            terminal_outcome=terminal_outcome,
                            state=None,
                        )
                    ),
                    deadline_monotonic=deadline_monotonic,
                    operation_kind=PhysicalOperationKind.TOOL_CALL,
                    operation_owner_id=owner.tool_call_id,
                )
            else:
                account = self._runtime_session.materialization_account_store.snapshot()
                if account is not None and any(
                    item.owner_kind is PhysicalOperationKind.TOOL_CALL
                    and item.owner_id == owner.tool_call_id
                    for item in account.active_reservations
                ):
                    self._runtime_session.latch_event_commit_outcome_unknown()
                    return
                result = await _execute_runtime_event_write(
                    self._runtime_session,
                    lambda: (
                        self._runtime_session.confirm_and_handoff_event_batch_from_thread(
                            candidates,
                            state=None,
                        )
                    ),
                    deadline_monotonic=deadline_monotonic,
                )
            if result.reconciliation_required or result.reducer_errors:
                return
            with self._lock:
                current = self._owners.get(owner.owner_id)
                if current is owner:
                    self._remove_locked(owner)
        finally:
            with self._lock:
                self._drain_in_flight.discard(owner.owner_id)

    def _freeze(
        self,
        *,
        run_id: str,
        reservation: RolloutReservationFact,
        candidates: Sequence[AgentEvent],
        target: ToolExecutionTerminalOwnerState,
        allowed: tuple[ToolExecutionTerminalOwnerState, ...],
    ) -> ToolExecutionTerminalOwner:
        frozen = tuple(candidates)
        if not frozen:
            raise ToolExecutionCommitContractError(
                "tool owner cannot freeze an empty candidate batch"
            )
        with self._lock:
            owner = self._require_owner_locked(run_id, reservation.owner_id)
            self._require_reservation(owner, reservation)
            if owner.state not in allowed:
                raise ToolExecutionCommitContractError(
                    f"tool owner cannot freeze candidates from {owner.state}"
                )
            owner.state = target
            owner.stable_candidates = frozen
            owner.attempt_generation += 1
            return owner

    def _owner_for_call_locked(
        self,
        run_id: str,
        tool_call_id: str,
    ) -> ToolExecutionTerminalOwner | None:
        owner_id = self._owner_id_by_call.get((run_id, tool_call_id))
        return self._owners.get(owner_id) if owner_id is not None else None

    def _require_owner_locked(
        self,
        run_id: str,
        tool_call_id: str,
    ) -> ToolExecutionTerminalOwner:
        owner = self._owner_for_call_locked(run_id, tool_call_id)
        if owner is None:
            raise ToolExecutionCommitContractError(
                "tool execution terminal owner is missing"
            )
        return owner

    @staticmethod
    def _require_reservation(
        owner: ToolExecutionTerminalOwner,
        reservation: RolloutReservationFact,
    ) -> None:
        if (
            owner.reservation_id != reservation.reservation_id
            or owner.reservation_fingerprint != reservation.semantic_fingerprint
        ):
            raise ToolExecutionCommitContractError(
                "tool terminal owner reservation identity mismatch"
            )

    def _remove_locked(self, owner: ToolExecutionTerminalOwner) -> None:
        if self._owners.get(owner.owner_id) is owner:
            self._owners.pop(owner.owner_id, None)
            self._owner_id_by_call.pop((owner.run_id, owner.tool_call_id), None)


def _observe_background_task(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _execute_runtime_event_write(
    runtime_session: RuntimeSession,
    operation,
    *,
    deadline_monotonic: float,
    operation_kind: PhysicalOperationKind | None = None,
    operation_owner_id: str | None = None,
) -> EventWriteResult:
    """Recover the writer-owned FULL result when its async waiter is cancelled."""

    from pulsara_agent.runtime.event_write_service import RuntimeEventWriteCancelled
    from pulsara_agent.runtime.session import EventWriteResult

    if (operation_kind is None) != (operation_owner_id is None):
        raise ValueError(
            "physical operation continuation requires kind and owner together"
        )
    admission_kwargs = {}
    if operation_kind is not None and operation_owner_id is not None:
        from pulsara_agent.primitives.authority_materialization import (
            LedgerWriteAdmissionClass,
        )

        admission_kwargs = {
            "admission_class": LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
            "operation_owner_id": (
                runtime_session.physical_operation_admission_owner_id(
                    operation_kind=operation_kind,
                    owner_id=operation_owner_id,
                )
            ),
        }

    try:
        return await runtime_session.event_write_service.execute(
            operation,
            deadline_monotonic=deadline_monotonic,
            **admission_kwargs,
        )
    except RuntimeEventWriteCancelled as cancelled:
        if isinstance(cancelled.operation_result, EventWriteResult):
            return cancelled.operation_result
        raise


def _tool_physical_reservation_id(*, run_id: str, tool_call_id: str) -> str:
    digest = sha256(f"{run_id}\x00{tool_call_id}".encode("utf-8")).hexdigest()
    return f"tool_physical:{digest}"



def build_tool_result_terminal_event(
    *,
    event_context: EventContext,
    prepared: PreparedToolTerminalResult,
) -> ToolResultEndCandidate:
    """Lower executor-owned terminal facts into one runtime-owned event candidate."""

    semantics = prepared.semantics
    return ToolResultEndCandidate(
        id=(
            f"tool_result_end:{event_context.run_id}:"
            f"{prepared.tool_call_id}"
        ),
        run_id=event_context.run_id,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        created_at=prepared.created_at,
        metadata={},
        tool_call_id=prepared.tool_call_id,
        state=prepared.state,
        artifacts=tuple(prepared.artifacts),
        observation_timing=prepared.observation_timing,
        execution_semantics=semantics,
        terminal_process_observation_receipt=(
            prepared.terminal_process_observation_receipt
        ),
        terminal_process_monitor_registration=(
            None
            if prepared.prepared_terminal_monitor_registration is None
            else prepared.prepared_terminal_monitor_registration.registration_semantic
        ),
        terminal_process_monitor_cancellation=(
            None
            if prepared.prepared_terminal_monitor_cancellation is None
            else prepared.prepared_terminal_monitor_cancellation.cancellation_semantic
        ),
    )


@dataclass(frozen=True, slots=True)
class RuntimeSessionToolExecutionEventCommitPort:
    runtime_session: RuntimeSession
    state: LoopState

    async def commit_gate_and_reservation(
        self,
        *,
        gate_candidate: CapabilityGateDecisionEvent,
        reservation_candidate: RolloutBudgetReservationCreatedEvent,
        expected_account_state_fingerprint: str,
    ) -> EventWriteResult:
        if gate_candidate.decision != "allow":
            raise ToolExecutionCommitContractError(
                "tool reservation requires an allow gate fact"
            )
        reservation = reservation_candidate.reservation
        if (
            reservation.owner_kind != "tool_call"
            or reservation.owner_id != gate_candidate.tool_call_id
        ):
            raise ToolExecutionCommitContractError(
                "tool reservation does not match gate call identity"
            )
        self._require_account_state(
            reservation.account_id,
            expected_account_state_fingerprint,
        )
        return await self._write_gate_items(
            ((gate_candidate, reservation_candidate, ()),)
        )

    async def commit_gate_and_reservation_batch(
        self,
        *,
        admission_candidates: Sequence[
            tuple[CapabilityGateDecisionEvent, RolloutBudgetReservationCreatedEvent]
        ],
        expected_account_state_fingerprint: str,
    ) -> EventWriteResult:
        """Commit one executable tool batch without exposing a partial admission.

        Calls in a concurrency-safe batch start together.  Their gate and budget
        facts therefore share one durability boundary: a later sibling cannot
        fail admission after an earlier sibling has already left an active
        reservation behind.
        """

        if not admission_candidates:
            raise ToolExecutionCommitContractError(
                "tool admission batch cannot be empty"
            )
        account_ids: set[str] = set()
        tool_call_ids: set[str] = set()
        reservation_ids: set[str] = set()
        events: list[AgentEvent] = []
        for gate_candidate, reservation_candidate in admission_candidates:
            if gate_candidate.decision != "allow":
                raise ToolExecutionCommitContractError(
                    "tool reservation batch requires allow gate facts"
                )
            reservation = reservation_candidate.reservation
            if (
                reservation.owner_kind != "tool_call"
                or reservation.owner_id != gate_candidate.tool_call_id
            ):
                raise ToolExecutionCommitContractError(
                    "tool reservation batch identity mismatch"
                )
            if gate_candidate.tool_call_id in tool_call_ids:
                raise ToolExecutionCommitContractError(
                    "tool reservation batch contains duplicate call identity"
                )
            if reservation.reservation_id in reservation_ids:
                raise ToolExecutionCommitContractError(
                    "tool reservation batch contains duplicate reservation identity"
                )
            account_ids.add(reservation.account_id)
            tool_call_ids.add(gate_candidate.tool_call_id)
            reservation_ids.add(reservation.reservation_id)
            events.extend((gate_candidate, reservation_candidate))
        if len(account_ids) != 1:
            raise ToolExecutionCommitContractError(
                "tool reservation batch spans multiple rollout accounts"
            )
        self._require_account_state(
            next(iter(account_ids)),
            expected_account_state_fingerprint,
        )
        return await self._write_gate_items(
            tuple((gate, reservation, ()) for gate, reservation in admission_candidates)
        )

    async def commit_gate_batch(
        self,
        *,
        gate_items: Sequence[
            tuple[
                CapabilityGateDecisionEvent,
                RolloutBudgetReservationCreatedEvent | None,
                Sequence[AgentEvent],
            ]
        ],
        expected_account_state_fingerprint: str,
        account_id: str,
    ) -> EventWriteResult:
        """Commit a mixed allow/deny tool batch in provider order."""

        if not gate_items:
            raise ToolExecutionCommitContractError("tool gate batch cannot be empty")
        tool_call_ids: set[str] = set()
        reservation_ids: set[str] = set()
        events: list[AgentEvent] = []
        for gate, reservation_event, denied_events in gate_items:
            if gate.tool_call_id in tool_call_ids:
                raise ToolExecutionCommitContractError(
                    "tool gate batch contains duplicate call identity"
                )
            tool_call_ids.add(gate.tool_call_id)
            if gate.decision == "allow":
                if reservation_event is None or denied_events:
                    raise ToolExecutionCommitContractError(
                        "allowed tool gate requires exactly one reservation"
                    )
                reservation = reservation_event.reservation
                if (
                    reservation.account_id != account_id
                    or reservation.owner_kind != "tool_call"
                    or reservation.owner_id != gate.tool_call_id
                    or reservation.reservation_id in reservation_ids
                ):
                    raise ToolExecutionCommitContractError(
                        "allowed tool gate reservation identity mismatch"
                    )
                reservation_ids.add(reservation.reservation_id)
                events.extend((gate, reservation_event))
                continue
            if gate.decision != "deny" or reservation_event is not None:
                raise ToolExecutionCommitContractError(
                    "tool gate batch supports only allow-with-reservation or deny"
                )
            ends = tuple(
                event
                for event in denied_events
                if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
            )
            if len(ends) != 1 or ends[0].tool_call_id != gate.tool_call_id:
                raise ToolExecutionCommitContractError(
                    "denied tool gate requires one matching terminal fact"
                )
            events.extend((gate, *denied_events))
        self._require_account_state(account_id, expected_account_state_fingerprint)
        return await self._write_gate_items(gate_items)

    async def commit_terminal_and_settlement(
        self,
        *,
        terminal_candidate: ToolResultEndCandidate,
        settlement_candidate: RolloutBudgetReservationSettledEvent,
        expected_reservation_fingerprint: str,
    ) -> EventWriteResult:
        reservation = self._active_reservation(
            settlement_candidate.reservation_id,
            expected_reservation_fingerprint,
        )
        if (
            reservation.owner_kind != "tool_call"
            or reservation.owner_id != terminal_candidate.tool_call_id
            or settlement_candidate.source_tool_result_event_id != terminal_candidate.id
        ):
            raise ToolExecutionCommitContractError(
                "tool terminal settlement identity mismatch"
            )
        return await self._write_terminal_batch(
            (terminal_candidate, settlement_candidate),
            terminal=terminal_candidate,
        )

    async def commit_terminal_batch_and_settlement(
        self,
        *,
        terminal_candidates: Sequence[AgentEvent | ToolResultEndCandidate],
        settlement_candidate: RolloutBudgetReservationSettledEvent,
        expected_reservation_fingerprint: str,
    ) -> EventWriteResult:
        ends = tuple(
            event
            for event in terminal_candidates
            if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
        )
        if len(ends) != 1:
            raise ToolExecutionCommitContractError(
                "tool terminal batch requires one terminal fact"
            )
        terminal = ends[0]
        reservation = self._active_reservation(
            settlement_candidate.reservation_id,
            expected_reservation_fingerprint,
        )
        if (
            reservation.owner_kind != "tool_call"
            or reservation.owner_id != terminal.tool_call_id
            or settlement_candidate.source_tool_result_event_id != terminal.id
        ):
            raise ToolExecutionCommitContractError(
                "tool terminal batch settlement identity mismatch"
            )
        gates = tuple(
            event
            for event in terminal_candidates
            if isinstance(event, CapabilityGateDecisionEvent)
        )
        if len(gates) > 1 or any(
            gate.tool_call_id != terminal.tool_call_id for gate in gates
        ):
            raise ToolExecutionCommitContractError(
                "tool terminal batch gate identity mismatch"
            )
        return await self._write_terminal_batch(
            (*terminal_candidates, settlement_candidate),
            terminal=terminal,
        )

    async def commit_gate_and_denial(
        self,
        *,
        gate_candidate: CapabilityGateDecisionEvent,
        denied_terminal_candidates: Sequence[AgentEvent | ToolResultEndCandidate],
        expected_account_state_fingerprint: str,
        account_id: str,
    ) -> EventWriteResult:
        if gate_candidate.decision != "deny":
            raise ToolExecutionCommitContractError(
                "tool denial batch requires a deny gate fact"
            )
        ends = tuple(
            event
            for event in denied_terminal_candidates
            if isinstance(event, (ToolResultEndEvent, ToolResultEndCandidate))
        )
        if len(ends) != 1 or ends[0].tool_call_id != gate_candidate.tool_call_id:
            raise ToolExecutionCommitContractError(
                "tool denial batch requires one matching terminal fact"
            )
        self._require_account_state(account_id, expected_account_state_fingerprint)
        return await self._write_gate_items(
            ((gate_candidate, None, tuple(denied_terminal_candidates)),)
        )

    async def _write_gate_items(
        self,
        gate_items: Sequence[
            tuple[
                CapabilityGateDecisionEvent,
                RolloutBudgetReservationCreatedEvent | None,
                Sequence[AgentEvent],
            ]
        ],
    ) -> EventWriteResult:
        events: list[AgentEvent] = []
        decisions: dict[str, str] = {}
        for gate, reservation, denied in gate_items:
            decisions[gate.tool_call_id] = gate.decision
            events.append(gate)
            if reservation is not None:
                events.append(reservation)
            events.extend(denied)
        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        prepared = await self.runtime_session.tool_terminal_projection_service.prepare_batch(
            tuple(events),
            deadline_monotonic=deadline,
        )
        by_call: dict[str, list[AgentEvent]] = {
            tool_call_id: [] for tool_call_id in decisions
        }
        for event in prepared:
            tool_call_id = getattr(event, "tool_call_id", None)
            if isinstance(event, RolloutBudgetReservationCreatedEvent):
                tool_call_id = event.reservation.owner_id
            if not isinstance(tool_call_id, str) or tool_call_id not in by_call:
                raise ToolExecutionCommitContractError(
                    "prepared tool gate fact cannot be attributed to one call"
                )
            by_call[tool_call_id].append(event)
        tool_contract = (
            self.runtime_session.authority_materialization_contracts.burst_registry
            .unique_binding_for_operation(PhysicalOperationKind.TOOL_CALL)
            .contract
        )
        dispatch_requests = tuple(
            PhysicalDispatchReservationRequest(
                reservation_id=_tool_physical_reservation_id(
                    run_id=self.state.run_id,
                    tool_call_id=tool_call_id,
                ),
                owner_id=tool_call_id,
                burst_contract=tool_contract,
                business_event_ids=tuple(event.id for event in by_call[tool_call_id]),
            )
            for tool_call_id, decision in decisions.items()
            if decision == "allow"
        )
        denied_event_ids = tuple(
            event.id
            for tool_call_id, decision in decisions.items()
            if decision == "deny"
            for event in by_call[tool_call_id]
        )
        if not dispatch_requests:
            return await self._write(prepared)
        one_shot_request = None
        if denied_event_ids:
            denied_digest = sha256("\x1f".join(denied_event_ids).encode()).hexdigest()
            runtime_contract = (
                self.runtime_session.authority_materialization_contracts.burst_registry
                .unique_binding_for_operation(
                    PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
                )
                .contract
            )
            one_shot_request = PhysicalOneShotReservationRequest(
                reservation_id=f"tool_deny_physical:{denied_digest}",
                owner_id=f"tool-deny-batch:{denied_digest}",
                burst_contract=runtime_contract,
                business_event_ids=denied_event_ids,
                terminal_outcome="denied",
            )
        self.runtime_session.publisher.bind_running_loop()

        def commit_gate_batch():
            _, result = self.runtime_session.reserve_physical_operation_batch_from_thread(
                prepared,
                dispatch_requests=dispatch_requests,
                one_shot_request=one_shot_request,
                state=self.state,
            )
            return result

        return await _execute_runtime_event_write(
            self.runtime_session,
            commit_gate_batch,
            deadline_monotonic=deadline,
        )

    async def commit_suspension(
        self,
        *,
        suspension_candidate: ToolExecutionSuspendedEvent,
        reservation_id: str,
        expected_reservation_fingerprint: str,
    ) -> EventWriteResult:
        reservation = self._active_reservation(
            reservation_id,
            expected_reservation_fingerprint,
        )
        if reservation.owner_id != suspension_candidate.tool_call_id:
            raise ToolExecutionCommitContractError(
                "tool suspension reservation identity mismatch"
            )
        physical_reservation = self.runtime_session.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.TOOL_CALL,
            owner_id=suspension_candidate.tool_call_id,
        )
        if physical_reservation is None:
            if (
                self.runtime_session.materialization_account_store.snapshot() is None
                and self.runtime_session.allow_unbootstrapped_test_events
            ):
                return await self._write((suspension_candidate,))
            raise ToolExecutionCommitContractError(
                "tool suspension lacks its active physical reservation"
            )
        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        binding_fingerprint = context_fingerprint(
            "tool-physical-suspension-binding:v1",
            {
                "interaction_kind": suspension_candidate.interaction_kind,
                "tool_call_id": suspension_candidate.tool_call_id,
                "mcp_binding_identity": suspension_candidate.payload.get(
                    "mcp_binding_identity"
                ),
                "mcp_pending_lease_reservation_id": suspension_candidate.payload.get(
                    "mcp_pending_lease_reservation_id"
                ),
            },
        )
        suspension_id = str(
            suspension_candidate.payload.get("interaction_id")
            or suspension_candidate.id
        )
        self.runtime_session.publisher.bind_running_loop()

        def commit_suspension_batch():
            return self.runtime_session.suspend_physical_operation_from_thread(
                (suspension_candidate,),
                reservation=physical_reservation,
                suspension_id=suspension_id,
                binding_identity_fingerprint=binding_fingerprint,
                state=self.state,
            )

        return await _execute_runtime_event_write(
            self.runtime_session,
            commit_suspension_batch,
            deadline_monotonic=deadline,
            operation_kind=PhysicalOperationKind.TOOL_CALL,
            operation_owner_id=suspension_candidate.tool_call_id,
        )

    async def _write_terminal_batch(
        self,
        events: tuple[AgentEvent, ...],
        *,
        terminal: ToolResultEndEvent | ToolResultEndCandidate,
    ) -> EventWriteResult:
        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        prepared = await self.runtime_session.tool_terminal_projection_service.prepare_batch(
            events,
            deadline_monotonic=deadline,
        )
        physical_reservation = self.runtime_session.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.TOOL_CALL,
            owner_id=terminal.tool_call_id,
        )
        if physical_reservation is None:
            if (
                self.runtime_session.materialization_account_store.snapshot() is None
                and self.runtime_session.allow_unbootstrapped_test_events
            ):
                return await self._write(prepared, retry_on_write_conflict=True)
            raise ToolExecutionCommitContractError(
                "tool terminal lacks its active physical reservation"
            )
        terminal_outcome = {
            "success": "completed",
            "denied": "denied",
            "interrupted": "cancelled",
            "error": "runtime_error",
        }[terminal.state.value]
        self.runtime_session.publisher.bind_running_loop()

        def commit_terminal_batch():
            return self.runtime_session.settle_physical_operation_from_thread(
                prepared,
                reservation=physical_reservation,
                terminal_outcome=terminal_outcome,
                state=self.state,
            )

        return await _execute_runtime_event_write(
            self.runtime_session,
            commit_terminal_batch,
            deadline_monotonic=deadline,
            operation_kind=PhysicalOperationKind.TOOL_CALL,
            operation_owner_id=terminal.tool_call_id,
        )

    async def _write(
        self,
        events: tuple[AgentEvent, ...],
        *,
        retry_on_write_conflict: bool = False,
    ) -> EventWriteResult:
        self.runtime_session.publisher.bind_running_loop()
        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        events = await self.runtime_session.tool_terminal_projection_service.prepare_batch(
            events,
            deadline_monotonic=deadline,
        )
        for attempt in range(8):
            expected_sequence = (
                self.runtime_session.long_horizon_state_store.through_sequence
            )
            def commit_or_confirm() -> EventWriteResult:
                try:
                    return self.runtime_session.write_events_from_thread(
                        events,
                        expected_last_sequence=expected_sequence,
                        state=self.state,
                    )
                except BaseException as original:
                    try:
                        return self.runtime_session.confirm_and_handoff_event_batch(
                            events,
                            state=self.state,
                        )
                    except Exception as confirmation_error:
                        from pulsara_agent.runtime.session import EventCommitError

                        if isinstance(confirmation_error, EventCommitError):
                            raise original
                        self.runtime_session.latch_event_commit_outcome_unknown()
                        raise
            try:
                return await _execute_runtime_event_write(
                    self.runtime_session,
                    commit_or_confirm,
                    deadline_monotonic=deadline,
                )
            except BaseException as original:
                from pulsara_agent.runtime.session import EventWriteConflict

                if (
                    retry_on_write_conflict
                    and isinstance(original, EventWriteConflict)
                    and attempt < 7
                ):
                    continue
                raise
        raise AssertionError("unreachable tool event commit retry loop")

    def _require_account_state(
        self,
        account_id: str,
        expected_fingerprint: str,
    ) -> None:
        state = self.runtime_session.long_horizon_state_store.rollout_state(account_id)
        if state is not None:
            actual_fingerprint = state.state_fingerprint
        else:
            binding = resolve_run_rollout_binding(
                self.runtime_session,
                run_id=self.state.run_id,
            )
            if binding.account.account_id != account_id:
                raise ToolExecutionCommitContractError(
                    "tool execution rollout account identity mismatch"
                )
            actual_fingerprint = binding.parent_state.state_fingerprint
        if actual_fingerprint != expected_fingerprint:
            raise ToolExecutionCommitContractError(
                "tool execution rollout account CAS mismatch"
            )

    def _active_reservation(
        self,
        reservation_id: str,
        expected_fingerprint: str,
    ):
        for state in self.runtime_session.long_horizon_state_store.rollout_states():
            for reservation in state.active_reservations:
                if reservation.reservation_id != reservation_id:
                    continue
                if reservation.semantic_fingerprint != expected_fingerprint:
                    raise ToolExecutionCommitContractError(
                        "tool execution reservation fingerprint mismatch"
                    )
                return reservation
        binding = resolve_run_rollout_binding(
            self.runtime_session,
            run_id=self.state.run_id,
        )
        if binding.child_state is not None:
            for reservation in binding.child_state.active_reservations:
                if reservation.reservation_id != reservation_id:
                    continue
                if reservation.semantic_fingerprint != expected_fingerprint:
                    raise ToolExecutionCommitContractError(
                        "tool execution reservation fingerprint mismatch"
                    )
                return reservation
        raise ToolExecutionCommitContractError(
            "tool execution reservation is not active"
        )


__all__ = [
    "RuntimeSessionToolExecutionEventCommitPort",
    "ToolExecutionCommitContractError",
    "ToolExecutionTerminalDrainBlocked",
    "ToolExecutionTerminalOwner",
    "ToolExecutionTerminalOwnerState",
    "ToolExecutionTerminalRegistry",
    "build_tool_result_terminal_event",
]
