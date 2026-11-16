"""Archiving must not stop a child whose physical execution is settling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.session_deletion import KernelSessionRetirement
from pulsara_agent.identity import random_id
from tests.support.model_config import test_model_runtime as model_runtime


def test_archive_gates_child_work_before_closing_hosts():
    async def scenario():
        core = KernelHostCore(model_runtime=model_runtime())
        root_gate, child_gate = object(), object()
        root_id, child_id = random_id("session"), random_id("session")
        writer = object()
        child_running = asyncio.Event()
        child_running.set()

        async def prepare_child():
            if child_running.is_set():
                raise RuntimeError("HostSession is not quiescent for runtime reopen")
            return child_gate

        root = SimpleNamespace(
            session_id=root_id, root_session_id=None,
            _lease=SimpleNamespace(guard=writer),
            prepare_safe_runtime_reopen=AsyncMock(return_value=root_gate),
            archive_has_terminal_work=lambda: False,
            abort_safe_runtime_reopen=AsyncMock(), commit_safe_runtime_reopen=AsyncMock(),
        )
        child = SimpleNamespace(
            session_id=child_id, root_session_id=root_id,
            _lease=SimpleNamespace(guard=object()),
            prepare_safe_runtime_reopen=AsyncMock(side_effect=prepare_child),
            archive_has_terminal_work=lambda: False,
            abort_safe_runtime_reopen=AsyncMock(), commit_safe_runtime_reopen=AsyncMock(),
        )
        repo = SimpleNamespace(archive_session=Mock(return_value="OPEN"))
        core._sessions = {random_id("host"): root, random_id("host"): child}
        core._ensure_resources = AsyncMock(return_value=repo)
        operation = KernelSessionRetirement(root_id, "u_local", core, asyncio.get_running_loop().create_future())
        core._session_retirements = {root_id: operation}

        with pytest.raises(RuntimeError, match="not quiescent"):
            await core.prepare_session_archive(operation)
        root.abort_safe_runtime_reopen.assert_awaited_once_with(root_gate)
        root.commit_safe_runtime_reopen.assert_not_awaited()
        repo.archive_session.assert_not_called()

        child_running.clear()
        await core.prepare_session_archive(operation)
        root.commit_safe_runtime_reopen.assert_awaited_once_with(root_gate)
        child.commit_safe_runtime_reopen.assert_awaited_once_with(child_gate)
        assert repo.archive_session.call_args.kwargs["closed_writer"] is writer
        assert repo.archive_session.call_args.kwargs["child_writers"] == (child._lease.guard,)

    asyncio.run(scenario())
