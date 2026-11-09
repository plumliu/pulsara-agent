"""MCP physical slots, lease authority, dispatch permits, and cancellation lanes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING, Callable

from pulsara_agent.identity import random_id, require_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint

from .contracts import McpPhysicalConcurrencyKind

if TYPE_CHECKING:
    from .sdk_facade import BoundedMcpSdkClient


class McpDispatchFenceState(StrEnum):
    OPEN = "OPEN"
    DIRTY_FENCED = "DIRTY_FENCED"
    CLOSED = "CLOSED"


class McpDispatchPermitState(StrEnum):
    ADMITTED = "ADMITTED"
    ATTEMPT_ACCEPTED = "ATTEMPT_ACCEPTED"
    LANE_ACQUIRED = "LANE_ACQUIRED"
    RELEASED = "RELEASED"


class McpSnapshotStale(RuntimeError):
    pass


class _McpDispatchCancelledBeforeStart(RuntimeError):
    pass


async def _acquire_dispatch_lane(lane: asyncio.Semaphore, cell_cancel_event: asyncio.Event | None) -> None:
    if cell_cancel_event is None:
        await lane.acquire()
        return
    if cell_cancel_event.is_set():
        raise _McpDispatchCancelledBeforeStart
    acquire = asyncio.create_task(lane.acquire())
    cell_cancelled = asyncio.create_task(cell_cancel_event.wait())
    transferred = False
    try:
        await asyncio.wait((acquire, cell_cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cell_cancel_event.is_set():
            raise _McpDispatchCancelledBeforeStart
        await acquire
        transferred = True
    finally:
        cell_cancelled.cancel()
        if not transferred:
            acquire.cancel()
        settled = await asyncio.gather(acquire, cell_cancelled, return_exceptions=True)
        # Cancellation can race a successful semaphore grant. Return exactly
        # that grant unless ownership transferred to the physical operation.
        if not transferred and settled[0] is True:
            lane.release()


class McpSlotLease:
    """Opaque, non-copyable capability issued by exactly one slot owner."""

    __slots__ = (
        "slot_id",
        "connection_generation",
        "admitted_discovery_generation",
        "admitted_execution_binding_fingerprint",
        "lease_identity",
        "_slot",
        "_authority",
        "_released",
    )

    def __init__(
        self,
        *,
        slot: "McpConnectionSlot",
        authority: object,
        admitted_discovery_generation: int,
    ) -> None:
        if authority is not slot._lease_authority:  # noqa: SLF001
            raise RuntimeError("MCP slot lease construction is sealed")
        self.slot_id = slot.slot_id
        self.connection_generation = slot.connection_generation
        self.admitted_discovery_generation = admitted_discovery_generation
        self.admitted_execution_binding_fingerprint = (
            slot.execution_binding_fingerprint
        )
        self.lease_identity = context_fingerprint(
            "mcp-slot-lease-identity:v1",
            {
                "slot_id": self.slot_id,
                "connection_generation": self.connection_generation,
                "admitted_discovery_generation": self.admitted_discovery_generation,
                "execution_binding_fingerprint": (
                    self.admitted_execution_binding_fingerprint
                ),
            },
        )
        self._slot = slot
        self._authority = authority
        self._released = False

    def exactly_joins(self, slot: "McpConnectionSlot") -> bool:
        return (
            not self._released
            and self._slot is slot
            and self._authority is slot._lease_authority  # noqa: SLF001
            and self.slot_id == slot.slot_id
            and self.connection_generation == slot.connection_generation
            and self.admitted_execution_binding_fingerprint
            == slot.execution_binding_fingerprint
        )

    def release(self) -> None:
        if self._released:
            return
        self._slot.release_lease(self)
        self._released = True

    def __copy__(self) -> None:
        raise TypeError("MCP slot leases cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("MCP slot leases cannot be copied")


@dataclass(slots=True)
class McpDispatchAdmissionPermit:
    permit_id: str
    session_id: str
    scope_kind: ModelInputScopeKind
    turn_id: str
    tool_call_id: str
    descriptor_fingerprint: str
    policy_fingerprint: str
    runtime_generation_id: int
    admitted_dirty_generation: int
    lease: McpSlotLease = field(repr=False)
    _slot: "McpConnectionSlot" = field(repr=False)
    _authority: object = field(repr=False)
    state: McpDispatchPermitState = McpDispatchPermitState.ADMITTED

    def mark_attempt_accepted(self) -> None:
        self._slot.mark_attempt_accepted(self)

    async def acquire_operation(self, *, cell_cancel_event: asyncio.Event | None = None) -> "McpPhysicalOperationPermit":
        return await self._slot.acquire_operation(self, cell_cancel_event=cell_cancel_event)

    def release(self) -> None:
        if self.state is McpDispatchPermitState.RELEASED:
            return
        self._slot.release_dispatch(self)


class McpPhysicalOperationPermit:
    def __init__(
        self,
        permit: McpDispatchAdmissionPermit,
        semaphore: asyncio.Semaphore,
        host_semaphore: asyncio.Semaphore,
    ) -> None:
        self.permit = permit
        self._semaphore = semaphore
        self._host_semaphore = host_semaphore
        self._released = False

    async def __aenter__(self) -> "McpPhysicalOperationPermit":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._host_semaphore.release()
        self._semaphore.release()
        self.permit._slot.release_operation(self.permit)  # noqa: SLF001


class McpConnectionSlot:
    def __init__(
        self,
        *,
        server_id: str,
        supervisor_epoch: str,
        connection_generation: int,
        runtime_config_fingerprint: str,
        client: BoundedMcpSdkClient,
        concurrency_kind: McpPhysicalConcurrencyKind,
        maximum_in_flight: int,
        host_lane: asyncio.Semaphore,
        failure_reporter: Callable[["McpConnectionSlot", str, bool], None],
        physical_lifetime_anchor: object | None = None,
    ) -> None:
        self.slot_id = random_id("slot")
        self.server_id = server_id
        self.supervisor_epoch = require_id(supervisor_epoch, "epoch")
        self.connection_generation = connection_generation
        self.runtime_config_fingerprint = runtime_config_fingerprint
        self.client = client
        self.concurrency_kind = concurrency_kind
        self.execution_binding_fingerprint = context_fingerprint(
            "mcp-slot-execution-binding:v1",
            {
                "slot_id": self.slot_id,
                "server_id": server_id,
                "supervisor_epoch": supervisor_epoch,
                "connection_generation": connection_generation,
                "runtime_config_fingerprint": runtime_config_fingerprint,
                "concurrency_kind": concurrency_kind.value,
            },
        )
        self._lock = RLock()
        self._lease_authority = object()
        self._permit_authority = object()
        self._lane = asyncio.Semaphore(maximum_in_flight)
        self._host_lane = host_lane
        self._failure_reporter = failure_reporter
        self._physical_lifetime_anchor = physical_lifetime_anchor
        self._dirty_generation = 0
        self._dispatch_fence = McpDispatchFenceState.OPEN
        self._accepting_new_leases = True
        self._leases: set[McpSlotLease] = set()
        self._permits: dict[str, McpDispatchAdmissionPermit] = {}
        self._active_operation_count = 0

    def close_physical_lifetime(self) -> None:
        anchor = self._physical_lifetime_anchor
        if anchor is None:
            return
        self._physical_lifetime_anchor = None
        _close_physical_lifetime_anchor(anchor)

    @property
    def dispatch_fence_state(self) -> McpDispatchFenceState:
        with self._lock:
            return self._dispatch_fence

    def report_transport_failure(
        self, category: str, *, retryable: bool
    ) -> None:
        # The exact physical slot is fenced even when a newer candidate has
        # already become the supervisor's reconnect target.  Global status is
        # updated only if this slot is still current.
        self.mark_dirty()
        self._failure_reporter(self, category, retryable)

    def configure_physical_concurrency(
        self,
        *,
        concurrency_kind: McpPhysicalConcurrencyKind,
        maximum_in_flight: int,
    ) -> None:
        """Freeze the negotiated lane before discovery leases are issued."""

        with self._lock:
            if self._leases or self._permits or self._active_operation_count:
                raise RuntimeError("MCP physical lane is already in use")
            if maximum_in_flight < 1:
                raise ValueError("MCP physical lane bound must be positive")
            self.concurrency_kind = concurrency_kind
            self._lane = asyncio.Semaphore(maximum_in_flight)
            self.execution_binding_fingerprint = context_fingerprint(
                "mcp-slot-execution-binding:v1",
                {
                    "slot_id": self.slot_id,
                    "server_id": self.server_id,
                    "supervisor_epoch": self.supervisor_epoch,
                    "connection_generation": self.connection_generation,
                    "runtime_config_fingerprint": self.runtime_config_fingerprint,
                    "concurrency_kind": concurrency_kind.value,
                },
            )

    @property
    def dirty_generation(self) -> int:
        with self._lock:
            return self._dirty_generation

    @property
    def active_slot_lease_count(self) -> int:
        with self._lock:
            return len(self._leases)

    @property
    def active_operation_count(self) -> int:
        with self._lock:
            return self._active_operation_count

    @property
    def active_dispatch_count(self) -> int:
        with self._lock:
            return len(self._permits)

    def issue_lease(self, discovery_generation: int) -> McpSlotLease:
        with self._lock:
            if not self._accepting_new_leases or self._dispatch_fence is McpDispatchFenceState.CLOSED:
                raise RuntimeError("MCP slot refuses new leases")
            lease = McpSlotLease(
                slot=self,
                authority=self._lease_authority,
                admitted_discovery_generation=discovery_generation,
            )
            self._leases.add(lease)
            return lease

    def prepare_refresh_lease(self, discovery_generation: int) -> McpSlotLease:
        """Prepare a candidate while the current slot remains dirty-fenced."""
        with self._lock:
            if (self._dispatch_fence is not McpDispatchFenceState.DIRTY_FENCED
                    or discovery_generation != self._dirty_generation):
                raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
            lease = McpSlotLease(slot=self, authority=self._lease_authority,
                                 admitted_discovery_generation=discovery_generation)
            self._leases.add(lease)
            return lease

    def complete_refresh(self, lease: McpSlotLease) -> None:
        """Reopen only the observed generation; old leases remain stale."""
        with self._lock:
            if (not lease.exactly_joins(self) or lease not in self._leases
                    or lease.admitted_discovery_generation != self._dirty_generation
                    or self._dispatch_fence is not McpDispatchFenceState.DIRTY_FENCED):
                raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
            self._dispatch_fence = McpDispatchFenceState.OPEN
            self._accepting_new_leases = True

    @asynccontextmanager
    async def catalog_operation(self, *, discovery_reservation: asyncio.Semaphore):
        # SDK listings mutate its output-schema cache. Drain every pre-fence
        # admission before listing, including bounded HTTP calls, then own the
        # existing lane. Do not duplicate SDK caches or result validators.
        while self.active_dispatch_count or self.active_operation_count:
            await asyncio.sleep(0.01)
        # Waiting for discovery memory is also queueing, not physical I/O.
        # Semaphore contexts unwind every partial acquisition in reverse order.
        async with self._lane, discovery_reservation, self._host_lane:
            with self._lock:
                if self._dispatch_fence is McpDispatchFenceState.CLOSED:
                    raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
                self._active_operation_count += 1
            try:
                yield
            finally:
                with self._lock:
                    self._active_operation_count -= 1

    def release_lease(self, lease: McpSlotLease) -> None:
        with self._lock:
            if lease._slot is not self or lease._authority is not self._lease_authority:  # noqa: SLF001
                raise RuntimeError("MCP slot lease authority conflicts")
            self._leases.discard(lease)

    @asynccontextmanager
    async def user_prompt_operation(self, lease: McpSlotLease):
        """User template reads share physical lanes without inventing a tool call."""
        async with self._lane, self._host_lane:
            with self._lock:
                if not self.lease_is_current(lease):
                    raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
                self._active_operation_count += 1
            try:
                yield
            finally:
                with self._lock:
                    self._active_operation_count -= 1

    def mark_dirty(self) -> int:
        with self._lock:
            if self._dispatch_fence is McpDispatchFenceState.CLOSED:
                return self._dirty_generation
            self._dirty_generation += 1
            self._dispatch_fence = McpDispatchFenceState.DIRTY_FENCED
            self._accepting_new_leases = False
            return self._dirty_generation

    def begin_retire(self) -> None:
        """Stop new leases while existing runtime generations drain normally."""

        with self._lock:
            self._accepting_new_leases = False

    def lease_is_current(self, lease: McpSlotLease) -> bool:
        with self._lock:
            return (
                lease.exactly_joins(self)
                and self._dispatch_fence is McpDispatchFenceState.OPEN
                and lease.admitted_discovery_generation == self._dirty_generation
            )

    def admit_dispatch(
        self,
        *,
        lease: McpSlotLease,
        session_id: str,
        scope_kind: ModelInputScopeKind,
        turn_id: str,
        tool_call_id: str,
        descriptor_fingerprint: str,
        policy_fingerprint: str,
        runtime_generation_id: int,
    ) -> McpDispatchAdmissionPermit:
        with self._lock:
            if (
                not lease.exactly_joins(self)
                or self._dispatch_fence is not McpDispatchFenceState.OPEN
                or lease.admitted_discovery_generation != self._dirty_generation
            ):
                raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
            permit_id = random_id("permit")
            while permit_id in self._permits:
                permit_id = random_id("permit")
            permit = McpDispatchAdmissionPermit(
                permit_id=permit_id,
                session_id=session_id,
                scope_kind=scope_kind,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                descriptor_fingerprint=descriptor_fingerprint,
                policy_fingerprint=policy_fingerprint,
                runtime_generation_id=runtime_generation_id,
                admitted_dirty_generation=self._dirty_generation,
                lease=lease,
                _slot=self,
                _authority=self._permit_authority,
            )
            self._permits[permit.permit_id] = permit
            return permit

    def _require_permit(self, permit: McpDispatchAdmissionPermit) -> None:
        if (
            permit._slot is not self
            or permit._authority is not self._permit_authority
            or self._permits.get(permit.permit_id) is not permit
            or permit.lease._slot is not self  # noqa: SLF001
        ):
            raise RuntimeError("MCP dispatch permit authority conflicts")

    def mark_attempt_accepted(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.ADMITTED:
                raise RuntimeError("MCP dispatch permit is not admissible")
            permit.state = McpDispatchPermitState.ATTEMPT_ACCEPTED

    async def acquire_operation(
        self, permit: McpDispatchAdmissionPermit, *, cell_cancel_event: asyncio.Event | None = None
    ) -> McpPhysicalOperationPermit:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.ATTEMPT_ACCEPTED:
                raise RuntimeError("MCP attempt is not accepted")
        # A request waiting on its server must not reserve capacity that an
        # unrelated ready server can use for actual physical I/O.
        await _acquire_dispatch_lane(self._lane, cell_cancel_event)
        try:
            await _acquire_dispatch_lane(self._host_lane, cell_cancel_event)
        except BaseException:
            self._lane.release()
            raise
        with self._lock:
            try:
                self._require_permit(permit)
                if cell_cancel_event is not None and cell_cancel_event.is_set():
                    raise _McpDispatchCancelledBeforeStart
                if permit.state is not McpDispatchPermitState.ATTEMPT_ACCEPTED:
                    raise RuntimeError("MCP operation permit raced settlement")
                permit.state = McpDispatchPermitState.LANE_ACQUIRED
                self._active_operation_count += 1
            except BaseException:
                self._host_lane.release()
                self._lane.release()
                raise
        return McpPhysicalOperationPermit(permit, self._lane, self._host_lane)

    def release_dispatch(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is McpDispatchPermitState.LANE_ACQUIRED:
                raise RuntimeError("MCP lane permit must settle the operation")
            permit.state = McpDispatchPermitState.RELEASED
            self._permits.pop(permit.permit_id, None)

    def release_operation(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.LANE_ACQUIRED:
                raise RuntimeError("MCP operation permit is not active")
            self._active_operation_count -= 1
            permit.state = McpDispatchPermitState.RELEASED
            self._permits.pop(permit.permit_id, None)

    def begin_close(self) -> None:
        with self._lock:
            self._accepting_new_leases = False
            self._dispatch_fence = McpDispatchFenceState.CLOSED
            for permit in tuple(self._permits.values()):
                if permit.state is McpDispatchPermitState.ADMITTED:
                    permit.state = McpDispatchPermitState.RELEASED
                    self._permits.pop(permit.permit_id, None)


def _close_physical_lifetime_anchor(anchor: object) -> None:
    close = getattr(anchor, "close", None)
    if not callable(close):
        raise TypeError("MCP physical lifetime anchor is not closable")
    close()
