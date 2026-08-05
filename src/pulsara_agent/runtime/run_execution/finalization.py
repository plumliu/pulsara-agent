"""Stable run-finalization task ownership outside an activation segment."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from pulsara_agent.event import AgentEvent
from pulsara_agent.ports.event_write import EventReconciliationRequired
from pulsara_agent.runtime.run_execution.owner import RunFinalizationOwner
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry

if TYPE_CHECKING:
    from pulsara_agent.runtime.state import RunActivationWorkingState


FinalizationOperation = Callable[[], AsyncIterator[AgentEvent]]
OutputMaterializationOperation = Callable[[], Awaitable[None]]


class RunFinalizationService:
    """Own finalization physical tasks after their activation has exited.

    The first caller installs the operation on the stable ``RunOwner``. Later
    callers join the same task. Cancelling a waiter never cancels the ledger
    mutation or final-output materialization it is observing.
    """

    def __init__(self, *, registry: RunExecutionRegistry, repair_port=None) -> None:
        self._registry = registry
        self._repair_port = repair_port
        self._accepting = True
        # ``drain()`` closes external admission immediately, but an already
        # admitted physical terminalization still owns the right to install
        # its RunEnd-FULL output-materialization successor.  Keeping that
        # lineage distinct avoids either accepting a new finalization during
        # close or cutting the admitted one in half.
        self._draining = False

    async def finalize(
        self,
        *,
        run_id: str,
        state: "RunActivationWorkingState",
        operation: FinalizationOperation,
    ) -> tuple[AgentEvent, ...]:
        if not self._accepting:
            raise RuntimeError("run finalization service is closing")
        run_owner = self._registry.require(run_id)
        segment = run_owner.active_segment
        finalization = run_owner.finalization_slot.owner
        active_state = (
            segment.state_carrier.borrow(owner_token=segment.state_owner_token)
            if segment is not None
            and segment.state_carrier is not None
            and segment.state_owner_token is not None
            else None
        )
        finalization_state = (
            finalization.state_carrier.borrow(
                owner_token=finalization.state_owner_token
            )
            if isinstance(finalization, RunFinalizationOwner)
            and finalization.state_carrier is not None
            and finalization.state_owner_token is not None
            else None
        )
        if state is not active_state and state is not finalization_state:
            raise RuntimeError("run finalization working-state authority mismatch")
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("run owner lacks its stable finalization owner")
        task = finalization.physical_task
        if task is not None and task.done():
            try:
                return task.result()
            except (asyncio.CancelledError, Exception):
                if finalization.state not in {
                    "candidate_frozen",
                    "committing",
                    "retry_wait",
                    "waiting_reducer_repair",
                    "full_output_pending",
                }:
                    raise
                finalization.physical_task = None
                task = None
        if task is None:
            run_owner.finalization_slot.state = "active"
            task = asyncio.create_task(
                self._execute(run_id=run_id, operation=operation),
                name=f"run-finalization:{run_id}",
            )
            finalization.physical_task = task
            task.add_done_callback(_consume_task_exception)
        return await asyncio.shield(task)

    async def _execute(
        self,
        *,
        run_id: str,
        operation: FinalizationOperation,
    ) -> tuple[AgentEvent, ...]:
        events: list[AgentEvent] = []
        async for event in operation():
            events.append(event)
        return tuple(events)

    def continue_terminalization(
        self,
        *,
        run_id: str,
        state: "RunActivationWorkingState",
        operation: FinalizationOperation,
    ) -> asyncio.Future[tuple[AgentEvent, ...]]:
        """Install the stable repair task after an activation relinquishes state."""

        if not self._accepting:
            raise RuntimeError("run finalization service is closing")
        run_owner = self._registry.require(run_id)
        finalization = run_owner.finalization_slot.owner
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("run owner lacks its stable finalization owner")
        if (
            finalization.state_carrier is None
            or finalization.state_owner_token is None
            or finalization.state_carrier.borrow(
                owner_token=finalization.state_owner_token
            )
            is not state
        ):
            raise RuntimeError("terminalization repair lacks finalization-owned state")
        task = finalization.physical_task
        if task is not None and not task.done():
            return asyncio.shield(task)
        if task is not None:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                if finalization.state in {"completed", "reconciliation_required"}:
                    return asyncio.shield(task)
            else:
                return asyncio.shield(task)
        task = asyncio.create_task(
            self._execute_repair(run_id=run_id, operation=operation),
            name=f"run-terminalization-repair:{run_id}",
        )
        finalization.physical_task = task
        task.add_done_callback(_consume_task_exception)
        # The returned future is only a waiter.  Cancelling it detaches the
        # caller while the service-owned physical task keeps the stable RunEnd
        # candidate and continues to its typed terminal state.
        return asyncio.shield(task)

    async def _execute_repair(
        self,
        *,
        run_id: str,
        operation: FinalizationOperation,
    ) -> tuple[AgentEvent, ...]:
        delay_seconds = 0.01
        immediate_retry_count = 0
        while True:
            events: list[AgentEvent] = []
            try:
                async for event in operation():
                    events.append(event)
                return tuple(events)
            except asyncio.CancelledError:
                raise
            except EventReconciliationRequired:
                owner = self._registry.get(run_id)
                finalization = (
                    owner.finalization_slot.owner if owner is not None else None
                )
                if not isinstance(
                    finalization, RunFinalizationOwner
                ) or finalization.state in {"completed", "reconciliation_required"}:
                    raise
                now = time.monotonic()
                finalization.first_failure_monotonic = (
                    now
                    if finalization.first_failure_monotonic is None
                    else finalization.first_failure_monotonic
                )
                finalization.last_failure_monotonic = now
                finalization.last_failure_code = "EVENT_RECONCILIATION_REQUIRED"
                if self._repair_port is None:
                    finalization.state = "reconciliation_required"
                    finalization.last_failure_code = "REDUCER_REPAIR_PORT_UNAVAILABLE"
                    raise
                # The service-owned repair may win between the rejected RunEnd
                # attempt and this exception handler.  In that case the latch
                # is already clear and the same stable RunEnd candidate may be
                # retried without inventing a new dependency.
                if not getattr(
                    self._repair_port,
                    "reconciliation_required",
                    True,
                ):
                    finalization.state = "candidate_frozen"
                    finalization.reducer_repair_handles = ()
                    immediate_retry_count = 0
                    delay_seconds = 0.01
                    continue
                try:
                    handles = (
                        self._repair_port.pending_committed_reducer_repair_handles()
                    )
                except BaseException:
                    finalization.state = "reconciliation_required"
                    finalization.last_failure_code = (
                        "REDUCER_REPAIR_HANDLE_RESOLUTION_FAILED"
                    )
                    raise
                if not handles:
                    finalization.state = "reconciliation_required"
                    finalization.last_failure_code = "REDUCER_REPAIR_OWNER_UNAVAILABLE"
                    raise
                finalization.state = "waiting_reducer_repair"
                finalization.reducer_repair_handles = handles
                repair_deadline = time.monotonic() + 30.0
                try:
                    for handle in handles:
                        receipt = await self._repair_port.wait_committed_reducer_repair(
                            handle,
                            deadline_monotonic=repair_deadline,
                        )
                        if (
                            getattr(receipt, "plan_fingerprint", None)
                            != getattr(handle, "plan_fingerprint", None)
                            or getattr(receipt, "reducer_id", None)
                            != getattr(handle, "reducer_id", None)
                            or getattr(receipt, "repaired_through_sequence", None)
                            != getattr(handle, "target_ledger_high_water", None)
                        ):
                            raise RuntimeError(
                                "finalization reducer repair receipt is stale"
                            )
                except BaseException as exc:
                    finalization.state = "reconciliation_required"
                    finalization.last_failure_monotonic = time.monotonic()
                    finalization.last_failure_code = type(exc).__name__.upper()
                    raise
                finalization.reducer_repair_handles = ()
                finalization.state = "candidate_frozen"
                immediate_retry_count = 0
                delay_seconds = 0.01
                continue
            except BaseException as exc:
                owner = self._registry.get(run_id)
                finalization = (
                    owner.finalization_slot.owner if owner is not None else None
                )
                if not isinstance(finalization, RunFinalizationOwner):
                    raise
                outcome = (
                    None
                    if self._repair_port is None
                    else self._repair_port.resolved_write_outcome(exc)
                )
                if outcome is None or outcome.status == "unknown":
                    finalization.state = "reconciliation_required"
                    raise
                if outcome.status == "full":
                    if (
                        finalization.commit_state == "confirmed"
                        and finalization.state in {"full_output_pending", "completed"}
                    ):
                        # The operation itself owns exact candidate validation
                        # and installs the confirmed RunEnd reference before it
                        # propagates a cancellation/publication-after-commit
                        # result.  Do not let that already-adopted FULL winner
                        # regress to reconciliation at this outer task owner.
                        return tuple(outcome.committed_events)
                    finalization.state = "reconciliation_required"
                    raise RuntimeError(
                        "finalization operation lost a compatible FULL outcome"
                    ) from exc
                now = time.monotonic()
                finalization.physical_attempt_generation += 1
                finalization.first_failure_monotonic = (
                    now
                    if finalization.first_failure_monotonic is None
                    else finalization.first_failure_monotonic
                )
                finalization.last_failure_monotonic = now
                finalization.last_failure_code = type(exc).__name__.upper()
                finalization.state = "retry_wait"
                immediate_retry_count += 1
                if immediate_retry_count > 4:
                    delay_seconds = min(max(delay_seconds, 0.1) * 2.0, 1.0)
                finalization.retry_not_before_monotonic = now + delay_seconds
                await asyncio.sleep(delay_seconds)
                finalization.retry_not_before_monotonic = None

    def continue_output_materialization(
        self,
        *,
        run_id: str,
        operation: OutputMaterializationOperation,
    ) -> asyncio.Task[None]:
        """Install the one stable output-recovery task for a RunEnd-FULL run."""

        run_owner = self._registry.require(run_id)
        finalization = run_owner.finalization_slot.owner
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("run owner lacks its stable finalization owner")
        if not self._accepting and not (
            self._draining and finalization.physical_task is asyncio.current_task()
        ):
            raise RuntimeError("run finalization service is closing")
        if run_owner.finalization_owner.commit_state != "confirmed":
            raise RuntimeError("output materialization cannot precede RunEnd FULL")
        task = finalization.output_materialization_task
        if task is not None:
            if not task.done():
                return task
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                if finalization.state != "full_output_pending":
                    raise
            else:
                return task
        task = asyncio.create_task(
            self._execute_output_materialization(run_id=run_id, operation=operation),
            name=f"run-final-output:{run_id}",
        )
        finalization.output_materialization_task = task
        task.add_done_callback(_consume_task_exception)
        return task

    async def _execute_output_materialization(
        self,
        *,
        run_id: str,
        operation: OutputMaterializationOperation,
    ) -> None:
        try:
            await operation()
        finally:
            owner = self._registry.get(run_id)
            if owner is None:
                return
            finalization = owner.finalization_slot.owner
            if (
                isinstance(finalization, RunFinalizationOwner)
                and finalization.output_materialization_task is asyncio.current_task()
                and finalization.state == "completed"
            ):
                finalization.output_materialization_task = None

    async def drain(self, *, deadline_monotonic: float) -> None:
        self._accepting = False
        self._draining = True
        while True:
            # Resnapshot after every join.  A physical RunEnd task is allowed
            # to install exactly one internal output successor while close is
            # in progress, so a one-shot task snapshot is not a drain proof.
            admitted = tuple(
                (owner, finalization)
                for owner in self._registry.owners()
                if isinstance(
                    (finalization := owner.finalization_slot.owner),
                    RunFinalizationOwner,
                )
            )
            task_owners = tuple(
                (owner, finalization, kind, task)
                for owner, finalization in admitted
                for kind, task in (
                    ("physical", finalization.physical_task),
                    ("output", finalization.output_materialization_task),
                )
                if task is not None
            )
            # A done task is evidence, not absence.  Consume its result before
            # deciding that the service is idle so cancellation or a terminal
            # exception cannot be silently converted into a successful close.
            for owner, _finalization, kind, task in task_owners:
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError as exc:
                    raise RuntimeError(
                        f"run finalization {kind} task was cancelled for "
                        f"{owner.identity.run_id}"
                    ) from exc
                except BaseException as exc:
                    raise RuntimeError(
                        f"run finalization {kind} task failed for "
                        f"{owner.identity.run_id}"
                    ) from exc
            tasks = tuple(
                dict.fromkeys(
                    task
                    for _owner, _finalization, _kind, task in task_owners
                    if not task.done()
                )
            )
            if not tasks:
                blocked = tuple(
                    (owner.identity.run_id, finalization.state)
                    for owner, finalization in admitted
                    if finalization.state not in {"idle", "completed"}
                )
                if blocked:
                    raise RuntimeError(
                        "run finalization close blocked by unresolved owners: "
                        f"{blocked!r}"
                    )
                self._draining = False
                return
            for task in tasks:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("run finalization drain deadline exceeded")
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except TimeoutError as exc:
                    raise TimeoutError(
                        "run finalization drain deadline exceeded"
                    ) from exc


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


__all__ = ["RunFinalizationService"]
