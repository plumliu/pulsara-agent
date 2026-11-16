"""Archive eligibility reflects live work throughout a session tree."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from tests.support.model_config import test_model_runtime as model_runtime


def test_archive_projection_checks_live_children_and_recovers_when_they_settle():
    async def run():
        root_id, child_id = random_id("session"), random_id("session")
        root = SimpleNamespace(session_id=root_id, root_session_id=None,
                               inspect_archive_idle=AsyncMock(return_value=True))
        child = SimpleNamespace(session_id=child_id, root_session_id=root_id,
                                inspect_archive_idle=AsyncMock(return_value=False))
        repository = SimpleNamespace(idle_session_ids=Mock(return_value=frozenset({root_id, child_id})))
        core = KernelHostCore(model_runtime=model_runtime())
        core._sessions = {random_id("host"): root, random_id("host"): child}
        core._ensure_resources = AsyncMock(return_value=repository)

        assert await core.idle_session_ids("u_local") == frozenset()
        child.inspect_archive_idle.return_value = True
        assert await core.idle_session_ids("u_local") == frozenset({root_id, child_id})
        assert repository.idle_session_ids.call_count == 2

    asyncio.run(run())
