"""Session-owned bounded physical I/O lane for the conversation kernel.

Repository and blob adapters intentionally remain synchronous.  This owner is
the only foreground seam allowed to execute those physical operations from an
async Host activation.  Cancellation remains attached until the admitted
physical operation exits; Host close also drains every admitted task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import Generic, TypeVar

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    normalize_observation_duration,
)


T = TypeVar("T")


class KernelSessionIOClosed(RuntimeError):
    pass


class PhysicalToolInvocationDisposition(StrEnum):
    RETURNED_EXACT = "RETURNED_EXACT"
    RAISED = "RAISED"


class PhysicalToolInvocationTiming(StrEnum):
    ON_TIME = "ON_TIME"
    LATE_AFTER_WATCHDOG = "LATE_AFTER_WATCHDOG"


@dataclass(frozen=True, slots=True)
class PhysicalToolInvocationOutcome(Generic[T]):
    """Exact terminal outcome of one process-local physical worker.

    This carrier is never serialized.  It lets the semantic owner preserve a
    value returned after a logical watchdog or caller cancellation instead of
    mistaking the logical waiter state for the physical effect outcome.
    """

    disposition: PhysicalToolInvocationDisposition
    timing: PhysicalToolInvocationTiming
    caller_cancelled: bool
    value: T | None = None
    error: BaseException | None = None
    observation: PhysicalToolObservationSupplement | None = None

    def __post_init__(self) -> None:
        if (self.disposition is PhysicalToolInvocationDisposition.RETURNED_EXACT) != (
            self.error is None
        ):
            raise ValueError("physical tool outcome value/error union is invalid")


class KernelSessionIO:
    def __init__(
        self,
        *,
        maximum_concurrency: int = STAGE2_LIMITS.foreground_io_hard_concurrency,
    ) -> None:
        if not 1 <= maximum_concurrency <= STAGE2_LIMITS.foreground_io_hard_concurrency:
            raise ValueError("foreground I/O concurrency is outside its hard bound")
        self._semaphore = asyncio.Semaphore(maximum_concurrency)
        self._active: set[asyncio.Task[object]] = set()
        self._closing = False
        self._lock = asyncio.Lock()

    async def run(
        self,
        operation: Callable[..., T],
        /,
        *args: object,
        deadline_monotonic: float,
        **kwargs: object,
    ) -> T:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("foreground I/O deadline expired before admission")
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except TimeoutError as exc:
            raise TimeoutError("foreground I/O admission deadline expired") from exc
        task: asyncio.Task[object] | None = None
        try:
            async with self._lock:
                if self._closing:
                    raise KernelSessionIOClosed("foreground I/O owner is closing")
                task = asyncio.create_task(
                    asyncio.to_thread(
                        partial(
                            operation,
                            *args,
                            deadline_monotonic=deadline_monotonic,
                            **kwargs,
                        )
                    ),
                    name=f"kernel-physical-io:{getattr(operation, '__name__', 'operation')}",
                )
                self._active.add(task)
                task.add_done_callback(self._retire)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                # Admission has already created the physical thread.  A
                # logical deadline may detach no mutation owner: wait for the
                # exact task to exit before reporting the timeout, just like
                # the timed wait branch below.
                await _drain_physical_task(task)
                raise TimeoutError(
                    "foreground physical I/O exited after its deadline"
                )
            try:
                return await asyncio.wait_for(  # type: ignore[return-value]
                    asyncio.shield(task), timeout=remaining
                )
            except TimeoutError as exc:
                # The logical deadline cannot terminate a worker thread.  The
                # caller may report a deadline only after the physical owner
                # has exited; close can meanwhile observe this task and block.
                await _drain_physical_task(task)
                raise TimeoutError(
                    "foreground physical I/O exited after its deadline"
                ) from exc
            except asyncio.CancelledError as cancellation:
                # asyncio cannot cancel a worker thread.  Do not let a caller
                # observe cancellation while the exact physical operation can
                # still mutate resources behind the next settlement/close.
                await _drain_physical_task(task)
                if not task.cancelled():
                    try:
                        task.result()
                    except BaseException:
                        # The operation's terminal error is retrieved, but it
                        # cannot erase the explicit cancellation signal.  The
                        # canonical caller owns ACK/outcome confirmation.
                        pass
                raise cancellation
        except BaseException:
            if task is None:
                self._semaphore.release()
            raise

    async def run_tool_invocation(
        self,
        operation: Callable[..., T],
        /,
        *args: object,
        deadline_monotonic: float,
        **kwargs: object,
    ) -> PhysicalToolInvocationOutcome[T]:
        """Run one physical tool call and never discard its exact terminal state."""

        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            return PhysicalToolInvocationOutcome(
                disposition=PhysicalToolInvocationDisposition.RAISED,
                timing=PhysicalToolInvocationTiming.ON_TIME,
                caller_cancelled=False,
                error=TimeoutError("tool invocation deadline expired before admission"),
            )
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except TimeoutError as exc:
            return PhysicalToolInvocationOutcome(
                disposition=PhysicalToolInvocationDisposition.RAISED,
                timing=PhysicalToolInvocationTiming.ON_TIME,
                caller_cancelled=False,
                error=exc,
            )
        task: asyncio.Task[object] | None = None
        physical_started: float | None = None
        try:
            async with self._lock:
                if self._closing:
                    raise KernelSessionIOClosed("foreground I/O owner is closing")
                physical_started = monotonic()
                task = asyncio.create_task(
                    asyncio.to_thread(
                        partial(
                            operation,
                            *args,
                            deadline_monotonic=deadline_monotonic,
                            **kwargs,
                        )
                    ),
                    name=(
                        "kernel-physical-tool:"
                        f"{getattr(operation, '__name__', 'operation')}"
                    ),
                )
                self._active.add(task)
                task.add_done_callback(self._retire)
            timed_out = False
            caller_cancelled = False
            remaining = deadline_monotonic - monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except TimeoutError:
                    timed_out = True
                except asyncio.CancelledError:
                    caller_cancelled = True
            else:
                timed_out = True
            if not task.done():
                await _drain_physical_task(task)
            timing = (
                PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG
                if timed_out or caller_cancelled
                else PhysicalToolInvocationTiming.ON_TIME
            )
            assert physical_started is not None
            observation = PhysicalToolObservationSupplement(
                observed_at=datetime.now(timezone.utc),
                elapsed_microseconds=normalize_observation_duration(
                    max(0, int((monotonic() - physical_started) * 1_000_000))
                ),
                # The exact sealed binding refines this placeholder before the
                # candidate crosses the canonical repository boundary.
                observation_origin_kind=ToolObservationOrigin.CUSTOM_OR_UNKNOWN,
            )
            try:
                value = task.result()
            except BaseException as exc:
                return PhysicalToolInvocationOutcome(
                    disposition=PhysicalToolInvocationDisposition.RAISED,
                    timing=timing,
                    caller_cancelled=caller_cancelled,
                    error=exc,
                    observation=observation,
                )
            return PhysicalToolInvocationOutcome(
                disposition=PhysicalToolInvocationDisposition.RETURNED_EXACT,
                timing=timing,
                caller_cancelled=caller_cancelled,
                value=value,  # type: ignore[arg-type]
                observation=observation,
            )
        except BaseException:
            if task is None:
                self._semaphore.release()
            raise

    def _retire(self, task: asyncio.Task[object]) -> None:
        self._active.discard(task)
        self._semaphore.release()
        # Retrieve errors from callers that detached on cancellation/deadline.
        if not task.cancelled():
            task.exception()

    async def aclose(self, *, deadline_monotonic: float) -> None:
        async with self._lock:
            self._closing = True
        deadline_expired = False
        while True:
            active = tuple(self._active)
            if not active:
                break
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                deadline_expired = True
                done, pending = (), active
            else:
                done, pending = await asyncio.wait(active, timeout=remaining)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                deadline_expired = True
                # The close watchdog is a logical force/failure boundary.  It
                # cannot make a worker thread disappear, so keep the exact
                # close owner alive until every admitted task is physically
                # terminal before exposing the typed timeout.
                for task in pending:
                    await _drain_physical_task(task)
        if deadline_expired:
            raise TimeoutError("foreground physical I/O exited after close deadline")


async def _drain_physical_task(task: asyncio.Task[object]) -> None:
    """Join one admitted thread even if its logical waiter is cancelled again."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation only detaches the logical waiter after the
            # physical operation has reached a terminal state.
            continue
        except BaseException:
            # The task is terminal; the caller decides whether the physical
            # exception or its logical timeout/cancellation is authoritative.
            break


__all__ = [
    "KernelSessionIO",
    "KernelSessionIOClosed",
    "PhysicalToolInvocationDisposition",
    "PhysicalToolInvocationOutcome",
    "PhysicalToolInvocationTiming",
]
