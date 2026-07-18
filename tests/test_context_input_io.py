from __future__ import annotations

import asyncio
import threading
from time import monotonic

import pytest

from pulsara_agent.event import EventContext, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime.context_input.event_slice import (
    EventLogContextEventSliceReader,
)
from pulsara_agent.runtime.context_input.io_service import (
    ContextInputIoDeadlineExceeded,
    ContextInputIoService,
)
from tests.conftest import run_start_permission_fields


def test_context_input_io_timeout_retains_physical_owner_until_worker_exit() -> None:
    async def scenario() -> None:
        service = ContextInputIoService(max_pending=1, max_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def blocking() -> str:
            entered.set()
            release.wait()
            return "done"

        operation = asyncio.create_task(
            service.execute(
                operation_name="blocking-probe",
                operation=blocking,
                deadline_monotonic=monotonic() + 0.05,
            )
        )
        await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(ContextInputIoDeadlineExceeded):
            await operation
        assert service.pending_count() == 1
        release.set()
        await service.drain_pending(deadline_monotonic=monotonic() + 1)
        assert service.pending_count() == 0
        service.close_if_idle()

    asyncio.run(scenario())


def test_owned_context_input_io_handle_survives_waiter_cancellation() -> None:
    async def scenario() -> None:
        service = ContextInputIoService(max_pending=1, max_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def blocking() -> str:
            entered.set()
            release.wait()
            return "physically-complete"

        handle = await service.start_owned(
            operation_name="owned-blocking-probe",
            operation=blocking,
            deadline_monotonic=monotonic() + 1,
        )
        await asyncio.to_thread(entered.wait, 1)
        waiter = asyncio.create_task(handle.wait_physical_completion())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert service.pending_count() == 1
        assert not handle.physically_complete
        release.set()
        assert await handle.wait_physical_completion() == "physically-complete"
        await service.drain_pending(deadline_monotonic=monotonic() + 1)
        assert service.pending_count() == 0
        service.close_if_idle()

    asyncio.run(scenario())


def test_event_slice_reader_uses_session_owned_deadline_aware_io() -> None:
    class RecordingLog(InMemoryEventLog):
        observed_deadline: float | None = None

        def read_raw_range_snapshot(
            self,
            *,
            minimum_sequence: int,
            through_sequence: int | None = None,
            deadline_monotonic: float | None = None,
        ):
            self.observed_deadline = deadline_monotonic
            return super().read_raw_range_snapshot(
                minimum_sequence=minimum_sequence,
                through_sequence=through_sequence,
                deadline_monotonic=deadline_monotonic,
            )

    async def scenario() -> None:
        log = RecordingLog()
        ctx = EventContext(
            run_id="run:io-reader",
            turn_id="turn:io-reader",
            reply_id="reply:io-reader",
        )
        log.append(
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(
                    ctx.run_id,
                    user_input="read",
                    turn_id=ctx.turn_id,
                    reply_id=ctx.reply_id,
                ),
                user_input_chars=4,
            )
        )
        service = ContextInputIoService()
        reader = EventLogContextEventSliceReader(
            event_log=log,
            runtime_session_id="runtime:test",
            io_service=service,
        )
        result = await reader.read_through_current_high_water(
            runtime_session_id="runtime:test",
            minimum_sequence=1,
        )
        assert result.through_sequence == 1
        assert log.observed_deadline is not None
        await service.drain_pending(deadline_monotonic=monotonic() + 1)
        service.close_if_idle()

    asyncio.run(scenario())
