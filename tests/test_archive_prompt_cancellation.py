"""A cancelled direct prompt must release its admission after the turn ends."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pulsara_agent.conversation_kernel.host_prompts import _HostPromptOperations
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode


def test_cancel_after_turn_completion_drains_prompt_admission():
    async def scenario():
        started, finish, settling = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Host(_HostPromptOperations):
            def __init__(self):
                self._lock = asyncio.Lock()
                self._prompt_admission_attempts = {}
                self.session_id = random_id("session")
                self.workspace_gate = SimpleNamespace(require_available=Mock())
                self._image_validator = SimpleNamespace(freeze=AsyncMock(side_effect=lambda value, **_: value))
                self._launch_permission_mode = PermissionMode.READ_ONLY
                self._query_command_row = AsyncMock(return_value=None)
                self._compaction = SimpleNamespace(is_fenced=Mock(return_value=False))
                self._session_scope_kind = "ROOT"
                self._plan_exit_fence = False
                self._external_new_turn_accepting = False
                self._active_task = None
                self._manual_compaction_command_attempts = {}
                self._io = SimpleNamespace(run=AsyncMock(return_value=object()))
                self.repository = SimpleNamespace(prepare_root_permission_snapshot=Mock())
                self._lease = SimpleNamespace(guard=object())
                snapshot = SimpleNamespace(reconcile=Mock(return_value=(object(), None, False)))
                self._model_runtime = SimpleNamespace(freeze_resolution_snapshot=Mock(return_value=snapshot))
                self.model_call_binding = AsyncMock(return_value=object())
                self._dispatch_user_prompt_hook = AsyncMock(return_value=(None, None))

            def _canonical_deadline(self):
                return 1

            def _require_open(self):
                pass

            def _retire_done_active_root_locked(self):
                pass

            def _install_active_root_task_locked(self, **kwargs):
                async def run():
                    started.set()
                    await finish.wait()
                    return object()
                return asyncio.create_task(run())

            async def _settle_prompt_admission_attempt(self, *args, **kwargs):
                settling.set()
                return await super()._settle_prompt_admission_attempt(*args, **kwargs)

        host = Host()
        task = asyncio.create_task(host.run_turn(PromptContent.text("hello")))
        await started.wait()
        await host._lock.acquire()
        finish.set()
        await settling.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        host._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host._prompt_admission_attempts

    asyncio.run(scenario())
