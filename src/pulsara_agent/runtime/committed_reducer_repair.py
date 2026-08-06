"""Session-owned live repair tasks for committed semantic reducers."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Callable

from pulsara_agent.blocking_executor import projection_maintenance_executor
from pulsara_agent.primitives.context import context_fingerprint


class CommittedReducerRepairState(StrEnum):
    INSTALLED = "installed"
    REBUILDING = "rebuilding"
    VERIFYING = "verifying"
    REPAIRED = "repaired"
    RETRY_WAIT = "retry_wait"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    OFFLINE_REPAIR_REQUIRED = "offline_repair_required"


@dataclass(frozen=True, slots=True)
class CommittedReducerRepairPlan:
    reducer_id: str
    failed_registration_high_water: int
    target_ledger_high_water: int
    last_error_code: str
    recovery_base_identity: str
    plan_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "committed-reducer-repair-plan:v1",
            {
                "reducer_id": self.reducer_id,
                "failed_registration_high_water": (self.failed_registration_high_water),
                "target_ledger_high_water": self.target_ledger_high_water,
                "last_error_code": self.last_error_code,
                "recovery_base_identity": self.recovery_base_identity,
            },
        )
        if self.plan_fingerprint != expected:
            raise ValueError("committed reducer repair plan fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CommittedReducerRepairReceipt:
    plan_fingerprint: str
    reducer_id: str
    repaired_through_sequence: int
    resulting_semantic_state_fingerprint: str
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "committed-reducer-repair-receipt:v1",
            {
                "plan_fingerprint": self.plan_fingerprint,
                "reducer_id": self.reducer_id,
                "repaired_through_sequence": self.repaired_through_sequence,
                "resulting_semantic_state_fingerprint": (
                    self.resulting_semantic_state_fingerprint
                ),
            },
        )
        if self.receipt_fingerprint != expected:
            raise ValueError("committed reducer repair receipt fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CommittedReducerRepairHandle:
    reducer_id: str
    target_ledger_high_water: int
    plan_fingerprint: str


@dataclass(slots=True)
class _Attempt:
    plan: CommittedReducerRepairPlan
    state: CommittedReducerRepairState
    task: asyncio.Task[CommittedReducerRepairReceipt] | None = None
    physical_future: ConcurrentFuture[CommittedReducerRepairReceipt] | None = None
    receipt: CommittedReducerRepairReceipt | None = None
    last_error_code: str | None = None
    physical_generation: int = 0
    retry_not_before: float = 0.0
    first_failure_monotonic: float | None = None
    last_failure_monotonic: float | None = None
    deadline_monotonic: float = 0.0


class CommittedReducerRepairService:
    """Keep exact repair tasks alive after individual waiters detach."""

    def __init__(
        self,
        *,
        repair_operation: Callable[
            [CommittedReducerRepairPlan, float],
            CommittedReducerRepairReceipt,
        ],
    ) -> None:
        self._repair_operation = repair_operation
        self._lock = RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attempts: dict[str, _Attempt] = {}
        # A reducer may fail again after an earlier exact repair completed.
        # Keep a tiny receipt tombstone window so a waiter that captured the
        # prior handle can still observe its compatible winner while the
        # reducer's current slot advances to the new stable plan.
        self._retired_receipts: dict[str, dict[str, CommittedReducerRepairReceipt]] = {}
        self._accepting = True

    def bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                if not self._loop.is_closed():
                    raise RuntimeError("committed reducer repair loop identity changed")
                for attempt in self._attempts.values():
                    task = attempt.task
                    if task is not None and not task.done():
                        raise RuntimeError(
                            "closed reducer-repair loop still owns a live task"
                        )
                    if task is not None:
                        attempt.task = None
                        if task.cancelled() and attempt.state not in {
                            CommittedReducerRepairState.REPAIRED,
                            CommittedReducerRepairState.RECONCILIATION_REQUIRED,
                            CommittedReducerRepairState.OFFLINE_REPAIR_REQUIRED,
                        }:
                            attempt.state = (
                                CommittedReducerRepairState.REBUILDING
                                if attempt.physical_future is not None
                                else CommittedReducerRepairState.INSTALLED
                            )
            self._loop = loop
            for attempt in self._attempts.values():
                self._start_locked(attempt)

    def install(
        self,
        *,
        reducer_id: str,
        failed_registration_high_water: int,
        target_ledger_high_water: int,
        last_error_code: str,
        recovery_base_identity: str,
    ) -> CommittedReducerRepairHandle:
        payload = {
            "reducer_id": reducer_id,
            "failed_registration_high_water": failed_registration_high_water,
            "target_ledger_high_water": target_ledger_high_water,
            "last_error_code": last_error_code,
            "recovery_base_identity": recovery_base_identity,
        }
        plan = CommittedReducerRepairPlan(
            **payload,
            plan_fingerprint=context_fingerprint(
                "committed-reducer-repair-plan:v1", payload
            ),
        )
        with self._lock:
            if not self._accepting:
                raise RuntimeError("committed reducer repair service is closing")
            existing = self._attempts.get(reducer_id)
            if existing is not None:
                if (
                    existing.plan.failed_registration_high_water
                    == failed_registration_high_water
                    and existing.plan.target_ledger_high_water
                    >= target_ledger_high_water
                ):
                    # Duplicate adoption of the same physical failure reuses
                    # the already-installed stable plan.
                    plan = existing.plan
                elif (
                    existing.state is CommittedReducerRepairState.REPAIRED
                    and existing.receipt is not None
                    and failed_registration_high_water
                    >= existing.plan.target_ledger_high_water
                    and target_ledger_high_water > failed_registration_high_water
                ):
                    self._retire_receipt_locked(existing)
                    existing = None
                else:
                    raise RuntimeError("committed reducer repair plan conflicts")
            if existing is None:
                installed_at = monotonic()
                existing = _Attempt(
                    plan=plan,
                    state=CommittedReducerRepairState.INSTALLED,
                    last_error_code=last_error_code,
                    first_failure_monotonic=installed_at,
                    last_failure_monotonic=installed_at,
                    deadline_monotonic=installed_at + 30.0,
                )
                self._attempts[reducer_id] = existing
            loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._start, reducer_id)
        return CommittedReducerRepairHandle(
            reducer_id=reducer_id,
            target_ledger_high_water=plan.target_ledger_high_water,
            plan_fingerprint=plan.plan_fingerprint,
        )

    def _start(self, reducer_id: str) -> None:
        with self._lock:
            attempt = self._attempts.get(reducer_id)
            if attempt is not None:
                self._start_locked(attempt)

    def _start_locked(self, attempt: _Attempt) -> None:
        if (
            self._loop is None
            or attempt.task is not None
            or attempt.state
            in {
                CommittedReducerRepairState.REPAIRED,
                CommittedReducerRepairState.RECONCILIATION_REQUIRED,
                CommittedReducerRepairState.OFFLINE_REPAIR_REQUIRED,
            }
        ):
            return
        attempt.task = self._loop.create_task(
            self._run_attempt(attempt),
            name=f"committed-reducer-repair:{attempt.plan.reducer_id}",
        )
        attempt.task.add_done_callback(_consume_task_exception)

    async def _run_attempt(self, attempt: _Attempt) -> CommittedReducerRepairReceipt:
        deadline = attempt.deadline_monotonic
        while True:
            with self._lock:
                retry_delay = max(0.0, attempt.retry_not_before - monotonic())
            if retry_delay:
                await asyncio.sleep(retry_delay)
            with self._lock:
                physical = attempt.physical_future
                if physical is None:
                    attempt.physical_generation += 1
                    physical = projection_maintenance_executor().submit(
                        self._repair_operation,
                        attempt.plan,
                        deadline,
                    )
                    attempt.physical_future = physical
                generation = attempt.physical_generation
                attempt.state = CommittedReducerRepairState.REBUILDING
            try:
                # The concurrent future, not this loop-bound waiter, owns the
                # physical repair.  Shielding prevents loop/task cancellation
                # from cancelling a queued executor operation.  A successor
                # event loop wraps and joins the exact same future.
                receipt = await asyncio.shield(asyncio.wrap_future(physical))
            except asyncio.CancelledError:
                # Event-loop teardown only detaches this driver.  The executor
                # future remains installed on the attempt and is rejoined by
                # the next loop; it must never be submitted again.
                raise
            except BaseException as exc:
                with self._lock:
                    if attempt.physical_future is physical:
                        attempt.physical_future = None
                if _is_transient_repair_failure(exc) and monotonic() < deadline:
                    delay = min(0.5, 0.025 * (2 ** min(generation - 1, 4)))
                    with self._lock:
                        now = monotonic()
                        attempt.last_error_code = type(exc).__name__.upper()
                        attempt.retry_not_before = now + delay
                        attempt.last_failure_monotonic = now
                        attempt.state = CommittedReducerRepairState.RETRY_WAIT
                    await asyncio.sleep(delay)
                    continue
                with self._lock:
                    now = monotonic()
                    attempt.last_error_code = type(exc).__name__.upper()
                    attempt.last_failure_monotonic = now
                    attempt.state = (
                        CommittedReducerRepairState.OFFLINE_REPAIR_REQUIRED
                        if isinstance(exc, OnlineReducerRepairBoundExceeded)
                        else CommittedReducerRepairState.RECONCILIATION_REQUIRED
                    )
                raise
            break
        with self._lock:
            if attempt.physical_future is physical:
                attempt.physical_future = None
            attempt.state = CommittedReducerRepairState.VERIFYING
            if receipt.plan_fingerprint != attempt.plan.plan_fingerprint:
                attempt.state = CommittedReducerRepairState.RECONCILIATION_REQUIRED
                raise RuntimeError("committed reducer repair receipt is stale")
            attempt.receipt = receipt
            attempt.state = CommittedReducerRepairState.REPAIRED
        return receipt

    def owns(self, reducer_id: str, target_through_sequence: int) -> bool:
        with self._lock:
            attempt = self._attempts.get(reducer_id)
            return bool(
                attempt is not None
                and attempt.plan.target_ledger_high_water >= target_through_sequence
                and attempt.state
                not in {
                    CommittedReducerRepairState.RECONCILIATION_REQUIRED,
                    CommittedReducerRepairState.OFFLINE_REPAIR_REQUIRED,
                }
            )

    def handle_for(
        self, reducer_id: str, target_through_sequence: int
    ) -> CommittedReducerRepairHandle | None:
        with self._lock:
            attempt = self._attempts.get(reducer_id)
            if (
                attempt is None
                or attempt.plan.target_ledger_high_water < target_through_sequence
            ):
                return None
            return CommittedReducerRepairHandle(
                reducer_id=reducer_id,
                target_ledger_high_water=attempt.plan.target_ledger_high_water,
                plan_fingerprint=attempt.plan.plan_fingerprint,
            )

    async def wait(
        self,
        handle: CommittedReducerRepairHandle,
        *,
        deadline_monotonic: float,
    ) -> CommittedReducerRepairReceipt:
        self.bind_running_loop()
        with self._lock:
            attempt = self._attempts.get(handle.reducer_id)
            if (
                attempt is None
                or attempt.plan.plan_fingerprint != handle.plan_fingerprint
            ):
                receipt = self._retired_receipts.get(handle.reducer_id, {}).get(
                    handle.plan_fingerprint
                )
                if (
                    receipt is None
                    or receipt.repaired_through_sequence
                    != handle.target_ledger_high_water
                ):
                    raise RuntimeError("committed reducer repair handle is stale")
                return receipt
            task = attempt.task
            if attempt.receipt is not None:
                return attempt.receipt
            if attempt.state in {
                CommittedReducerRepairState.RECONCILIATION_REQUIRED,
                CommittedReducerRepairState.OFFLINE_REPAIR_REQUIRED,
            }:
                raise RuntimeError(
                    "committed reducer repair requires explicit reconciliation"
                )
        assert task is not None
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("committed reducer repair waiter deadline expired")
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)

    def _retire_receipt_locked(self, attempt: _Attempt) -> None:
        receipt = attempt.receipt
        if receipt is None:
            raise RuntimeError("repaired reducer attempt lacks its receipt")
        receipts = self._retired_receipts.setdefault(attempt.plan.reducer_id, {})
        receipts[attempt.plan.plan_fingerprint] = receipt
        # Process-local recovery history is acceleration only.  Two completed
        # winners cover a waiter spanning one subsequent independent failure
        # without allowing session lifetime to grow resident memory.
        while len(receipts) > 2:
            receipts.pop(next(iter(receipts)))

    def diagnostics(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "reducer_id": item.plan.reducer_id,
                    "state": item.state.value,
                    "target_high_water": item.plan.target_ledger_high_water,
                    "plan_fingerprint": item.plan.plan_fingerprint,
                    "last_error_code": item.last_error_code,
                    "physical_generation": item.physical_generation,
                    "retry_not_before": item.retry_not_before,
                    "first_failure_monotonic": item.first_failure_monotonic,
                    "last_failure_monotonic": item.last_failure_monotonic,
                }
                for item in self._attempts.values()
            )

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        """Drain installed repairs without closing admission for RunEnd folds."""

        self.bind_running_loop()
        with self._lock:
            tasks = tuple(
                item.task
                for item in self._attempts.values()
                if item.task is not None and not item.task.done()
            )
        for task in tasks:
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("committed reducer repair close blocked")
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        with self._lock:
            self._accepting = False
        await self.drain_pending(deadline_monotonic=deadline_monotonic)

    def close_if_idle(self) -> None:
        with self._lock:
            if any(
                (item.task is not None and not item.task.done())
                or (
                    item.physical_future is not None and not item.physical_future.done()
                )
                for item in self._attempts.values()
            ):
                raise RuntimeError("committed reducer repair service is not idle")
            self._accepting = False


class OnlineReducerRepairBoundExceeded(RuntimeError):
    """The online checkpoint delta exceeded its frozen recovery bound."""


def build_committed_reducer_repair_receipt(
    *,
    plan: CommittedReducerRepairPlan,
    resulting_semantic_state_fingerprint: str,
) -> CommittedReducerRepairReceipt:
    payload = {
        "plan_fingerprint": plan.plan_fingerprint,
        "reducer_id": plan.reducer_id,
        "repaired_through_sequence": plan.target_ledger_high_water,
        "resulting_semantic_state_fingerprint": (resulting_semantic_state_fingerprint),
    }
    return CommittedReducerRepairReceipt(
        **payload,
        receipt_fingerprint=context_fingerprint(
            "committed-reducer-repair-receipt:v1", payload
        ),
    )


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


def _is_transient_repair_failure(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    return type(error).__name__ in {
        "InterfaceError",
        "OperationalError",
        "PoolTimeout",
    }


__all__ = [
    "CommittedReducerRepairHandle",
    "CommittedReducerRepairPlan",
    "CommittedReducerRepairReceipt",
    "CommittedReducerRepairService",
    "CommittedReducerRepairState",
    "OnlineReducerRepairBoundExceeded",
    "build_committed_reducer_repair_receipt",
]
