"""Process-local Plan QUESTION waiter and Host continuation ownership.

The canonical repository owns every accepted Plan fact.  This module only
bridges a same-Host running coroutine to a canonical QUESTION resolution and
keeps automatic continuation tasks alive when an origin request detaches.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable

from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanResolution,
    ConversationKernelConflict,
)
from pulsara_agent.primitives.plan_workflow import PlanQuestionContent


@dataclass(frozen=True, slots=True)
class PlanQuestionWaiter:
    interaction_id: str
    origin_turn_id: str
    waiter_generation: int
    _future: asyncio.Future[AcceptedPlanResolution]


@dataclass(frozen=True, slots=True)
class OpenPlanQuestion:
    interaction_id: str
    origin_turn_id: str
    question: PlanQuestionContent
    waiter_generation: int


class KernelPlanInteractionCoordinator:
    """One dormant-before-write QUESTION waiter per active Plan workflow."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._waiter: PlanQuestionWaiter | None = None
        self._open: OpenPlanQuestion | None = None
        self._generation = 0
        self._closed = False

    async def prepare_question(
        self, *, interaction_id: str, origin_turn_id: str
    ) -> PlanQuestionWaiter:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Plan interaction coordinator is closed")
            if self._waiter is not None:
                raise RuntimeError("another Plan question waiter is active")
            self._generation += 1
            waiter = PlanQuestionWaiter(
                interaction_id=interaction_id,
                origin_turn_id=origin_turn_id,
                waiter_generation=self._generation,
                _future=asyncio.get_running_loop().create_future(),
            )
            self._waiter = waiter
            return waiter

    async def publish_open(
        self, waiter: PlanQuestionWaiter, question: PlanQuestionContent
    ) -> OpenPlanQuestion:
        async with self._lock:
            self._require_waiter(waiter)
            opened = OpenPlanQuestion(
                waiter.interaction_id,
                waiter.origin_turn_id,
                question,
                waiter.waiter_generation,
            )
            # Resolution may commit after OPEN FULL but before the runner
            # promotes its dormant waiter.  Canonical ANSWERED wins; do not
            # synthesize a stale process-local Opened view in that window.
            self._open = None if waiter._future.done() else opened
            return opened

    async def current_open(self) -> OpenPlanQuestion | None:
        async with self._lock:
            return self._open

    async def settle(
        self, *, interaction_id: str, resolution: AcceptedPlanResolution
    ) -> bool:
        async with self._lock:
            waiter = self._waiter
            if waiter is None or waiter.interaction_id != interaction_id:
                return False
            if not waiter._future.done():
                waiter._future.set_result(resolution)
            self._open = None
            return True

    async def wait(
        self, waiter: PlanQuestionWaiter
    ) -> AcceptedPlanResolution:
        # Deliberately no operation timeout: waiting for a human is not a
        # physical provider/tool operation deadline.
        try:
            return await asyncio.shield(waiter._future)
        finally:
            async with self._lock:
                if self._waiter is waiter and waiter._future.done():
                    self._waiter = None
                    self._open = None

    async def abandon(self, waiter: PlanQuestionWaiter, error: BaseException) -> None:
        async with self._lock:
            if self._waiter is not waiter:
                return
            if not waiter._future.done():
                waiter._future.set_exception(error)
            self._waiter = None
            self._open = None

    async def abort_current(self, error: BaseException) -> None:
        """Settle the sole same-Host waiter after canonical interruption."""

        async with self._lock:
            waiter = self._waiter
            self._waiter = None
            self._open = None
            if waiter is not None and not waiter._future.done():
                waiter._future.set_exception(error)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            waiter = self._waiter
            self._waiter = None
            self._open = None
            if waiter is not None and not waiter._future.done():
                waiter._future.set_exception(
                    RuntimeError("Host closed while a Plan question was open")
                )

    def _require_waiter(self, waiter: PlanQuestionWaiter) -> None:
        if self._waiter is not waiter:
            raise RuntimeError("Plan question waiter authority changed")


class ContinuationAdmissionPhase(StrEnum):
    ADMITTING = "ADMITTING"
    TERMINALIZING = "TERMINALIZING"


@dataclass(slots=True)
class ContinuationAdmissionAttempt:
    """Host-owned process-local task; request cancellation only detaches."""

    attempt_id: str
    turn_id: str
    semantic_candidate_fingerprint: str
    task: asyncio.Task[object]
    phase: ContinuationAdmissionPhase = ContinuationAdmissionPhase.ADMITTING


class ContinuationAdmissionOwner:
    def __init__(self) -> None:
        self._attempts: dict[str, ContinuationAdmissionAttempt] = {}
        self._closing = False

    def start(
        self,
        *,
        attempt_id: str,
        turn_id: str,
        semantic_candidate_fingerprint: str,
        run: Callable[[], Awaitable[object]],
        before_start: Callable[[], None] | None = None,
    ) -> ContinuationAdmissionAttempt:
        # Host methods and close all run on the owning event loop.  Installation
        # deliberately has no await point: once a command is admitted, caller
        # cancellation can only detach from the already-owned physical task.
        existing = self._attempts.get(attempt_id)
        if existing is not None:
            if (
                existing.turn_id != turn_id
                or existing.semantic_candidate_fingerprint
                != semantic_candidate_fingerprint
            ):
                raise ConversationKernelConflict(
                    "continuation attempt semantic identity conflicts"
                )
            return existing
        if self._closing:
            raise RuntimeError("continuation admission owner is closing")
        if not semantic_candidate_fingerprint:
            raise ValueError("continuation semantic candidate fingerprint is absent")
        if before_start is not None:
            before_start()
        task = asyncio.create_task(run(), name=f"kernel-plan-continuation:{turn_id}")
        attempt = ContinuationAdmissionAttempt(
            attempt_id,
            turn_id,
            semantic_candidate_fingerprint,
            task,
        )
        self._attempts[attempt_id] = attempt
        task.add_done_callback(
            lambda completed: self._retire(attempt_id, completed)
        )
        return attempt

    def mark_terminalizing(
        self, *, attempt_id: str, task: asyncio.Task[object]
    ) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.task is not task:
            raise RuntimeError("continuation terminalization owner changed")
        attempt.phase = ContinuationAdmissionPhase.TERMINALIZING

    def _retire(self, attempt_id: str, task: asyncio.Task[object]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        current = self._attempts.get(attempt_id)
        if current is not None and current.task is task:
            self._attempts.pop(attempt_id, None)

    async def drain(self) -> None:
        """Join the currently admitted finite operations without closing admission."""

        while self._attempts:
            attempts = tuple(self._attempts.values())
            await asyncio.gather(
                *(attempt.task for attempt in attempts), return_exceptions=True
            )
            # Let done callbacks retire the exact snapshot before deciding
            # whether another already-admitted attempt must also be joined.
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self._closing = True
        attempts = tuple(self._attempts.values())
        # Admission tasks own bounded repository write/confirmation.  Close
        # detaches callers but drains those physical owners; it must not cancel
        # them after a canonical continuation may already be FULL.
        if attempts:
            await asyncio.gather(
                *(attempt.task for attempt in attempts), return_exceptions=True
            )


__all__ = [
    "ContinuationAdmissionAttempt",
    "ContinuationAdmissionOwner",
    "ContinuationAdmissionPhase",
    "KernelPlanInteractionCoordinator",
    "OpenPlanQuestion",
    "PlanQuestionWaiter",
]
