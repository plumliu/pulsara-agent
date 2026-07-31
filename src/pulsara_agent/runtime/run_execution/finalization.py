"""Stable run-finalization task ownership outside an activation segment."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from pulsara_agent.event import AgentEvent
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

    def __init__(self, *, registry: RunExecutionRegistry) -> None:
        self._registry = registry
        self._accepting = True

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
    ) -> asyncio.Task[tuple[AgentEvent, ...]]:
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
            return task
        if task is not None:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                if finalization.state in {"completed", "reconciliation_required"}:
                    return task
            else:
                return task
        task = asyncio.create_task(
            self._execute_repair(run_id=run_id, operation=operation),
            name=f"run-terminalization-repair:{run_id}",
        )
        finalization.physical_task = task
        task.add_done_callback(_consume_task_exception)
        return task

    async def _execute_repair(
        self,
        *,
        run_id: str,
        operation: FinalizationOperation,
    ) -> tuple[AgentEvent, ...]:
        delay_seconds = 0.01
        while True:
            events: list[AgentEvent] = []
            try:
                async for event in operation():
                    events.append(event)
                return tuple(events)
            except asyncio.CancelledError:
                raise
            except BaseException:
                owner = self._registry.get(run_id)
                finalization = (
                    owner.finalization_slot.owner if owner is not None else None
                )
                if not isinstance(
                    finalization, RunFinalizationOwner
                ) or finalization.state in {"completed", "reconciliation_required"}:
                    raise
                finalization.state = "retry_wait"
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2.0, 0.25)

    def continue_output_materialization(
        self,
        *,
        run_id: str,
        operation: OutputMaterializationOperation,
    ) -> asyncio.Task[None]:
        """Install the one stable output-recovery task for a RunEnd-FULL run."""

        if not self._accepting:
            raise RuntimeError("run finalization service is closing")
        run_owner = self._registry.require(run_id)
        finalization = run_owner.finalization_slot.owner
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("run owner lacks its stable finalization owner")
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
        tasks = tuple(
            task
            for owner in self._registry.owners()
            if isinstance(
                (finalization := owner.finalization_slot.owner),
                RunFinalizationOwner,
            )
            for task in (
                finalization.physical_task,
                finalization.output_materialization_task,
            )
            if task is not None and not task.done()
        )
        for task in dict.fromkeys(tasks):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("run finalization drain deadline exceeded")
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


__all__ = ["RunFinalizationService"]
