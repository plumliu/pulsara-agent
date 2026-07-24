"""Session-owned asynchronous MCP discovery and execution supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from pulsara_agent.primitives.mcp import (
    McpDiagnosticFact,
    McpServerLifecycleTimingFact,
)
from pulsara_agent.runtime.mcp.sdk import (
    SdkMcpConnectCancelled,
    SdkMcpConnectError,
    SdkMcpClientManager,
    SdkMcpConnection,
    discover_mcp_server,
)
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpCandidateBatch,
    McpDrainError,
    McpManagerLease,
    McpManagerSlot,
    McpPendingLeaseOwner,
    McpPendingLeaseReservation,
    McpReconcileTicket,
    McpRequiredStartupError,
    McpRequiredStartupResult,
    McpRuntimeConfigIdentity,
    McpServerAttempt,
    McpServerCandidate,
    McpServerConfig,
    McpServerRuntimeSpec,
    McpServerSnapshot,
    McpServerStatus,
    MAX_MCP_SERVERS_PER_SESSION,
    event_safe_mcp_config_fingerprint,
    mcp_config_set_fingerprint,
    new_mcp_slot,
    redact_mcp_error_message,
    runtime_mcp_config_fingerprint,
    snapshot_semantic_fingerprint,
)


@dataclass(frozen=True, slots=True)
class _AttemptRuntime:
    ticket_id: str
    trigger: str
    attempt: McpServerAttempt
    spec: McpServerRuntimeSpec
    queued_at_utc: str
    queued_monotonic: float


@dataclass(slots=True)
class _CloseAttempt:
    attempt_id: str
    completion: asyncio.Future[None]


@dataclass(slots=True)
class McpServerSupervisor:
    """The only owner of MCP workers, manager slots, leases, and close."""

    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    _epoch: int = 0
    _runtime_identity: McpRuntimeConfigIdentity | None = None
    _event_safe_config_set_fingerprint: str = "sha256:empty"
    _desired_specs: dict[str, McpServerRuntimeSpec] = field(default_factory=dict)
    _current_attempts: dict[str, _AttemptRuntime] = field(default_factory=dict)
    _generation_by_server: dict[str, int] = field(default_factory=dict)
    _workers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _discovery_worker_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _retry_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _retry_timer_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _owned_background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _candidate_cleanup_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _candidate_cleanup_managers: dict[int, Any] = field(default_factory=dict)
    _retiring_slot_cleanup_tasks: set[asyncio.Task[None]] = field(
        default_factory=set
    )
    _orphan_connections: dict[int, SdkMcpConnection] = field(default_factory=dict)
    _candidates: list[McpServerCandidate] = field(default_factory=list)
    _slots: dict[str, McpManagerSlot] = field(default_factory=dict)
    _installed_slot_by_server: dict[str, str] = field(default_factory=dict)
    _leases: dict[str, McpManagerLease] = field(default_factory=dict)
    _pending_leases: dict[str, McpPendingLeaseOwner] = field(default_factory=dict)
    _retry_attempts: dict[str, int] = field(default_factory=dict)
    _next_retry_monotonic: dict[str, float] = field(default_factory=dict)
    _refresh_due_monotonic: dict[str, float] = field(default_factory=dict)
    _stale_discard_count: dict[str, int] = field(default_factory=dict)
    _lifecycle: str = "open"
    _state_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _close_attempt: _CloseAttempt | None = field(default=None, init=False, repr=False)

    @property
    def config_epoch(self) -> int:
        return self._epoch

    @property
    def event_safe_config_set_fingerprint(self) -> str:
        return self._event_safe_config_set_fingerprint

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    @property
    def pending_completion_count(self) -> int:
        return len(self._pending_leases)

    def prepare(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        trigger: str,
    ) -> McpReconcileTicket:
        if trigger not in {"initial", "config_change", "ttl_refresh", "retry", "manual_refresh"}:
            raise ValueError(f"invalid MCP reconcile trigger: {trigger}")
        with self._state_lock:
            if self._lifecycle != "open":
                raise RuntimeError("MCP supervisor is closing")
            ordered = tuple(sorted(configs, key=lambda item: item.server_id))
            if len(ordered) > MAX_MCP_SERVERS_PER_SESSION:
                raise ValueError(
                    f"MCP server count exceeds bounded cap: {MAX_MCP_SERVERS_PER_SESSION}"
                )
            server_ids = tuple(config.server_id for config in ordered)
            if len(set(server_ids)) != len(server_ids):
                raise ValueError("MCP server ids must be unique")
            runtime_set = mcp_config_set_fingerprint(ordered, event_safe=False)
            event_safe_set = mcp_config_set_fingerprint(ordered, event_safe=True)
            previous_runtime_set = (
                self._runtime_identity.runtime_config_set_fingerprint
                if self._runtime_identity is not None
                else None
            )
            changed_set = previous_runtime_set != runtime_set
            if changed_set:
                self._epoch += 1
            runtime_by_server = {
                config.server_id: runtime_mcp_config_fingerprint(config)
                for config in ordered
            }
            self._runtime_identity = McpRuntimeConfigIdentity(
                config_epoch=self._epoch,
                runtime_config_set_fingerprint=runtime_set,
                runtime_server_config_fingerprints=MappingProxyType(runtime_by_server),
            )
            self._event_safe_config_set_fingerprint = event_safe_set
            specs = {
                config.server_id: McpServerRuntimeSpec(
                    config=config,
                    runtime_config_fingerprint=runtime_by_server[config.server_id],
                    event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
                )
                for config in ordered
            }
            previous_specs = dict(self._desired_specs)
            removed = set(previous_specs).difference(specs)
            if changed_set:
                for server_id in previous_specs:
                    self._reset_retry_state(server_id)
            for server_id in set(previous_specs) | set(specs):
                previous = previous_specs.get(server_id)
                current = specs.get(server_id)
                if previous is None:
                    continue
                if (
                    current is None
                    or previous.runtime_config_fingerprint
                    != current.runtime_config_fingerprint
                ):
                    self._refresh_due_monotonic.pop(server_id, None)
            self._desired_specs = specs
            ticket_id = f"mcp_ticket:{uuid4().hex}"
            attempts: dict[str, McpServerAttempt] = {}
            now = time.monotonic()
            for server_id in removed:
                old_worker = self._workers.pop(server_id, None)
                if old_worker is not None and not old_worker.done():
                    old_worker.cancel()
                self._reserve_disabled_candidate(
                    server_id=server_id,
                    ticket_id=ticket_id,
                    trigger="config_change",
                    required=False,
                    now=now,
                    spec=previous_specs[server_id],
                )
            for server_id, spec in specs.items():
                config = spec.config
                if not config.enabled:
                    old_worker = self._workers.pop(server_id, None)
                    if old_worker is not None and not old_worker.done():
                        old_worker.cancel()
                    self._reserve_disabled_candidate(
                        server_id=server_id,
                        ticket_id=ticket_id,
                        trigger=trigger,
                        required=config.required,
                        now=now,
                        spec=spec,
                    )
                    continue
                installed_slot = self._installed_slot_for_server(server_id)
                same_runtime = (
                    installed_slot is not None
                    and installed_slot.runtime_config_fingerprint == spec.runtime_config_fingerprint
                )
                current_runtime = self._current_attempts.get(server_id)
                current_attempt_is_same_runtime = (
                    current_runtime is not None
                    and current_runtime.attempt.config_epoch == self._epoch
                    and current_runtime.spec.runtime_config_fingerprint
                    == spec.runtime_config_fingerprint
                )
                current_worker = self._workers.get(server_id)
                current_candidate = (
                    self._latest_candidate(
                        server_id,
                        current_runtime.attempt.reconcile_attempt_id,
                    )
                    if current_runtime is not None
                    else None
                )
                due = now >= self._refresh_due_monotonic.get(server_id, 0.0)
                retry_deadline = self._next_retry_monotonic.get(server_id)
                retry_due = (
                    retry_deadline is not None and now >= retry_deadline
                )
                force = trigger in {"manual_refresh", "ttl_refresh", "retry"}
                if same_runtime and not force and not due:
                    continue
                if (
                    installed_slot is None
                    and current_attempt_is_same_runtime
                    and not force
                ):
                    if current_worker is not None and not current_worker.done():
                        if config.required:
                            assert current_runtime is not None
                            attempts[server_id] = current_runtime.attempt
                        continue
                    if (
                        current_candidate is not None
                        and current_candidate.server_snapshot.status
                        is McpServerStatus.READY
                    ):
                        continue
                    if retry_deadline is not None:
                        continue
                if trigger == "retry" and not retry_due:
                    continue
                retry_timer = self._retry_tasks.get(server_id)
                current_task = asyncio.current_task()
                if retry_timer is not None and retry_timer is not current_task:
                    retry_timer.cancel()
                    self._retry_tasks.pop(server_id, None)
                attempt = self._reserve_attempt(spec, now=now)
                attempts[server_id] = attempt
                runtime = _AttemptRuntime(
                    ticket_id=ticket_id,
                    trigger=trigger,
                    attempt=attempt,
                    spec=spec,
                    queued_at_utc=_utc_now(),
                    queued_monotonic=now,
                )
                self._current_attempts[server_id] = runtime
                self._discard_queued_candidates_for_server(server_id)
                old_worker = self._workers.get(server_id)
                if old_worker is not None and not old_worker.done():
                    old_worker.cancel()
                worker = asyncio.create_task(
                    self._run_attempt(runtime),
                    name=f"pulsara-mcp-discovery:{server_id}:{attempt.reconcile_attempt_id}",
                )
                self._workers[server_id] = worker
                self._discovery_worker_tasks.add(worker)
                worker.add_done_callback(self._discovery_worker_tasks.discard)
                self._own_background_task(worker)
            required_ids = tuple(
                config.server_id for config in ordered if config.enabled and config.required
            )
            optional_ids = tuple(
                config.server_id for config in ordered if config.enabled and not config.required
            )
            required_deadline = max(
                (
                    attempts[server_id].deadline_monotonic
                    for server_id in required_ids
                    if server_id in attempts
                ),
                default=None,
            )
            return McpReconcileTicket(
                ticket_id=ticket_id,
                config_epoch=self._epoch,
                event_safe_config_set_fingerprint=event_safe_set,
                trigger=trigger,  # type: ignore[arg-type]
                required_server_ids=required_ids,
                optional_server_ids=optional_ids,
                server_attempts=MappingProxyType(attempts),
                required_wait_deadline_monotonic=required_deadline,
            )

    async def await_required(self, ticket: McpReconcileTicket) -> McpRequiredStartupResult:
        failures: list[str] = []
        diagnostics: list[McpDiagnosticFact] = []
        for server_id in ticket.required_server_ids:
            attempt = ticket.server_attempts.get(server_id)
            if attempt is None:
                slot = self._installed_slot_for_server(server_id)
                desired = self._desired_specs.get(server_id)
                slot_ready = (
                    slot is not None
                    and desired is not None
                    and slot.lifecycle == "installed"
                    and slot.runtime_config_fingerprint
                    == desired.runtime_config_fingerprint
                )
                current = self._current_attempts.get(server_id)
                candidate = (
                    self._latest_candidate(
                        server_id,
                        current.attempt.reconcile_attempt_id,
                    )
                    if current is not None
                    and current.attempt.config_epoch == ticket.config_epoch
                    and desired is not None
                    and current.spec.runtime_config_fingerprint
                    == desired.runtime_config_fingerprint
                    else None
                )
                if not slot_ready and (
                    candidate is None
                    or candidate.server_snapshot.status is not McpServerStatus.READY
                ):
                    failures.append(server_id)
                continue
            runtime = self._current_attempts.get(server_id)
            if runtime is None or runtime.attempt.reconcile_attempt_id != attempt.reconcile_attempt_id:
                failures.append(server_id)
                continue
            task = self._workers.get(server_id)
            if task is None:
                failures.append(server_id)
                continue
            remaining = attempt.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                failures.append(server_id)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                failures.append(server_id)
            candidate = self._latest_candidate(server_id, attempt.reconcile_attempt_id)
            if candidate is None or candidate.server_snapshot.status is not McpServerStatus.READY:
                failures.append(server_id)
                diagnostics.append(
                    McpDiagnosticFact(
                        severity="error",
                        code="mcp_required_generation_unavailable",
                        message=(
                            f"Required MCP server {server_id!r} did not produce a "
                            "READY candidate before its absolute deadline."
                        ),
                        metadata={
                            "server_id": server_id,
                            "status": (
                                candidate.server_snapshot.status.value
                                if candidate is not None
                                else "missing"
                            ),
                        },
                    )
                )
        if failures:
            unique = tuple(sorted(set(failures)))
            raise McpRequiredStartupError(
                server_ids=unique,
                reason_code="mcp_required_generation_unavailable",
                diagnostics=tuple(diagnostics[:16]),
            )
        return McpRequiredStartupResult(ready_server_ids=ticket.required_server_ids)

    async def await_ticket_snapshots(
        self,
        ticket: McpReconcileTicket,
    ) -> tuple[McpServerSnapshot, ...]:
        """Direct-CLI facade: await this ticket without installing its surface."""

        for server_id, attempt in ticket.server_attempts.items():
            task = self._workers.get(server_id)
            if task is None:
                continue
            remaining = attempt.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        snapshots: list[McpServerSnapshot] = []
        for server_id in sorted(
            set(ticket.server_attempts)
            | set(ticket.required_server_ids)
            | set(ticket.optional_server_ids)
        ):
            attempt = ticket.server_attempts.get(server_id)
            if attempt is None:
                slot = self._installed_slot_for_server(server_id)
                if slot is not None:
                    snapshots.extend(slot.manager.snapshots)
                continue
            candidate = self._latest_candidate(
                server_id,
                attempt.reconcile_attempt_id,
            )
            if candidate is not None:
                snapshots.append(candidate.server_snapshot)
        return tuple(snapshots)

    def terminalize_attempt_for_pending_reconfiguration(
        self,
        ticket: McpReconcileTicket,
        error: Exception,
        *,
        server_id: str,
    ) -> None:
        """Create a fail-closed candidate when a related pending binding changed.

        An attempt may still be discovering, or a required attempt may reach its
        deadline just before its worker queues a failure candidate.  A suspended
        call whose own server was reconfigured must not resume through the old
        slot during that race.  The supervisor
        cancels the exact desired attempt and materializes one bounded FAILED
        candidate at the same generation; a late worker completion is therefore
        cancellation-only and cannot overwrite the terminal projection.
        """

        with self._state_lock:
            attempt = ticket.server_attempts.get(server_id)
            current = self._current_attempts.get(server_id)
            if (
                attempt is None
                or current is None
                or current.attempt.reconcile_attempt_id
                != attempt.reconcile_attempt_id
                or current.attempt.reserved_discovery_generation
                != attempt.reserved_discovery_generation
            ):
                return
            if self._latest_candidate(server_id, attempt.reconcile_attempt_id):
                return
            worker = self._workers.get(server_id)
            if worker is not None and not worker.done():
                worker.cancel()
            self._candidates.append(
                self._failed_candidate(
                    current,
                    status=McpServerStatus.FAILED,
                    exc=error,
                )
            )
            self._schedule_retry(server_id)

    def drain_installable_candidates(self, *, expected_epoch: int) -> McpCandidateBatch:
        with self._state_lock:
            if expected_epoch != self._epoch:
                return McpCandidateBatch(config_epoch=self._epoch, candidates=())
            accepted_by_server: dict[str, McpServerCandidate] = {}
            retained: list[McpServerCandidate] = []
            stale: list[McpServerCandidate] = []
            for candidate in self._candidates:
                if candidate.config_epoch != expected_epoch:
                    stale.append(candidate)
                    continue
                current = self._current_attempts.get(candidate.server_snapshot.server_id)
                if (
                    current is not None
                    and current.attempt.reconcile_attempt_id
                    == candidate.reconcile_attempt_id
                    and current.attempt.reserved_discovery_generation
                    == candidate.reserved_discovery_generation
                    and current.spec.runtime_config_fingerprint
                    == candidate.runtime_spec.runtime_config_fingerprint
                ):
                    server_id = candidate.server_snapshot.server_id
                    previous = accepted_by_server.get(server_id)
                    if previous is not None:
                        stale.append(previous)
                    accepted_by_server[server_id] = candidate
                else:
                    stale.append(candidate)
            self._candidates = retained
            for candidate in stale:
                server_id = candidate.server_snapshot.server_id
                self._stale_discard_count[server_id] = (
                    self._stale_discard_count.get(server_id, 0) + 1
                )
                self._schedule_candidate_close(candidate)
            return McpCandidateBatch(
                config_epoch=self._epoch,
                candidates=tuple(
                    accepted_by_server[server_id]
                    for server_id in sorted(accepted_by_server)
                ),
            )

    def reject_candidates(
        self,
        candidates: tuple[McpServerCandidate, ...],
    ) -> None:
        """Retain close ownership for drained candidates that were not installed."""

        for candidate in candidates:
            slot = candidate.manager_slot
            if slot is not None and slot.lifecycle == "candidate":
                self._schedule_candidate_close(candidate)

    def current_starting_snapshots(self) -> tuple[McpServerSnapshot, ...]:
        with self._state_lock:
            snapshots = []
            for server_id, runtime in self._current_attempts.items():
                task = self._workers.get(server_id)
                if task is None or task.done():
                    continue
                snapshots.append(_starting_snapshot(runtime))
            return tuple(snapshots)

    def stale_discard_counts(self) -> dict[str, int]:
        """Snapshot unacknowledged stale-candidate counts for a pending audit."""

        with self._state_lock:
            return dict(self._stale_discard_count)

    def acknowledge_stale_discard_counts(self, counts: dict[str, int]) -> None:
        """Subtract counts after their installation audit enters the pending queue."""

        with self._state_lock:
            for server_id, count in counts.items():
                remaining = max(0, self._stale_discard_count.get(server_id, 0) - count)
                if remaining:
                    self._stale_discard_count[server_id] = remaining
                else:
                    self._stale_discard_count.pop(server_id, None)

    def commit_slot_transition(
        self,
        *,
        candidates: tuple[McpServerCandidate, ...],
        retiring_slot_ids: tuple[str, ...],
    ) -> None:
        """Synchronous slot lifecycle transition used by HostSession's commit block."""

        with self._state_lock:
            if self._lifecycle != "open":
                raise RuntimeError("MCP supervisor is not open")
            for candidate in candidates:
                slot = candidate.manager_slot
                if slot is not None and slot.lifecycle != "candidate":
                    raise RuntimeError("MCP candidate slot is not installable")
            for slot_id in retiring_slot_ids:
                slot = self._slots.get(slot_id)
                if slot is not None and slot.lifecycle == "installed":
                    slot.lifecycle = "retiring"
            for candidate in candidates:
                slot = candidate.manager_slot
                server_id = candidate.server_snapshot.server_id
                if slot is None:
                    self._installed_slot_by_server.pop(server_id, None)
                    continue
                slot.lifecycle = "installed"
                self._slots[slot.slot_id] = slot
                self._installed_slot_by_server[server_id] = slot.slot_id
                self._refresh_due_monotonic[server_id] = (
                    time.monotonic() + candidate.runtime_spec.config.refresh_ttl_ms / 1000
                )

    def restore_retiring_slots(self, slot_ids: tuple[str, ...]) -> None:
        """Rollback a pre-linearization optional installation failure."""

        with self._state_lock:
            if self._lifecycle != "open":
                raise RuntimeError("MCP supervisor is not open")
            for slot_id in slot_ids:
                slot = self._slots.get(slot_id)
                if (
                    slot is not None
                    and slot.lifecycle == "retiring"
                    and self._installed_slot_by_server.get(slot.server_id) == slot_id
                ):
                    slot.lifecycle = "installed"

    def installed_slot(self, server_id: str) -> McpManagerSlot | None:
        with self._state_lock:
            return self._installed_slot_for_server(server_id)

    def binding_matches_current_desired_runtime(
        self,
        identity: McpBindingIdentity,
    ) -> bool:
        """Return whether a frozen binding still matches the desired server config.

        Discovery preparation is deliberately not the slot-retirement linearization
        point.  This query lets HostSession distinguish a related reconfiguration
        from an unrelated required-server failure without mutating the old slot
        before a candidate installation is committed.
        """

        with self._state_lock:
            slot = self._slots.get(identity.slot_id)
            if (
                slot is None
                or slot.binding_identity != identity
                or slot.lifecycle != "installed"
            ):
                return False
            desired = self._desired_specs.get(identity.server_id)
            return (
                desired is not None
                and desired.config.enabled
                and desired.runtime_config_fingerprint
                == slot.runtime_config_fingerprint
            )

    def slots(self) -> tuple[McpManagerSlot, ...]:
        with self._state_lock:
            return tuple(self._slots.values())

    def acquire_binding_lease(self, identity: McpBindingIdentity) -> McpManagerLease:
        with self._state_lock:
            if self._lifecycle != "open":
                raise RuntimeError("MCP supervisor is closing")
            slot = self._slots.get(identity.slot_id)
            if slot is None or slot.binding_identity != identity or slot.lifecycle != "installed":
                raise RuntimeError("mcp_binding_generation_unavailable")
            desired = self._desired_specs.get(identity.server_id)
            if (
                desired is None
                or not desired.config.enabled
                or desired.runtime_config_fingerprint
                != slot.runtime_config_fingerprint
            ):
                raise RuntimeError("mcp_binding_generation_unavailable")
            slot.borrower_count += 1
            lease = McpManagerLease(
                lease_id=f"mcp_lease:{uuid4().hex}",
                slot_id=slot.slot_id,
                binding_identity=identity,
            )
            self._leases[lease.lease_id] = lease
            return lease

    def manager_for_lease(self, lease: McpManagerLease):
        with self._state_lock:
            stored = self._leases.get(lease.lease_id)
            if stored != lease:
                raise RuntimeError("MCP lease is not active")
            slot = self._slots.get(lease.slot_id)
            if slot is None:
                raise RuntimeError("MCP lease slot is unavailable")
            return slot.manager

    def release_lease(self, lease: McpManagerLease) -> None:
        cleanup_slot: McpManagerSlot | None = None
        with self._state_lock:
            if self._leases.pop(lease.lease_id, None) is None:
                return
            slot = self._slots.get(lease.slot_id)
            if slot is not None:
                slot.borrower_count = max(0, slot.borrower_count - 1)
                if (
                    slot.borrower_count == 0
                    and slot.lifecycle == "retiring"
                    and self._lifecycle == "open"
                ):
                    cleanup_slot = slot
        if cleanup_slot is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Component/direct sync callers have no task owner.  The next
                # explicit close_retiring_slots()/Host close remains canonical.
                pass
            else:
                self._schedule_retiring_slot_close(cleanup_slot)

    def promote_lease_to_pending(
        self,
        lease: McpManagerLease,
        interaction_id: str,
    ) -> McpPendingLeaseReservation:
        with self._state_lock:
            if lease.lease_id not in self._leases:
                raise RuntimeError("cannot promote inactive MCP lease")
            if interaction_id in self._pending_leases:
                raise RuntimeError("MCP interaction already owns a lease")
            reservation = McpPendingLeaseReservation(
                reservation_id=f"mcp_pending_lease:{uuid4().hex}",
                interaction_id=interaction_id,
                binding_identity=lease.binding_identity,
            )
            self._pending_leases[interaction_id] = McpPendingLeaseOwner(
                interaction_id=interaction_id,
                lease=lease,
                reservation_id=reservation.reservation_id,
            )
            return reservation

    def confirm_pending_lease(self, interaction_id: str, reservation_id: str) -> None:
        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is None or owner.reservation_id != reservation_id:
                raise RuntimeError("MCP pending lease reservation mismatch")
            owner.confirmed = True

    def abort_pending_lease(self, interaction_id: str, reservation_id: str) -> None:
        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is None or owner.reservation_id != reservation_id:
                return
            self._pending_leases.pop(interaction_id, None)
            lease = owner.lease
        self.release_lease(lease)

    def borrow_pending_lease(
        self,
        interaction_id: str,
        binding_identity: McpBindingIdentity,
    ) -> McpManagerLease:
        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is None or not owner.confirmed or owner.lease.binding_identity != binding_identity:
                raise RuntimeError("MCP pending lease binding mismatch")
            owner.active_borrows += 1
            return owner.lease

    def pending_lease_reservation(
        self,
        interaction_id: str,
    ) -> McpPendingLeaseReservation:
        """Return the immutable identity for one live pending lease owner."""

        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is None:
                raise RuntimeError("MCP pending lease owner is unavailable")
            return McpPendingLeaseReservation(
                reservation_id=owner.reservation_id,
                interaction_id=owner.interaction_id,
                binding_identity=owner.lease.binding_identity,
            )

    def return_pending_borrow(self, interaction_id: str) -> None:
        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is not None:
                owner.active_borrows = max(0, owner.active_borrows - 1)

    def complete_pending_lease(self, interaction_id: str) -> None:
        with self._state_lock:
            owner = self._pending_leases.get(interaction_id)
            if owner is None:
                return
            if owner.active_borrows:
                raise RuntimeError("cannot complete borrowed MCP pending lease")
            self._pending_leases.pop(interaction_id, None)
            lease = owner.lease
        self.release_lease(lease)

    async def close_retiring_slots(
        self,
        *,
        timeout_seconds: float = 5.0,
        wait_for_borrowers: bool = True,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._state_lock:
                cleanup_tasks = tuple(self._retiring_slot_cleanup_tasks)
            if cleanup_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpDrainError(
                        "timed out draining retiring MCP slot cleanup"
                    )
                _done, cleanup_pending = await asyncio.wait(
                    cleanup_tasks,
                    timeout=remaining,
                )
                with self._state_lock:
                    self._retiring_slot_cleanup_tasks.difference_update(_done)
                if cleanup_pending:
                    raise McpDrainError(
                        "timed out draining retiring MCP slot cleanup"
                    )
            with self._state_lock:
                closable = [
                    slot
                    for slot in self._slots.values()
                    if slot.lifecycle == "retiring" and slot.borrower_count == 0
                ]
                pending = any(
                    slot.lifecycle == "retiring" and slot.borrower_count > 0
                    for slot in self._slots.values()
                )
                for slot in closable:
                    slot.lifecycle = "closing"
            for slot in closable:
                try:
                    await slot.manager.aclose(
                        timeout_seconds=max(0.01, deadline - time.monotonic())
                    )
                except BaseException:
                    with self._state_lock:
                        if self._slots.get(slot.slot_id) is slot:
                            slot.lifecycle = "retiring"
                    raise
                with self._state_lock:
                    slot.lifecycle = "closed"
                    self._slots.pop(slot.slot_id, None)
            if not pending or not wait_for_borrowers:
                return
            if time.monotonic() >= deadline:
                raise McpDrainError("timed out waiting for retiring MCP slot leases")
            await asyncio.sleep(0.01)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        with self._state_lock:
            existing = self._close_attempt
            if existing is not None and not existing.completion.done():
                waiter = existing.completion
                owner = False
            else:
                loop = asyncio.get_running_loop()
                attempt = _CloseAttempt(
                    attempt_id=f"mcp_close:{uuid4().hex}",
                    completion=loop.create_future(),
                )
                self._close_attempt = attempt
                waiter = attempt.completion
                owner = True
        if not owner:
            await asyncio.shield(waiter)
            return
        attempt = self._close_attempt
        assert attempt is not None
        try:
            await self._close_owned(timeout_seconds=timeout_seconds)
        except BaseException as exc:
            with self._state_lock:
                self._lifecycle = "open_with_close_pending"
                if not attempt.completion.done():
                    attempt.completion.set_exception(exc)
                    attempt.completion.exception()
                if self._close_attempt is attempt:
                    self._close_attempt = None
            raise
        else:
            with self._state_lock:
                self._lifecycle = "closed"
                if not attempt.completion.done():
                    attempt.completion.set_result(None)
                if self._close_attempt is attempt:
                    self._close_attempt = None

    async def _close_owned(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        with self._state_lock:
            self._lifecycle = "closing"
            workers = tuple(
                task for task in self._discovery_worker_tasks if not task.done()
            )
            retry_timers = tuple(
                task for task in self._retry_timer_tasks if not task.done()
            )
            self._workers.clear()
            self._retry_tasks.clear()
            candidates = tuple(self._candidates)
            for slot in self._slots.values():
                if slot.lifecycle not in {"closed", "closing"}:
                    slot.lifecycle = "retiring"
        for task in (*workers, *retry_timers):
            task.cancel()
        background_attempts = (*workers, *retry_timers)
        if background_attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpDrainError("MCP close deadline expired before worker drain")
            try:
                await asyncio.wait_for(
                    asyncio.gather(*background_attempts, return_exceptions=True),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise McpDrainError("timed out draining MCP workers") from exc
        with self._state_lock:
            orphan_connections = tuple(self._orphan_connections.values())
        for connection in orphan_connections:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpDrainError(
                    "MCP close deadline expired during connection cleanup"
                )
            await connection.aclose(timeout_seconds=remaining)
            with self._state_lock:
                self._orphan_connections.pop(id(connection), None)
        with self._state_lock:
            cleanup_tasks = tuple(
                self._candidate_cleanup_tasks | self._retiring_slot_cleanup_tasks
            )
        if cleanup_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpDrainError(
                    "MCP close deadline expired before stale-candidate cleanup drain"
                )
            _done, pending = await asyncio.wait(
                cleanup_tasks,
                timeout=remaining,
            )
            if pending:
                raise McpDrainError(
                    "timed out draining background MCP manager cleanup"
                )
        with self._state_lock:
            orphan_candidate_managers = tuple(
                self._candidate_cleanup_managers.values()
            )
        for manager in orphan_candidate_managers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpDrainError(
                    "MCP close deadline expired during candidate manager retry"
                )
            await manager.aclose(timeout_seconds=remaining)
            with self._state_lock:
                self._candidate_cleanup_managers.pop(id(manager), None)
        for candidate in candidates:
            if candidate.manager_slot is not None:
                await candidate.manager_slot.manager.aclose(
                    timeout_seconds=max(0.01, deadline - time.monotonic())
                )
            with self._state_lock:
                self._candidates = [
                    current
                    for current in self._candidates
                    if current is not candidate
                ]
        while True:
            with self._state_lock:
                borrowers = sum(slot.borrower_count for slot in self._slots.values())
            if borrowers == 0:
                break
            if time.monotonic() >= deadline:
                raise McpDrainError("timed out draining MCP leases")
            await asyncio.sleep(0.01)
        with self._state_lock:
            slots = tuple(self._slots.values())
        for slot in slots:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpDrainError("MCP close deadline expired during manager close")
            await slot.manager.aclose(timeout_seconds=remaining)
            with self._state_lock:
                slot.lifecycle = "closed"
                self._slots.pop(slot.slot_id, None)
        with self._state_lock:
            self._installed_slot_by_server.clear()
            self._pending_leases.clear()
            self._leases.clear()

    def _own_background_task(self, task: asyncio.Task[None]) -> None:
        self._owned_background_tasks.add(task)
        task.add_done_callback(self._owned_background_tasks.discard)

    def _discard_queued_candidates_for_server(self, server_id: str) -> None:
        stale = [
            candidate
            for candidate in self._candidates
            if candidate.server_snapshot.server_id == server_id
        ]
        if not stale:
            return
        self._candidates = [
            candidate
            for candidate in self._candidates
            if candidate.server_snapshot.server_id != server_id
        ]
        self._stale_discard_count[server_id] = (
            self._stale_discard_count.get(server_id, 0) + len(stale)
        )
        for candidate in stale:
            self._schedule_candidate_close(candidate)

    def _schedule_candidate_close(self, candidate: McpServerCandidate) -> None:
        slot = candidate.manager_slot
        if slot is None:
            return
        manager = slot.manager
        manager_key = id(manager)
        with self._state_lock:
            if manager_key in self._candidate_cleanup_managers:
                return
            self._candidate_cleanup_managers[manager_key] = manager

        async def close_candidate() -> None:
            try:
                await manager.aclose(timeout_seconds=5.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep manager ownership for the bounded Host close retry path.
                return
            with self._state_lock:
                self._candidate_cleanup_managers.pop(manager_key, None)

        task = asyncio.create_task(
            close_candidate(),
            name=f"pulsara-mcp-stale-close:{candidate.server_snapshot.server_id}",
        )
        self._candidate_cleanup_tasks.add(task)
        task.add_done_callback(self._candidate_cleanup_tasks.discard)
        self._own_background_task(task)

    def _schedule_retiring_slot_close(self, slot: McpManagerSlot) -> None:
        with self._state_lock:
            if (
                self._slots.get(slot.slot_id) is not slot
                or slot.lifecycle != "retiring"
                or slot.borrower_count != 0
            ):
                return
            slot.lifecycle = "closing"

        async def close_slot() -> None:
            try:
                await slot.manager.aclose(timeout_seconds=5.0)
            except asyncio.CancelledError:
                with self._state_lock:
                    if self._slots.get(slot.slot_id) is slot:
                        slot.lifecycle = "retiring"
                raise
            except Exception:
                with self._state_lock:
                    if self._slots.get(slot.slot_id) is slot:
                        slot.lifecycle = "retiring"
                return
            with self._state_lock:
                slot.lifecycle = "closed"
                self._slots.pop(slot.slot_id, None)
                if self._installed_slot_by_server.get(slot.server_id) == slot.slot_id:
                    self._installed_slot_by_server.pop(slot.server_id, None)

        task = asyncio.create_task(
            close_slot(),
            name=f"pulsara-mcp-retiring-close:{slot.server_id}:{slot.slot_id}",
        )
        self._retiring_slot_cleanup_tasks.add(task)
        task.add_done_callback(self._retiring_slot_cleanup_tasks.discard)
        self._own_background_task(task)

    async def _run_attempt(self, runtime: _AttemptRuntime) -> None:
        spec = runtime.spec
        config = spec.config
        connection: SdkMcpConnection | None = None
        slot: McpManagerSlot | None = None
        connect_started_at = _utc_now()
        connect_started = time.monotonic()
        try:
            remaining = runtime.attempt.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP startup deadline expired")
            try:
                connection = await SdkMcpConnection.connect(
                    config,
                    timeout_seconds=min(
                        config.connect_timeout_ms / 1000,
                        remaining,
                    ),
                )
            except (SdkMcpConnectError, SdkMcpConnectCancelled) as exc:
                connection = exc.connection
                if isinstance(exc, SdkMcpConnectCancelled):
                    raise asyncio.CancelledError from exc
                raise
            connect_ended = time.monotonic()
            connect_ended_at = _utc_now()
            remaining = runtime.attempt.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP startup deadline expired before discovery")
            discovery_started_at = _utc_now()
            discovery_started = time.monotonic()
            snapshot, request_count, page_count = await discover_mcp_server(
                connection,
                config_epoch=runtime.attempt.config_epoch,
                reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
                discovery_generation=runtime.attempt.reserved_discovery_generation,
                queued_at_utc=runtime.queued_at_utc,
                queued_monotonic=runtime.queued_monotonic,
                connect_started_at_utc=connect_started_at,
                connect_ended_at_utc=connect_ended_at,
                connect_duration_seconds=max(0.0, connect_ended - connect_started),
                discovery_started_at_utc=discovery_started_at,
                discovery_started_monotonic=discovery_started,
                timeout_seconds=min(config.discovery_timeout_ms / 1000, remaining),
            )
            manager = SdkMcpClientManager.from_connected_server(
                connection=connection,
                snapshot=snapshot,
            )
            connection = None
            slot = new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager)
            candidate = McpServerCandidate(
                ticket_id=runtime.ticket_id,
                config_epoch=runtime.attempt.config_epoch,
                reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
                reserved_discovery_generation=runtime.attempt.reserved_discovery_generation,
                server_snapshot=snapshot,
                runtime_spec=spec,
                manager_slot=slot,
                trigger=runtime.trigger,  # type: ignore[arg-type]
                retry_attempt=self._retry_attempts.get(config.server_id, 0),
                request_count=request_count,
                page_count=page_count,
            )
        except asyncio.CancelledError:
            if connection is not None:
                await self._close_connection_or_retain(connection)
            raise
        except Exception as exc:
            if connection is not None:
                await self._close_connection_or_retain(connection)
            status = (
                McpServerStatus.NEEDS_AUTH
                if _is_missing_auth(config, exc)
                else (
                    McpServerStatus.FAILED
                    if config.required
                    else McpServerStatus.DEGRADED
                )
            )
            candidate = self._failed_candidate(runtime, status=status, exc=exc)
        with self._state_lock:
            current = self._current_attempts.get(config.server_id)
            accepted = (
                self._lifecycle == "open"
                and current is not None
                and current.attempt.reconcile_attempt_id == runtime.attempt.reconcile_attempt_id
                and current.attempt.reserved_discovery_generation
                == runtime.attempt.reserved_discovery_generation
                and current.spec.runtime_config_fingerprint == spec.runtime_config_fingerprint
                and self._epoch == runtime.attempt.config_epoch
            )
            if accepted:
                self._candidates.append(candidate)
                if candidate.server_snapshot.status is McpServerStatus.READY:
                    self._retry_attempts.pop(config.server_id, None)
                    self._next_retry_monotonic.pop(config.server_id, None)
                else:
                    self._schedule_retry(config.server_id)
            else:
                self._stale_discard_count[config.server_id] = (
                    self._stale_discard_count.get(config.server_id, 0) + 1
                )
        if not accepted and candidate.manager_slot is not None:
            self._schedule_candidate_close(candidate)

    async def _close_connection_or_retain(
        self,
        connection: SdkMcpConnection,
    ) -> None:
        try:
            await connection.aclose(timeout_seconds=5.0)
        except asyncio.CancelledError:
            with self._state_lock:
                self._orphan_connections[id(connection)] = connection
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception:
            with self._state_lock:
                self._orphan_connections[id(connection)] = connection

    def _reserve_attempt(self, spec: McpServerRuntimeSpec, *, now: float) -> McpServerAttempt:
        server_id = spec.config.server_id
        generation = self._generation_by_server.get(server_id, 0) + 1
        self._generation_by_server[server_id] = generation
        return McpServerAttempt(
            server_id=server_id,
            reconcile_attempt_id=f"mcp_attempt:{uuid4().hex}",
            config_epoch=self._epoch,
            reserved_discovery_generation=generation,
            runtime_config_fingerprint=spec.runtime_config_fingerprint,
            deadline_monotonic=now + spec.config.startup_deadline_ms / 1000,
        )

    def _reserve_disabled_candidate(
        self,
        *,
        server_id: str,
        ticket_id: str,
        trigger: str,
        required: bool,
        now: float,
        spec: McpServerRuntimeSpec | None = None,
    ) -> None:
        if spec is None:
            old = self._desired_specs.get(server_id)
            if old is None:
                return
            spec = old
        attempt = self._reserve_attempt(spec, now=now)
        runtime = _AttemptRuntime(
            ticket_id=ticket_id,
            trigger=trigger,
            attempt=attempt,
            spec=spec,
            queued_at_utc=_utc_now(),
            queued_monotonic=now,
        )
        self._current_attempts[server_id] = runtime
        completed_at = _utc_now()
        snapshot = McpServerSnapshot(
            snapshot_id=f"mcp_snapshot:{uuid4().hex}",
            server_id=server_id,
            config_epoch=self._epoch,
            event_safe_config_fingerprint=spec.event_safe_config_fingerprint,
            snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
                server_id=server_id,
                status=McpServerStatus.DISABLED,
            ),
            reconcile_attempt_id=attempt.reconcile_attempt_id,
            discovery_generation=attempt.reserved_discovery_generation,
            status=McpServerStatus.DISABLED,
            required=required,
            timing=McpServerLifecycleTimingFact(
                queued_at_utc=runtime.queued_at_utc,
                completed_at_utc=completed_at,
                total_duration_seconds=0.0,
            ),
        )
        self._candidates.append(
            McpServerCandidate(
                ticket_id=ticket_id,
                config_epoch=self._epoch,
                reconcile_attempt_id=attempt.reconcile_attempt_id,
                reserved_discovery_generation=attempt.reserved_discovery_generation,
                server_snapshot=snapshot,
                runtime_spec=spec,
                manager_slot=None,
                trigger=trigger,  # type: ignore[arg-type]
                cache_outcome="not_applicable",
            )
        )

    def _failed_candidate(
        self,
        runtime: _AttemptRuntime,
        *,
        status: McpServerStatus,
        exc: Exception,
    ) -> McpServerCandidate:
        completed_at = _utc_now()
        elapsed = max(0.0, time.monotonic() - runtime.queued_monotonic)
        config = runtime.spec.config
        snapshot = McpServerSnapshot(
            snapshot_id=f"mcp_snapshot:{uuid4().hex}",
            server_id=config.server_id,
            config_epoch=runtime.attempt.config_epoch,
            event_safe_config_fingerprint=runtime.spec.event_safe_config_fingerprint,
            snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
                server_id=config.server_id,
                status=status,
            ),
            reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
            discovery_generation=runtime.attempt.reserved_discovery_generation,
            status=status,
            required=config.required,
            message=f"{type(exc).__name__}: {redact_mcp_error_message(exc)}",
            diagnostics=(
                {
                    "code": "mcp_server_startup_failed",
                    "error_type": type(exc).__name__,
                },
            ),
            timing=McpServerLifecycleTimingFact(
                queued_at_utc=runtime.queued_at_utc,
                completed_at_utc=completed_at,
                total_duration_seconds=elapsed,
            ),
        )
        return McpServerCandidate(
            ticket_id=runtime.ticket_id,
            config_epoch=runtime.attempt.config_epoch,
            reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
            reserved_discovery_generation=runtime.attempt.reserved_discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=runtime.spec,
            manager_slot=None,
            trigger=runtime.trigger,  # type: ignore[arg-type]
            retry_attempt=self._retry_attempts.get(config.server_id, 0),
            cache_outcome="miss",
        )

    def _installed_slot_for_server(self, server_id: str) -> McpManagerSlot | None:
        slot_id = self._installed_slot_by_server.get(server_id)
        return self._slots.get(slot_id) if slot_id is not None else None

    def _latest_candidate(
        self, server_id: str, reconcile_attempt_id: str
    ) -> McpServerCandidate | None:
        with self._state_lock:
            for candidate in reversed(self._candidates):
                if (
                    candidate.server_snapshot.server_id == server_id
                    and candidate.reconcile_attempt_id == reconcile_attempt_id
                ):
                    return candidate
        return None

    def _schedule_retry(self, server_id: str) -> None:
        with self._state_lock:
            spec = self._desired_specs.get(server_id)
            if (
                self._lifecycle != "open"
                or spec is None
                or not spec.config.enabled
            ):
                return
            attempt = self._retry_attempts.get(server_id, 0) + 1
            self._retry_attempts[server_id] = attempt
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** (attempt - 1)),
            )
            deadline = time.monotonic() + delay
            self._next_retry_monotonic[server_id] = deadline
            previous = self._retry_tasks.pop(server_id, None)
            if previous is not None and previous is not asyncio.current_task():
                previous.cancel()
            expected_epoch = self._epoch
            expected_runtime_fingerprint = spec.runtime_config_fingerprint

            async def retry_after_backoff() -> None:
                task = asyncio.current_task()
                try:
                    await asyncio.sleep(max(0.0, deadline - time.monotonic()))
                    with self._state_lock:
                        desired = self._desired_specs.get(server_id)
                        if (
                            self._lifecycle != "open"
                            or self._epoch != expected_epoch
                            or desired is None
                            or not desired.config.enabled
                            or desired.runtime_config_fingerprint
                            != expected_runtime_fingerprint
                            or self._retry_tasks.get(server_id) is not task
                        ):
                            return
                        configs = tuple(
                            current.config
                            for current in self._desired_specs.values()
                        )
                    self.prepare(configs, trigger="retry")
                finally:
                    with self._state_lock:
                        if self._retry_tasks.get(server_id) is task:
                            self._retry_tasks.pop(server_id, None)

            timer = asyncio.create_task(
                retry_after_backoff(),
                name=f"pulsara-mcp-retry:{server_id}:{attempt}",
            )
            self._retry_tasks[server_id] = timer
            self._retry_timer_tasks.add(timer)
            timer.add_done_callback(self._retry_timer_tasks.discard)
            self._own_background_task(timer)

    def _reset_retry_state(self, server_id: str) -> None:
        timer = self._retry_tasks.pop(server_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self._retry_attempts.pop(server_id, None)
        self._next_retry_monotonic.pop(server_id, None)


def _starting_snapshot(runtime: _AttemptRuntime) -> McpServerSnapshot:
    return McpServerSnapshot(
        snapshot_id=f"mcp_snapshot:{uuid4().hex}",
        server_id=runtime.spec.config.server_id,
        config_epoch=runtime.attempt.config_epoch,
        event_safe_config_fingerprint=runtime.spec.event_safe_config_fingerprint,
        snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
            server_id=runtime.spec.config.server_id,
            status=McpServerStatus.STARTING,
        ),
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
        discovery_generation=runtime.attempt.reserved_discovery_generation,
        status=McpServerStatus.STARTING,
        required=runtime.spec.config.required,
        timing=McpServerLifecycleTimingFact(queued_at_utc=runtime.queued_at_utc),
    )


def _is_missing_auth(config: McpServerConfig, exc: Exception) -> bool:
    transport = config.transport
    env_var = getattr(transport, "bearer_token_env_var", None)
    if env_var and not os.getenv(env_var):
        return True
    return "auth" in str(exc).lower() or "token" in str(exc).lower()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["McpServerSupervisor"]
