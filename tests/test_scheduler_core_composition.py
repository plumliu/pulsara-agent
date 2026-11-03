"""Kernel scheduling and desktop recovery share the canonical open owner."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore, KernelHostCoreClosing, SessionControlRejected
from pulsara_agent.identity import random_id
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime


def workspace(tmp_path):
    return HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path, memory_domain_id="u_local")


def test_core_owns_scheduler_before_any_resources_or_ui_exist():
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime(), memory_domain_id="other-user")
        try:
            assert isinstance(core.scheduled_tasks, ScheduledTaskService)
            assert core.scheduled_tasks._core is core
            assert core.memory_domain_id == "other-user"
            assert core.scheduled_tasks._task is None
            assert not core.has_database_resources
            await core.start_scheduled_tasks()
            assert core.scheduled_tasks._task is None
        finally:
            await core.shutdown()
    asyncio.run(run())


def test_shared_resume_waiter_cancellation_keeps_one_physical_open(tmp_path):
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        entered, release = asyncio.Event(), asyncio.Event()
        result = object()
        async def open_owner(*args, **kwargs):
            entered.set()
            await release.wait()
            return result
        core._open_admitted = AsyncMock(side_effect=open_owner)
        session_id = random_id("session")
        first = asyncio.create_task(core.resume_session(session_id, workspace_input=workspace(tmp_path)))
        await entered.wait()
        second = asyncio.create_task(core.resume_session(session_id, workspace_input=workspace(tmp_path)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(core._open_attempts) == 1
        assert core._open_admitted.await_count == 1
        release.set()
        assert await second is result
        assert not core._open_attempts
        await core.shutdown()
    asyncio.run(run())


def test_root_open_inherits_core_configuration_and_preserves_explicit_empty_skills(tmp_path):
    async def run():
        from pulsara_agent.primitives.permission import PermissionMode
        from pulsara_agent.tool_permission import preset_to_policy
        policy = preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        core = KernelHostCore.production(model_runtime=test_model_runtime(),
            permission_policy=policy, active_skill_names=frozenset({"review"}))
        core._open_admitted = AsyncMock(return_value=object())
        await core.open_session(workspace(tmp_path))
        assert core._open_admitted.call_args.kwargs["permission_policy"] is policy
        assert core._open_admitted.call_args.kwargs["active_skill_names"] == frozenset({"review"})
        await core.open_session(workspace(tmp_path), active_skill_names=frozenset())
        assert core._open_admitted.call_args.kwargs["active_skill_names"] == frozenset()
        await core.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("conflict", ["domain", "workspace", "permission", "skills", "system"])
def test_pending_resume_never_joins_a_conflicting_owner_configuration(tmp_path, conflict):
    async def run():
        from dataclasses import replace
        from pulsara_agent.primitives.permission import PermissionMode
        from pulsara_agent.tool_permission import preset_to_policy
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        entered, release = asyncio.Event(), asyncio.Event()
        async def open_owner(*args, **kwargs):
            entered.set()
            await release.wait()
            return object()
        core._open_admitted = AsyncMock(side_effect=open_owner)
        session_id = random_id("session")
        request = workspace(tmp_path)
        first = asyncio.create_task(core.resume_session(session_id, workspace_input=request))
        await entered.wait()
        options = {}
        if conflict == "domain":
            request = replace(request, memory_domain_id="different-user")
        elif conflict == "workspace":
            request = replace(request, workspace_root=tmp_path / "other")
        elif conflict == "permission":
            options["permission_policy"] = preset_to_policy(PermissionMode.READ_ONLY)
        elif conflict == "skills":
            options["active_skill_names"] = frozenset({"other"})
        else:
            options["system_prompt"] = "A different system."
        with pytest.raises(ValueError, match="configuration conflicts"):
            await core.resume_session(session_id, workspace_input=request, **options)
        assert core._open_admitted.await_count == 1
        release.set()
        await first
        await core.shutdown()
    asyncio.run(run())


def test_ui_lifecycle_fence_blocks_scheduler_and_management_until_released(tmp_path):
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        session_id = random_id("session")
        token = await core.reserve_session_operation(session_id)
        result = object()
        core._open_admitted = AsyncMock(return_value=result)
        try:
            with pytest.raises(SessionControlRejected, match="会话正在"):
                await core.resume_session(session_id, workspace_input=workspace(tmp_path), scheduled=True)
            with pytest.raises(SessionControlRejected):
                await core.scheduled_management_host(session_id)
            assert core._open_admitted.await_count == 0
            assert await core.resume_session(session_id, workspace_input=workspace(tmp_path), operation=token) is result
            with pytest.raises(SessionControlRejected):
                await core.scheduled_management_host(session_id)
        finally:
            await core.release_session_operation(token)
        assert await core.scheduled_management_host(session_id) is None
        await core.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("close_fails", [False, True])
def test_lost_writer_recovery_joins_real_close_and_retains_failed_owner(tmp_path, close_fails):
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        entered, release = asyncio.Event(), asyncio.Event()
        session_id, host_id = random_id("session"), random_id("host")
        old = SimpleNamespace(session_id=session_id, host_session_id=host_id,
            root_session_id=None, writer_unavailable=True, _closing=False, _closed=False,
            _lease=SimpleNamespace(guard=object()), _stop_close_admission=AsyncMock(),
            physical_close_complete=False, necessary_settlement_complete=True,
            local_children_stopped=True)
        async def close(**kwargs):
            entered.set()
            await release.wait()
            if close_fails:
                raise OSError("physical worker has not exited")
            old._closed = True
            old.physical_close_complete = True
        old.aclose = AsyncMock(side_effect=close)
        core._sessions[host_id] = old
        core._repository = SimpleNamespace(release_host_writer=lambda *args, **kwargs: None)
        replacement = object()
        core._open_admitted = AsyncMock(return_value=replacement)
        first = asyncio.create_task(core.resume_session(session_id, workspace_input=workspace(tmp_path)))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert old.aclose.await_count == 1
        assert core._open_admitted.await_count == 0
        with pytest.raises(SessionControlRejected):
            await core.resume_session(session_id, workspace_input=workspace(tmp_path), scheduled=True)
        release.set()
        attempts = [attempt.task for attempt in core._open_attempts.values() if attempt.task is not None]
        assert len(attempts) == 1
        if close_fails:
            with pytest.raises(SessionControlRejected) as rejected:
                await attempts[0]
            assert rejected.value.public_code == "SESSION_RECOVERY_CLOSE_FAILED"
            with pytest.raises(SessionControlRejected):
                await core.resume_session(session_id, workspace_input=workspace(tmp_path))
            assert core._sessions[host_id] is old
            assert old.aclose.await_count == 1
            assert core._open_admitted.await_count == 0
        else:
            assert await attempts[0] is replacement
            assert old.aclose.await_count == 1
            assert core._open_admitted.await_count == 1
        if close_fails:
            with pytest.raises(Exception):
                await core.shutdown()
            assert core._sessions[host_id] is old
        else:
            await core.shutdown()
    asyncio.run(run())


def test_shutdown_wins_while_recovery_waits_for_old_physical_close(tmp_path):
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        entered, release = asyncio.Event(), asyncio.Event()
        session_id, host_id = random_id("session"), random_id("host")
        async def close(**kwargs):
            entered.set()
            await release.wait()
        old = SimpleNamespace(session_id=session_id, host_session_id=host_id,
            root_session_id=None, writer_unavailable=True, _closing=False, _closed=False,
            _lease=SimpleNamespace(guard=object()), aclose=AsyncMock(side_effect=close),
            _stop_close_admission=AsyncMock(), physical_close_complete=True,
            necessary_settlement_complete=True, local_children_stopped=True)
        core._sessions[host_id] = old
        core._repository = SimpleNamespace(release_host_writer=lambda *args, **kwargs: None)
        core._open_admitted = AsyncMock()
        shutdown_admitted = asyncio.Event()
        original_shutdown = core._shutdown_owner
        async def shutdown_owner(*args):
            shutdown_admitted.set()
            return await original_shutdown(*args)
        core._shutdown_owner = shutdown_owner
        resume = asyncio.create_task(core.resume_session(session_id, workspace_input=workspace(tmp_path)))
        await entered.wait()
        shutdown = asyncio.create_task(core.shutdown())
        await shutdown_admitted.wait()
        release.set()
        with pytest.raises(KernelHostCoreClosing):
            await resume
        await shutdown
        assert core._open_admitted.await_count == 0
        assert old.aclose.await_count == 1
        with pytest.raises(KernelHostCoreClosing):
            await core.resume_session(session_id, workspace_input=workspace(tmp_path))
    asyncio.run(run())
