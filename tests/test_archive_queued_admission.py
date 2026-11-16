"""Queued delivery must respect a gate installed while preparation awaits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pulsara_agent.conversation_kernel.host_prompts import _HostPromptOperations
from pulsara_agent.identity import random_id


def test_queued_delivery_rechecks_archive_gate_before_installing():
    async def run():
        hook_started = asyncio.Event()
        release_hook = asyncio.Event()
        gated_loop = asyncio.Event()
        installed = asyncio.Event()
        workspace_checks = 0
        candidate = SimpleNamespace(model_call_binding=object(), exact_turn_id=random_id("turn"))

        async def workspace_wait():
            nonlocal workspace_checks
            workspace_checks += 1
            if workspace_checks == 2:
                gated_loop.set()

        async def prepare_hook(_candidate):
            hook_started.set()
            await release_hook.wait()
            return True

        async def run_io(*args, **kwargs):
            return candidate

        def install(**kwargs):
            installed.set()
            return asyncio.create_task(asyncio.sleep(0))

        host = SimpleNamespace(
            _queue_wake=asyncio.Event(), _closing=False,
            workspace_gate=SimpleNamespace(wait_available=workspace_wait),
            _lock=asyncio.Lock(), _retire_done_active_root_locked=Mock(),
            _active_task=None, _external_new_turn_accepting=False,
            _plan_exit_fence=False, _runtime_reopen_quiescence=None,
            _compaction=SimpleNamespace(is_fenced=Mock(return_value=False),
                                        has_active_work=Mock(return_value=False)),
            _session_scope_kind="ROOT", _manual_compaction_command_attempts={},
            _io=SimpleNamespace(run=run_io),
            repository=SimpleNamespace(prepare_prompt_head_consumption=Mock()),
            session_id=random_id("session"), host_session_id=random_id("host"), _canonical_deadline=Mock(return_value=1),
            root_session_id=None,
            _model_runtime=SimpleNamespace(freeze_resolution_snapshot=Mock(
                return_value=SimpleNamespace(validate=Mock()))),
            _prepare_queued_prompt_hook=prepare_hook,
            _install_active_root_task_locked=install,
            _run_queued_root_admission=AsyncMock(),
        )
        host._queue_wake.set()
        loop = asyncio.create_task(_HostPromptOperations._prompt_delivery_loop(host))
        try:
            await asyncio.wait_for(hook_started.wait(), 2)
            host._runtime_reopen_quiescence = object()
            release_hook.set()
            await asyncio.wait_for(gated_loop.wait(), 2)
            assert not installed.is_set()
            host._runtime_reopen_quiescence = None
            host._queue_wake.set()
            await asyncio.wait_for(installed.wait(), 2)
        finally:
            host._closing = True
            loop.cancel()
            outcome = await asyncio.gather(loop, return_exceptions=True)
            if isinstance(outcome[0], Exception):
                raise outcome[0]

    asyncio.run(run())
