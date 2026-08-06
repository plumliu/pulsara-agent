"""Process-local child admission, composition, and activation-operation owners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Callable, Literal
from uuid import uuid4

from pulsara_agent.ports.run_execution import LedgerHorizonFact
from pulsara_agent.ports.runtime_session_teardown import (
    NonHostRuntimeSessionTeardownCapability,
    NonHostRuntimeSessionTeardownPurpose,
    NonHostRuntimeSessionTeardownReconciliationRequired,
    NonHostRuntimeSessionTeardownRetryableError,
    bind_non_host_runtime_session_teardown_capability,
)
from pulsara_agent.ports.subagent import (
    ChildCapacitySlot,
    LiveChildCapacityReservationSlot,
    RecoveredChildCapacityOccupancySlot,
    RecoveredChildOccupancyProof,
    ReleasedChildCapacitySlot,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.mcp.types import McpBindingIdentity
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.subagent.facts import SubagentGraphState

if TYPE_CHECKING:
    from pulsara_agent.runtime.run_execution.factory import RunActivationComposition


_CHILD_TEARDOWN_MAX_PHYSICAL_ATTEMPTS = 3
_CHILD_TEARDOWN_RETRY_BACKOFF_SECONDS = (0.05, 0.10)


def _bind_child_teardown_capability(
    session: RuntimeSession | None,
) -> NonHostRuntimeSessionTeardownCapability | None:
    if session is None:
        return None
    return bind_non_host_runtime_session_teardown_capability(
        session,
        purpose=NonHostRuntimeSessionTeardownPurpose.CHILD_TERMINAL,
    )


@dataclass(slots=True)
class ChildCapacityReservation:
    """One live admission reservation; it is never reconstructed after restart."""

    reservation_id: str
    parent_run_id: str
    count: int
    generation: int = 1
    attached_run_ids: set[str] = field(default_factory=set)
    released_run_ids: set[str] = field(default_factory=set)
    uncommitted_released: bool = False
    released: bool = False
    commit_state: Literal["pending", "full", "none", "unknown"] = "pending"

    @property
    def uncommitted_count(self) -> int:
        if self.released or self.uncommitted_released:
            return 0
        return max(0, self.count - len(self.attached_run_ids))

    @property
    def active_slot_count(self) -> int:
        if self.released:
            return 0
        return max(0, len(self.attached_run_ids - self.released_run_ids))

    def release(self) -> None:
        self.uncommitted_released = True


@dataclass(slots=True)
class ParentSubagentGraphSlot:
    parent_runtime_session_id: str
    parent_run_id: str
    subagent_run_id: str
    spawn_edge_id: str
    generation: int
    state: Literal[
        "active",
        "terminal_settlement_pending",
        "terminal_settlement_full",
        "reconciliation_required",
    ]
    source_horizon: LedgerHorizonFact
    slot_fingerprint: str


@dataclass(slots=True)
class ChildRuntimeCompositionLease:
    lease_id: str
    child_runtime_session_id: str
    generation: int
    state: Literal["active", "closing", "released"]
    child_session: RuntimeSession | None
    physical_teardown_capability: NonHostRuntimeSessionTeardownCapability | None
    physical_teardown_state: Literal[
        "active",
        "closing",
        "retry_wait",
        "closed",
        "reconciliation_required",
    ] = "active"
    physical_teardown_generation: int = 0
    physical_teardown_task: asyncio.Task[None] | None = None
    physical_teardown_failure_code: str | None = None
    composition: RunActivationComposition | None = None


ChildAdmissionSettlementState = Literal[
    "active",
    "child_terminal_full",
    "parent_graph_pending",
    "composition_closing",
    "capacity_releasing",
    "released",
    "reconciliation_required",
]


@dataclass(slots=True)
class ChildAdmissionSessionOwner:
    """Own admission resources only; never an activation task or run handles."""

    subagent_run_id: str
    child_runtime_session_id: str
    capacity_slot: ChildCapacitySlot
    parent_graph_slot: ParentSubagentGraphSlot
    child_composition_lease: ChildRuntimeCompositionLease
    settlement_state: ChildAdmissionSettlementState
    activation_operation_state: Literal["not_started", "running", "exited"]
    started_in_process_at: datetime
    mcp_binding_identities: frozenset[McpBindingIdentity] = frozenset()


@dataclass(frozen=True, slots=True)
class ChildAdmissionDiagnostic:
    code: str
    subagent_run_id: str
    child_runtime_session_id: str | None
    severity: Literal["warning", "error"] = "error"


@dataclass(slots=True)
class ChildActivationOperation:
    """The only owner of the process task that drives child activation setup."""

    subagent_run_id: str
    generation: int
    task: asyncio.Task[None]
    state: Literal["running", "cancelling", "exited"] = "running"


class ChildActivationOperationRegistry:
    """Own child orchestration tasks independently from admission/session state."""

    def __init__(
        self,
        *,
        on_started: Callable[[str], None],
        on_exited: Callable[[str], None],
    ) -> None:
        self._lock = RLock()
        self._operations: dict[str, ChildActivationOperation] = {}
        self._generations: dict[str, int] = {}
        self._on_started = on_started
        self._on_exited = on_exited

    def install(self, subagent_run_id: str, task: asyncio.Task[None]) -> None:
        with self._lock:
            current = self._operations.get(subagent_run_id)
            if current is not None and not current.task.done():
                raise ValueError(
                    f"Child activation operation already exists: {subagent_run_id}"
                )
            generation = self._generations.get(subagent_run_id, 0) + 1
            self._generations[subagent_run_id] = generation
            operation = ChildActivationOperation(
                subagent_run_id=subagent_run_id,
                generation=generation,
                task=task,
            )
            self._operations[subagent_run_id] = operation
        self._on_started(subagent_run_id)
        task.add_done_callback(
            lambda completed, run_id=subagent_run_id, exact=operation: (
                self._operation_done(run_id, exact, completed)
            )
        )

    def task(self, subagent_run_id: str) -> asyncio.Task[None] | None:
        with self._lock:
            operation = self._operations.get(subagent_run_id)
            return operation.task if operation is not None else None

    def operations(self) -> tuple[ChildActivationOperation, ...]:
        with self._lock:
            return tuple(self._operations.values())

    def request_cancel(self, subagent_run_id: str) -> asyncio.Task[None] | None:
        with self._lock:
            operation = self._operations.get(subagent_run_id)
            if operation is None:
                return None
            operation.state = "cancelling"
            task = operation.task
        _cancel_task_on_owner_loop(task)
        return task

    def cancel_now(self, subagent_run_id: str) -> None:
        self.request_cancel(subagent_run_id)

    async def cancel(
        self,
        subagent_run_id: str,
        *,
        timeout_seconds: float | None,
    ) -> None:
        task = self.request_cancel(subagent_run_id)
        if task is None:
            return
        if not await _wait_for_task_completion(task, timeout_seconds=timeout_seconds):
            raise TimeoutError(
                f"Timed out draining child activation for {subagent_run_id}"
            )

    async def cancel_for_terminal_handoff(
        self,
        subagent_run_id: str,
        *,
        timeout_seconds: float | None,
    ) -> None:
        await self.cancel(
            subagent_run_id,
            timeout_seconds=timeout_seconds,
        )

    async def wait_run_ids(
        self,
        subagent_run_ids: tuple[str, ...],
        *,
        timeout_seconds: float | None,
    ) -> None:
        with self._lock:
            tasks = {
                run_id: operation.task
                for run_id in subagent_run_ids
                if (operation := self._operations.get(run_id)) is not None
            }
        await _wait_for_tasks(
            tasks,
            timeout_seconds=timeout_seconds,
            timeout_message="Timed out waiting for child activations",
        )

    async def drain_run_ids(
        self,
        subagent_run_ids: tuple[str, ...],
        *,
        timeout_seconds: float | None,
    ) -> None:
        tasks: dict[str, asyncio.Task[None]] = {}
        for run_id in subagent_run_ids:
            task = self.request_cancel(run_id)
            if task is not None:
                tasks[run_id] = task
        await _wait_for_tasks(
            tasks,
            timeout_seconds=timeout_seconds,
            timeout_message="Timed out draining child activations",
        )

    async def drain(self, *, timeout_seconds: float | None) -> None:
        await self.drain_run_ids(
            tuple(item.subagent_run_id for item in self.operations()),
            timeout_seconds=timeout_seconds,
        )

    def _operation_done(
        self,
        subagent_run_id: str,
        exact: ChildActivationOperation,
        completed: asyncio.Task[None],
    ) -> None:
        with self._lock:
            current = self._operations.get(subagent_run_id)
            if current is not exact or current.task is not completed:
                return
            current.state = "exited"
            self._operations.pop(subagent_run_id, None)
        self._on_exited(subagent_run_id)


class ChildAdmissionSessionRegistry:
    """Own child capacity, graph settlement, and composition leases only."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: dict[str, ChildAdmissionSessionOwner] = {}
        self._reservations: dict[str, ChildCapacityReservation] = {}
        self._child_ids_by_mcp_binding_identity: dict[McpBindingIdentity, set[str]] = {}

    def reserve(self, *, parent_run_id: str, count: int) -> ChildCapacityReservation:
        if count < 1:
            raise ValueError("reservation count must be >= 1")
        reservation = ChildCapacityReservation(
            reservation_id=f"subagent_capacity:{uuid4().hex}",
            parent_run_id=parent_run_id,
            count=count,
        )
        with self._lock:
            self._reservations[reservation.reservation_id] = reservation
        return reservation

    def register_prepared(
        self,
        *,
        subagent_run_id: str,
        child_runtime_session_id: str,
        child_session: RuntimeSession | None,
        reservation: ChildCapacityReservation,
        parent_runtime_session_id: str,
        parent_run_id: str,
        spawn_edge_id: str,
        parent_graph_horizon: LedgerHorizonFact,
        parent_graph_state_fingerprint: str,
        mcp_binding_identities: frozenset[McpBindingIdentity] = frozenset(),
    ) -> ChildAdmissionSessionOwner:
        with self._lock:
            if subagent_run_id in self._owners:
                raise ValueError(
                    f"Child admission owner already exists: {subagent_run_id}"
                )
            resident = self._reservations.get(reservation.reservation_id)
            if resident is not reservation or reservation.released:
                raise ValueError("capacity reservation is not live")
            if reservation.parent_run_id != parent_run_id:
                raise ValueError("capacity reservation parent identity mismatch")
            if len(reservation.attached_run_ids) >= reservation.count:
                raise ValueError("capacity reservation has no remaining slots")
            reservation.attached_run_ids.add(subagent_run_id)
            graph_slot = _parent_graph_slot(
                parent_runtime_session_id=parent_runtime_session_id,
                parent_run_id=parent_run_id,
                subagent_run_id=subagent_run_id,
                spawn_edge_id=spawn_edge_id,
                source_horizon=parent_graph_horizon,
                graph_state_fingerprint=parent_graph_state_fingerprint,
            )
            owner = ChildAdmissionSessionOwner(
                subagent_run_id=subagent_run_id,
                child_runtime_session_id=child_runtime_session_id,
                capacity_slot=LiveChildCapacityReservationSlot(
                    slot_kind="live_reservation",
                    reservation=reservation,
                    reservation_generation=reservation.generation,
                ),
                parent_graph_slot=graph_slot,
                child_composition_lease=ChildRuntimeCompositionLease(
                    lease_id=f"child_composition:{uuid4().hex}",
                    child_runtime_session_id=child_runtime_session_id,
                    generation=1,
                    state="active",
                    child_session=child_session,
                    physical_teardown_capability=(
                        _bind_child_teardown_capability(child_session)
                    ),
                ),
                settlement_state="active",
                activation_operation_state="not_started",
                started_in_process_at=datetime.now(timezone.utc),
                mcp_binding_identities=mcp_binding_identities,
            )
            self._owners[subagent_run_id] = owner
            for identity in mcp_binding_identities:
                self._child_ids_by_mcp_binding_identity.setdefault(identity, set()).add(
                    subagent_run_id
                )
            return owner

    def register_recovered(
        self,
        *,
        proof: RecoveredChildOccupancyProof,
        child_runtime_session_id: str,
        child_session: RuntimeSession | None,
        mcp_binding_identities: frozenset[McpBindingIdentity] = frozenset(),
    ) -> ChildAdmissionSessionOwner:
        """Install the parent-graph-backed capacity barrier before child repair."""

        with self._lock:
            existing = self._owners.get(proof.subagent_run_id)
            if existing is not None:
                slot = existing.capacity_slot
                if (
                    isinstance(slot, RecoveredChildCapacityOccupancySlot)
                    and slot.proof == proof
                ):
                    return existing
                raise RuntimeError("recovered child occupancy proof conflicts")
            occupancy_id = context_fingerprint(
                "recovered-child-occupancy:v1",
                proof.model_dump(mode="json"),
            )
            graph_slot = _parent_graph_slot(
                parent_runtime_session_id=proof.parent_runtime_session_id,
                parent_run_id=proof.parent_run_id,
                subagent_run_id=proof.subagent_run_id,
                spawn_edge_id=proof.spawn_edge_id,
                source_horizon=proof.parent_graph_horizon,
                graph_state_fingerprint=proof.parent_graph_state_fingerprint,
            )
            owner = ChildAdmissionSessionOwner(
                subagent_run_id=proof.subagent_run_id,
                child_runtime_session_id=child_runtime_session_id,
                capacity_slot=RecoveredChildCapacityOccupancySlot(
                    slot_kind="recovered_occupancy",
                    occupancy_id=occupancy_id,
                    proof=proof,
                ),
                parent_graph_slot=graph_slot,
                child_composition_lease=ChildRuntimeCompositionLease(
                    lease_id=f"child_composition:recovered:{uuid4().hex}",
                    child_runtime_session_id=child_runtime_session_id,
                    generation=1,
                    state="active",
                    child_session=child_session,
                    physical_teardown_capability=(
                        _bind_child_teardown_capability(child_session)
                    ),
                ),
                settlement_state="active",
                activation_operation_state="not_started",
                started_in_process_at=datetime.now(timezone.utc),
                mcp_binding_identities=mcp_binding_identities,
            )
            self._owners[proof.subagent_run_id] = owner
            for identity in mcp_binding_identities:
                self._child_ids_by_mcp_binding_identity.setdefault(identity, set()).add(
                    proof.subagent_run_id
                )
            return owner

    def child_ids_for_mcp_bindings(
        self,
        identities: frozenset[McpBindingIdentity],
    ) -> frozenset[str]:
        with self._lock:
            result: set[str] = set()
            for identity in identities:
                result.update(self._child_ids_by_mcp_binding_identity.get(identity, ()))
            return frozenset(result)

    def attach_session(self, subagent_run_id: str, session: RuntimeSession) -> None:
        with self._lock:
            owner = self._owners[subagent_run_id]
            lease = owner.child_composition_lease
            if session.runtime_session_id != lease.child_runtime_session_id:
                raise ValueError("child runtime session identity mismatch")
            if lease.state != "active":
                raise RuntimeError("child composition lease is not active")
            lease.child_session = session
            lease.physical_teardown_capability = _bind_child_teardown_capability(
                session
            )
            lease.physical_teardown_state = "active"
            lease.physical_teardown_generation = 0
            lease.physical_teardown_task = None
            lease.physical_teardown_failure_code = None

    def attach_activation_composition(
        self,
        subagent_run_id: str,
        composition: RunActivationComposition,
    ) -> None:
        with self._lock:
            owner = self._owners[subagent_run_id]
            lease = owner.child_composition_lease
            if lease.composition is not None:
                raise ValueError("child activation composition is already attached")
            if (
                composition.agent_runtime.runtime_session_id
                != lease.child_runtime_session_id
            ):
                raise ValueError("child activation composition identity mismatch")
            lease.composition = composition

    def mark_activation_started(self, subagent_run_id: str) -> None:
        with self._lock:
            owner = self._owners[subagent_run_id]
            if owner.activation_operation_state != "not_started":
                raise RuntimeError("child activation operation state drifted")
            owner.activation_operation_state = "running"

    def mark_activation_exited(self, subagent_run_id: str) -> None:
        with self._lock:
            owner = self._owners.get(subagent_run_id)
            if owner is None:
                return
            owner.activation_operation_state = "exited"
        self._try_release(subagent_run_id)

    def get(self, subagent_run_id: str) -> ChildAdmissionSessionOwner | None:
        with self._lock:
            return self._owners.get(subagent_run_id)

    def owners(self) -> tuple[ChildAdmissionSessionOwner, ...]:
        with self._lock:
            return tuple(self._owners.values())

    def uncommitted_reservation_count(self, *, parent_run_id: str | None = None) -> int:
        with self._lock:
            return sum(
                reservation.uncommitted_count
                for reservation in self._reservations.values()
                if parent_run_id is None or reservation.parent_run_id == parent_run_id
            )

    def release_reservation(self, reservation: ChildCapacityReservation) -> None:
        with self._lock:
            reservation.release()
            if reservation.active_slot_count == 0:
                reservation.released = True
                self._reservations.pop(reservation.reservation_id, None)

    def record_reservation_commit_outcome(
        self,
        reservation: ChildCapacityReservation,
        *,
        status: Literal["full", "none", "unknown"],
    ) -> None:
        with self._lock:
            resident = self._reservations.get(reservation.reservation_id)
            if resident is not reservation:
                if status == "none" and reservation.released:
                    return
                raise RuntimeError("child capacity reservation owner is unavailable")
            if reservation.commit_state not in {"pending", status}:
                raise RuntimeError("child capacity reservation commit outcome drifted")
            reservation.commit_state = status
        if status == "none":
            self.release_reservation(reservation)

    def unresolved_commit_reservations(
        self,
    ) -> tuple[ChildCapacityReservation, ...]:
        with self._lock:
            return tuple(
                reservation
                for reservation in self._reservations.values()
                if reservation.commit_state == "unknown"
                or (
                    reservation.commit_state == "pending"
                    and reservation.uncommitted_count > 0
                )
            )

    def require_no_unresolved_commit_reservations(self) -> None:
        unresolved = self.unresolved_commit_reservations()
        if unresolved:
            identities = ",".join(sorted(item.reservation_id for item in unresolved))
            raise RuntimeError(
                "subagent capacity commit reservations require reconciliation: "
                f"{identities}"
            )

    def occupied_run_ids(self, *, parent_run_id: str | None = None) -> frozenset[str]:
        with self._lock:
            return frozenset(
                owner.subagent_run_id
                for owner in self._owners.values()
                if owner.settlement_state != "released"
                and (
                    parent_run_id is None
                    or owner.parent_graph_slot.parent_run_id == parent_run_id
                )
                and not isinstance(owner.capacity_slot, ReleasedChildCapacitySlot)
            )

    def mark_parent_graph_terminal_full(self, subagent_run_id: str) -> None:
        """Release remains pending until the independent activation operation exits."""

        with self._lock:
            owner = self._owners.get(subagent_run_id)
            if owner is None:
                return
            owner.parent_graph_slot.state = "terminal_settlement_full"
            owner.settlement_state = "child_terminal_full"
            if owner.activation_operation_state == "not_started":
                owner.activation_operation_state = "exited"
        self._try_release(subagent_run_id)

    def mark_parent_graph_reconciliation_required(self, subagent_run_id: str) -> None:
        with self._lock:
            owner = self._owners[subagent_run_id]
            owner.parent_graph_slot.state = "reconciliation_required"
            owner.settlement_state = "reconciliation_required"

    def install_or_get_child_session_teardown_task(
        self,
        subagent_run_id: str,
        *,
        deadline_monotonic: float,
    ) -> asyncio.Task[None] | None:
        """Install the sole teardown lineage owner or return its exact winner."""

        with self._lock:
            owner = self._owners.get(subagent_run_id)
            if owner is None:
                raise RuntimeError("child session owner disappeared before teardown")
            lease = owner.child_composition_lease
            if lease.physical_teardown_state == "closed":
                if lease.physical_teardown_task is not None:
                    raise RuntimeError("closed child teardown retained its task")
                return None
            if lease.physical_teardown_state in {"closing", "retry_wait"}:
                task = lease.physical_teardown_task
                if task is None:
                    raise RuntimeError("active child teardown lineage lost its task")
                if task.done():
                    raise RuntimeError(
                        "active child teardown lineage retained an exited task"
                    )
                return task
            if lease.physical_teardown_state == "reconciliation_required":
                raise NonHostRuntimeSessionTeardownReconciliationRequired(
                    lease.physical_teardown_failure_code
                    or "child RuntimeSession teardown requires reconciliation"
                )
            if lease.physical_teardown_task is not None:
                raise RuntimeError("settled child teardown unexpectedly owns a task")
            if lease.physical_teardown_state != "active":
                raise RuntimeError("child teardown state is unsupported")
            capability = lease.physical_teardown_capability
            if capability is None:
                raise RuntimeError("child teardown capability is unavailable")
            if capability.runtime_session_id != lease.child_runtime_session_id:
                raise RuntimeError("child teardown capability identity drifted")
            if (
                capability.purpose
                is not NonHostRuntimeSessionTeardownPurpose.CHILD_TERMINAL
            ):
                raise RuntimeError("child teardown capability purpose drifted")
            lease.physical_teardown_generation += 1
            generation = lease.physical_teardown_generation
            lease.physical_teardown_failure_code = None

            async def run_teardown_lineage() -> None:
                attempt_generation = generation
                for attempt_index in range(_CHILD_TEARDOWN_MAX_PHYSICAL_ATTEMPTS):
                    loop = asyncio.get_running_loop()
                    attempt_deadline = deadline_monotonic
                    retryable_error: BaseException | None = None
                    try:
                        if attempt_deadline <= loop.time():
                            raise NonHostRuntimeSessionTeardownRetryableError(
                                "child RuntimeSession teardown lineage deadline expired"
                            )
                        await capability.teardown(
                            deadline_monotonic=attempt_deadline,
                        )
                    except NonHostRuntimeSessionTeardownRetryableError as exc:
                        retryable_error = exc
                    except NonHostRuntimeSessionTeardownReconciliationRequired:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code=(
                                "child_runtime_session_teardown_reconciliation_required"
                            ),
                        )
                        raise
                    except asyncio.CancelledError:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code="child_runtime_session_teardown_owner_cancelled",
                        )
                        raise
                    except BaseException as exc:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code=(
                                "child_runtime_session_teardown_reconciliation_required"
                            ),
                        )
                        raise NonHostRuntimeSessionTeardownReconciliationRequired(
                            "child RuntimeSession teardown failed physically"
                        ) from exc
                    else:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="closed",
                            failure_code=None,
                        )
                        self._try_release(subagent_run_id)
                        return

                    if retryable_error is None:
                        raise RuntimeError(
                            "child teardown retry classification drifted"
                        )
                    retry_ordinal = attempt_index + 1
                    if retry_ordinal >= _CHILD_TEARDOWN_MAX_PHYSICAL_ATTEMPTS:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code=(
                                "child_runtime_session_teardown_retry_exhausted"
                            ),
                        )
                        raise NonHostRuntimeSessionTeardownReconciliationRequired(
                            "child RuntimeSession teardown retry budget was exhausted"
                        ) from retryable_error
                    backoff = _CHILD_TEARDOWN_RETRY_BACKOFF_SECONDS[retry_ordinal - 1]
                    if loop.time() + backoff >= deadline_monotonic:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code=(
                                "child_runtime_session_teardown_deadline_exhausted"
                            ),
                        )
                        raise NonHostRuntimeSessionTeardownReconciliationRequired(
                            "child RuntimeSession teardown lineage deadline was exhausted"
                        ) from retryable_error
                    self._mark_child_teardown_retry_wait(
                        subagent_run_id,
                        lease=lease,
                        generation=attempt_generation,
                    )
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        self._settle_child_teardown_task(
                            subagent_run_id,
                            lease=lease,
                            generation=attempt_generation,
                            resulting_state="reconciliation_required",
                            failure_code="child_runtime_session_teardown_owner_cancelled",
                        )
                        raise
                    attempt_generation = self._advance_child_teardown_retry(
                        subagent_run_id,
                        lease=lease,
                        generation=attempt_generation,
                    )

            task = asyncio.create_task(
                run_teardown_lineage(),
                name=f"child-session-teardown-lineage:{subagent_run_id}:{generation}",
            )
            lease.physical_teardown_state = "closing"
            lease.physical_teardown_task = task
            return task

    def _settle_child_teardown_task(
        self,
        subagent_run_id: str,
        *,
        lease: ChildRuntimeCompositionLease,
        generation: int,
        resulting_state: Literal["closed", "reconciliation_required"],
        failure_code: str | None,
    ) -> None:
        with self._lock:
            current = self._owners.get(subagent_run_id)
            if current is None:
                raise RuntimeError("child session owner disappeared during teardown")
            current_lease = current.child_composition_lease
            if (
                current_lease is not lease
                or current_lease.physical_teardown_generation != generation
                or current_lease.physical_teardown_task is not asyncio.current_task()
            ):
                raise RuntimeError("child teardown winner identity drifted")
            current_lease.physical_teardown_state = resulting_state
            current_lease.physical_teardown_task = None
            current_lease.physical_teardown_failure_code = failure_code
            if resulting_state == "reconciliation_required":
                current.settlement_state = "reconciliation_required"

    def _mark_child_teardown_retry_wait(
        self,
        subagent_run_id: str,
        *,
        lease: ChildRuntimeCompositionLease,
        generation: int,
    ) -> None:
        with self._lock:
            current = self._owners.get(subagent_run_id)
            if current is None:
                raise RuntimeError("child session owner disappeared during retry")
            current_lease = current.child_composition_lease
            if (
                current_lease is not lease
                or current_lease.physical_teardown_generation != generation
                or current_lease.physical_teardown_task is not asyncio.current_task()
                or current_lease.physical_teardown_state != "closing"
            ):
                raise RuntimeError("child teardown retry owner identity drifted")
            current_lease.physical_teardown_state = "retry_wait"
            current_lease.physical_teardown_failure_code = (
                "child_runtime_session_teardown_retryable"
            )

    def _advance_child_teardown_retry(
        self,
        subagent_run_id: str,
        *,
        lease: ChildRuntimeCompositionLease,
        generation: int,
    ) -> int:
        with self._lock:
            current = self._owners.get(subagent_run_id)
            if current is None:
                raise RuntimeError("child session owner disappeared during retry")
            current_lease = current.child_composition_lease
            if (
                current_lease is not lease
                or current_lease.physical_teardown_generation != generation
                or current_lease.physical_teardown_task is not asyncio.current_task()
                or current_lease.physical_teardown_state != "retry_wait"
            ):
                raise RuntimeError("child teardown retry generation drifted")
            current_lease.physical_teardown_generation += 1
            current_lease.physical_teardown_state = "closing"
            current_lease.physical_teardown_failure_code = None
            return current_lease.physical_teardown_generation

    def child_session_is_physically_closed(self, subagent_run_id: str) -> bool:
        with self._lock:
            owner = self._owners.get(subagent_run_id)
            if owner is None:
                raise RuntimeError("child session owner is unavailable")
            return owner.child_composition_lease.physical_teardown_state == "closed"

    def reconcile(
        self,
        graph: SubagentGraphState,
        *,
        active_operation_run_ids: frozenset[str] = frozenset(),
    ) -> tuple[ChildAdmissionDiagnostic, ...]:
        diagnostics: list[ChildAdmissionDiagnostic] = []
        owners = {owner.subagent_run_id: owner for owner in self.owners()}
        for run in graph.runs.values():
            owner = owners.pop(run.subagent_run_id, None)
            active = run.status in {"running", "suspended"}
            owner_active = (
                owner is not None
                and owner.settlement_state
                not in {"released", "reconciliation_required"}
                and owner.child_composition_lease.state != "released"
            )
            if active and not owner_active:
                diagnostics.append(
                    ChildAdmissionDiagnostic(
                        code="subagent_active_admission_owner_missing",
                        subagent_run_id=run.subagent_run_id,
                        child_runtime_session_id=run.child_runtime_session_id,
                    )
                )
            elif not active and run.subagent_run_id in active_operation_run_ids:
                diagnostics.append(
                    ChildAdmissionDiagnostic(
                        code="subagent_terminal_activation_operation_active",
                        subagent_run_id=run.subagent_run_id,
                        child_runtime_session_id=run.child_runtime_session_id,
                    )
                )
        for owner in owners.values():
            diagnostics.append(
                ChildAdmissionDiagnostic(
                    code="subagent_admission_registry_orphan_owner",
                    subagent_run_id=owner.subagent_run_id,
                    child_runtime_session_id=owner.child_runtime_session_id,
                )
            )
        return tuple(diagnostics)

    def _try_release(self, subagent_run_id: str) -> None:
        child_session: RuntimeSession | None = None
        with self._lock:
            owner = self._owners.get(subagent_run_id)
            if owner is None:
                return
            lease = owner.child_composition_lease
            if lease.physical_teardown_state == "reconciliation_required":
                owner.settlement_state = "reconciliation_required"
                return
            if owner.parent_graph_slot.state != "terminal_settlement_full":
                owner.settlement_state = "parent_graph_pending"
                return
            if owner.activation_operation_state != "exited":
                owner.settlement_state = "composition_closing"
                return
            composition = lease.composition
            if composition is not None and composition.registry.owner_count != 0:
                owner.settlement_state = "composition_closing"
                return
            if (
                lease.child_session is not None
                and lease.physical_teardown_state != "closed"
            ):
                owner.settlement_state = "composition_closing"
                return
            lease.state = "closing"
            owner.settlement_state = "capacity_releasing"
            prior_slot = owner.capacity_slot
            if isinstance(prior_slot, LiveChildCapacityReservationSlot):
                reservation = prior_slot.reservation
                if not isinstance(reservation, ChildCapacityReservation):
                    raise RuntimeError("live child capacity slot lost concrete owner")
                reservation.released_run_ids.add(subagent_run_id)
                if (
                    reservation.active_slot_count == 0
                    and reservation.uncommitted_count == 0
                ):
                    reservation.released = True
                    self._reservations.pop(reservation.reservation_id, None)
            prior_fingerprint = _capacity_slot_fingerprint(prior_slot)
            owner.capacity_slot = ReleasedChildCapacitySlot(
                slot_kind="released",
                release_receipt_id=f"child_capacity_release:{uuid4().hex}",
                released_from_fingerprint=prior_fingerprint,
            )
            self._owners.pop(subagent_run_id, None)
            for identity in owner.mcp_binding_identities:
                run_ids = self._child_ids_by_mcp_binding_identity.get(identity)
                if run_ids is None:
                    continue
                run_ids.discard(subagent_run_id)
                if not run_ids:
                    self._child_ids_by_mcp_binding_identity.pop(identity, None)
            child_session = lease.child_session
            lease.child_session = None
            lease.composition = None
            lease.state = "released"
            owner.settlement_state = "released"
        if child_session is not None:
            child_session.close()


def _parent_graph_slot(
    *,
    parent_runtime_session_id: str,
    parent_run_id: str,
    subagent_run_id: str,
    spawn_edge_id: str,
    source_horizon: LedgerHorizonFact,
    graph_state_fingerprint: str,
) -> ParentSubagentGraphSlot:
    payload = {
        "parent_runtime_session_id": parent_runtime_session_id,
        "parent_run_id": parent_run_id,
        "subagent_run_id": subagent_run_id,
        "spawn_edge_id": spawn_edge_id,
        "generation": 1,
        "source_horizon_fingerprint": source_horizon.horizon_fingerprint,
        "graph_state_fingerprint": graph_state_fingerprint,
    }
    return ParentSubagentGraphSlot(
        parent_runtime_session_id=parent_runtime_session_id,
        parent_run_id=parent_run_id,
        subagent_run_id=subagent_run_id,
        spawn_edge_id=spawn_edge_id,
        generation=1,
        state="active",
        source_horizon=source_horizon,
        slot_fingerprint=context_fingerprint("parent-subagent-graph-slot:v1", payload),
    )


def _capacity_slot_fingerprint(slot: ChildCapacitySlot) -> str:
    if isinstance(slot, LiveChildCapacityReservationSlot):
        payload = {
            "slot_kind": slot.slot_kind,
            "reservation_id": slot.reservation.reservation_id,
            "reservation_generation": slot.reservation_generation,
        }
    elif isinstance(slot, RecoveredChildCapacityOccupancySlot):
        payload = {
            "slot_kind": slot.slot_kind,
            "occupancy_id": slot.occupancy_id,
            "proof_fingerprint": slot.proof.proof_fingerprint,
        }
    else:
        payload = {
            "slot_kind": slot.slot_kind,
            "release_receipt_id": slot.release_receipt_id,
            "released_from_fingerprint": slot.released_from_fingerprint,
        }
    return context_fingerprint("child-capacity-slot:v1", payload)


def _cancel_task_on_owner_loop(task: asyncio.Task[None]) -> None:
    if task.done():
        return
    owner_loop = task.get_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is owner_loop:
        task.cancel()
        return
    if owner_loop.is_closed():
        return
    owner_loop.call_soon_threadsafe(_cancel_task_if_pending, task)


def _cancel_task_if_pending(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()


async def _wait_for_tasks(
    tasks: dict[str, asyncio.Task[None]],
    *,
    timeout_seconds: float | None,
    timeout_message: str,
) -> None:
    if not tasks:
        return
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    timed_out: list[str] = []
    for run_id, task in tasks.items():
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        if not await _wait_for_task_completion(task, timeout_seconds=remaining):
            timed_out.append(run_id)
    if timed_out:
        raise TimeoutError(timeout_message + ": " + ", ".join(sorted(timed_out)))


async def _wait_for_task_completion(
    task: asyncio.Task[None],
    *,
    timeout_seconds: float | None,
) -> bool:
    if task.done():
        return True
    current_loop = asyncio.get_running_loop()
    owner_loop = task.get_loop()
    if owner_loop is current_loop:
        _, pending = await asyncio.wait({task}, timeout=timeout_seconds)
        return not pending
    if owner_loop.is_closed():
        return task.done()

    completed = asyncio.Event()

    def signal_completion(_task: asyncio.Task[None]) -> None:
        current_loop.call_soon_threadsafe(completed.set)

    def register_callback() -> None:
        if task.done():
            signal_completion(task)
        else:
            task.add_done_callback(signal_completion)

    owner_loop.call_soon_threadsafe(register_callback)
    try:
        if timeout_seconds is None:
            await completed.wait()
        else:
            await asyncio.wait_for(completed.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return task.done()
    return True


__all__ = [
    "ChildActivationOperation",
    "ChildActivationOperationRegistry",
    "ChildAdmissionDiagnostic",
    "ChildAdmissionSessionOwner",
    "ChildAdmissionSessionRegistry",
    "ChildCapacityReservation",
    "ChildRuntimeCompositionLease",
    "ParentSubagentGraphSlot",
]
