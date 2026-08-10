from __future__ import annotations

import asyncio
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO


def _blocking_operation(started: Event, release: Event, *, deadline_monotonic: float):
    assert monotonic() < deadline_monotonic
    started.set()
    release.wait(timeout=2)
    return "done"


def test_session_io_keeps_event_loop_live_and_close_joins_physical_operation() -> None:
    async def exercise() -> None:
        owner = KernelSessionIO(maximum_concurrency=1)
        started = Event()
        release = Event()
        operation = asyncio.create_task(
            owner.run(
                _blocking_operation,
                started,
                release,
                deadline_monotonic=monotonic() + 2,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        # This sleep must execute while the physical call is blocked.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        with pytest.raises(TimeoutError, match="physical I/O"):
            await owner.aclose(deadline_monotonic=monotonic() + 0.01)
        release.set()
        assert await operation == "done"
        await owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(exercise())


def test_session_io_post_admission_deadline_joins_the_physical_thread() -> None:
    async def exercise() -> None:
        owner = KernelSessionIO(maximum_concurrency=1)
        started = Event()
        release = Event()

        def operation(*, deadline_monotonic: float) -> str:
            del deadline_monotonic
            started.set()
            release.wait(timeout=2)
            return "done"

        # Hold the owner lock after semaphore admission so the logical
        # deadline expires immediately after the physical task is installed.
        await owner._lock.acquire()
        caller = asyncio.create_task(
            owner.run(operation, deadline_monotonic=monotonic() + 0.01)
        )
        await asyncio.sleep(0.02)
        owner._lock.release()
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not caller.done()
        assert len(owner._active) == 1
        release.set()
        with pytest.raises(TimeoutError, match="physical I/O"):
            await caller
        assert owner._active == set()
        await owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(exercise())
