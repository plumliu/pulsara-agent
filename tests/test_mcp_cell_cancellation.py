import asyncio

import pytest

from pulsara_agent.conversation_kernel.mcp.supervisor import (
    _McpDispatchCancelledBeforeStart,
    _acquire_dispatch_lane,
)


def test_cell_stop_racing_lane_grant_returns_permit():
    async def run():
        lane = asyncio.Semaphore(1)
        await lane.acquire()
        stopped = asyncio.Event()
        waiter = asyncio.create_task(_acquire_dispatch_lane(lane, stopped))
        await asyncio.sleep(0)
        lane.release()
        stopped.set()
        with pytest.raises(_McpDispatchCancelledBeforeStart):
            await asyncio.wait_for(waiter, 1)
        await asyncio.wait_for(lane.acquire(), 1)
        assert lane.locked()
        lane.release()

    asyncio.run(run())
