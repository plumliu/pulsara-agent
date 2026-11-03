"""Exit facts wait for actual local subprocesses in admitted filesystem workers."""
from __future__ import annotations

import asyncio
import os
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO, KernelSessionIOClosed
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.ripgrep import private_ripgrep
from pulsara_agent.tools.builtins.filesystem_search import _Diagnostics, _Producer
from tests.support.model_config import scheduled_task_service


def test_io_admission_fence_retains_original_deadline_and_exact_accepted_result():
    async def run():
        lane = KernelSessionIO(maximum_concurrency=2)
        entered, release = Event(), Event()
        deadline = monotonic() + 10
        calls = []
        def accepted(*, deadline_monotonic):
            calls.append(deadline_monotonic)
            entered.set()
            assert release.wait(10), "test did not release admitted physical work"
            return "committed result"
        caller = asyncio.create_task(lane.run(accepted, deadline_monotonic=deadline))
        assert await asyncio.to_thread(entered.wait, 5)
        lane.stop_admission()
        assert lane.physical_operation_deadline == deadline
        assert not lane.physical_close_complete
        try:
            with pytest.raises(KernelSessionIOClosed):
                await lane.run(accepted, deadline_monotonic=deadline)
            assert calls == [deadline]
        finally:
            release.set()
        assert await caller == "committed result"
        assert lane.physical_operation_deadline is None
        assert lane.physical_close_complete
        await lane.aclose(deadline_monotonic=deadline)
    asyncio.run(run())


def test_tool_exit_cannot_drop_real_rg_child_owned_by_physical_worker(tmp_path):
    async def run():
        scheduler = scheduled_task_service()
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id=random_id("host"),
            session_id=random_id("session"), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(), scheduled_tasks=scheduler)
        fifo = tmp_path / "search-input"
        os.mkfifo(fifo)
        entered, release = Event(), Event()
        process = None
        deadline = monotonic() + 10
        def search_worker(*, deadline_monotonic):
            nonlocal process
            with _Producer([str(private_ripgrep()), "--text", "needle", str(fifo)],
                    cwd=tmp_path, call=ToolCall(random_id("call"), "search_content"),
                    diagnostics=_Diagnostics(False), deadline=deadline_monotonic) as producer:
                process = producer.process
                entered.set()
                assert release.wait(10), "test did not release real rg owner"
            return "search stopped and joined"
        caller = asyncio.create_task(port._physical_io.run(search_worker, deadline_monotonic=deadline))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert process is not None and process.poll() is None
            port.stop_local_process_admission()
            await port.stop_terminal_physical_owners(timeout_seconds=5)
            assert port._terminal_physically_closed
            assert port._visualization_screenshots.local_children_stopped
            assert not port.local_children_stopped
            assert port._physical_io.physical_operation_deadline == deadline
            with pytest.raises(KernelSessionIOClosed):
                await port._physical_io.run(search_worker, deadline_monotonic=deadline)
            assert process.poll() is None
        finally:
            release.set()
            assert await caller == "search stopped and joined"
            await port.aclose(timeout_seconds=5)
            await scheduler._core.shutdown()
        assert process.returncode is not None
        assert port.local_children_stopped
        assert port.physical_close_complete
    asyncio.run(run())
