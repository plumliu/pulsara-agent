"""A generic child open participates in root retirement admission."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.host_contracts import SessionControlRejected
from pulsara_agent.conversation_kernel.session_deletion import SessionDeleteRejected
from pulsara_agent.identity import random_id
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime as model_runtime


def test_child_open_cannot_pass_root_retirement_during_root_lookup():
    async def run():
        root_id, child_id = random_id("session"), random_id("session")
        loop = asyncio.get_running_loop()
        read_started = asyncio.Event()
        release_read = threading.Event()

        def read_state(**kwargs):
            loop.call_soon_threadsafe(read_started.set)
            assert release_read.wait(timeout=5)
            return {"root_session_id": root_id}

        core = KernelHostCore(model_runtime=model_runtime())
        core._ensure_resources = AsyncMock(return_value=SimpleNamespace(
            read_agent_runtime_state=read_state))
        core._open_admitted = AsyncMock()
        opening = asyncio.create_task(core.resume_session(
            child_id, workspace_input=HostWorkspaceInput("project", "/tmp")))
        try:
            await read_started.wait()
            retirement = await core.begin_session_retirement(root_id, "u_local")
            release_read.set()
            with pytest.raises(SessionDeleteRejected):
                await opening
            core._open_admitted.assert_not_awaited()
            await core.finish_session_retirement(retirement, quarantine=False)
        finally:
            release_read.set()
            await asyncio.gather(opening, return_exceptions=True)

    asyncio.run(run())


def test_root_retirement_joins_already_registered_child_open():
    async def run():
        root_id, child_id = random_id("session"), random_id("session")
        opening_started = asyncio.Event()
        release_open = asyncio.Event()
        result = object()

        async def open_admitted(*args, **kwargs):
            opening_started.set()
            await release_open.wait()
            return result

        core = KernelHostCore(model_runtime=model_runtime())
        repository = SimpleNamespace(read_agent_runtime_state=Mock(
            return_value={"root_session_id": root_id}))
        core._ensure_resources = AsyncMock(return_value=repository)
        core._open_admitted = AsyncMock(side_effect=open_admitted)
        opening = asyncio.create_task(core.resume_session(
            child_id, workspace_input=HostWorkspaceInput("project", "/tmp")))
        retirement_task = None
        try:
            await opening_started.wait()
            assert any(root_id in attempt.session_ids for attempt in core._open_attempts.values())
            retirement_task = asyncio.create_task(core.begin_session_retirement(root_id, "u_local"))
            await asyncio.sleep(0)
            assert root_id in core._session_retirements
            assert not retirement_task.done()
            release_open.set()
            assert await opening is result
            retirement = await retirement_task
            await core.finish_session_retirement(retirement, quarantine=False)
        finally:
            release_open.set()
            await asyncio.gather(opening, return_exceptions=True)
            if retirement_task is not None:
                await asyncio.gather(retirement_task, return_exceptions=True)

    asyncio.run(run())


def test_root_open_never_joins_child_and_same_child_open_still_joins():
    async def run():
        root_id, child_id = random_id("session"), random_id("session")
        child_started = asyncio.Event()
        root_started = asyncio.Event()
        release_open = asyncio.Event()
        root_result, child_result = object(), object()

        def read_state(*, session_id, **kwargs):
            return {"root_session_id": root_id if session_id == child_id else None}

        async def open_admitted(*args, session_id, **kwargs):
            (child_started if session_id == child_id else root_started).set()
            await release_open.wait()
            return child_result if session_id == child_id else root_result

        core = KernelHostCore(model_runtime=model_runtime())
        core._ensure_resources = AsyncMock(return_value=SimpleNamespace(
            read_agent_runtime_state=read_state))
        core._open_admitted = AsyncMock(side_effect=open_admitted)
        workspace = HostWorkspaceInput("project", "/tmp")
        first_child = asyncio.create_task(core.resume_session(child_id, workspace_input=workspace))
        other_child = root = None
        try:
            await child_started.wait()
            other_child = asyncio.create_task(core.resume_session(child_id, workspace_input=workspace))
            root = asyncio.create_task(core.resume_session(root_id, workspace_input=workspace))
            await asyncio.wait_for(root_started.wait(), 2)
            assert core._open_admitted.await_count == 2
            release_open.set()
            assert await first_child is child_result
            assert await other_child is child_result
            assert await root is root_result
            assert core._open_admitted.await_count == 2
        finally:
            release_open.set()
            await asyncio.gather(*(task for task in (first_child, other_child, root)
                                   if task is not None), return_exceptions=True)

    asyncio.run(run())


def test_child_open_does_not_block_root_management_or_scheduled_resume():
    async def run():
        root_id, child_id = random_id("session"), random_id("session")
        child_started, release_child = asyncio.Event(), asyncio.Event()
        root_host = SimpleNamespace(session_id=root_id, _memory_domain_id="u_local",
                                    writer_unavailable=False)

        async def open_admitted(*args, **kwargs):
            child_started.set()
            await release_child.wait()
            return object()

        core = KernelHostCore(model_runtime=model_runtime())
        core._ensure_resources = AsyncMock(return_value=SimpleNamespace(
            read_agent_runtime_state=Mock(return_value={"root_session_id": root_id})))
        core._sessions = {random_id("host"): root_host}
        core._open_admitted = AsyncMock(side_effect=open_admitted)
        opening = asyncio.create_task(core.resume_session(
            child_id, workspace_input=HostWorkspaceInput("project", "/tmp")))
        try:
            await child_started.wait()
            assert await core.scheduled_management_host(root_id) is root_host
            assert await core.resume_scheduled_session(root_id) is root_host
            reservation = await core.reserve_session_operation(root_id)
            with pytest.raises(SessionControlRejected) as rejected:
                await core.scheduled_management_host(root_id)
            assert rejected.value.public_code == "SESSION_CONTROL_BUSY"
            await core.release_session_operation(reservation)
        finally:
            release_child.set()
            await asyncio.gather(opening, return_exceptions=True)

    asyncio.run(run())
