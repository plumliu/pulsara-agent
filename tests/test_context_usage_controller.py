from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.web_app.session_controller import (
    LocalSessionController,
    _Quarantined,
)


def test_context_usage_get_reads_only_live_handles(monkeypatch) -> None:
    async def scenario() -> None:
        controller = LocalSessionController(
            core=cast(
                KernelHostCore,
                SimpleNamespace(mcp_management=SimpleNamespace(lane=asyncio.Lock())),
            ),
            permission_policy=cast(EffectivePermissionPolicy, object()),
            active_skill_names=frozenset(),
        )
        resume = AsyncMock(
            side_effect=AssertionError("a metric must not start session activation")
        )
        monkeypatch.setattr(controller, "resume_session", resume)
        assert await controller.read_context_usage("cold") == {
            "state": "unavailable",
            "connection_id": None,
        }
        assert not controller._by_session and not controller._operations

        value = {"state": "ready", "connection_id": "model-a", "input_tokens": 20}
        read = AsyncMock(return_value=value)
        handle = SimpleNamespace(session=SimpleNamespace(read_context_usage=read))
        controller._by_session["live"] = handle
        assert await controller.read_context_usage("live") == value
        read.assert_awaited_once()

        controller._operations["live"] = _Quarantined("SESSION_CLOSE_QUARANTINED")
        assert await controller.read_context_usage("live") == {
            "state": "updating",
            "connection_id": None,
        }
        read.assert_awaited_once()
        controller._operations.clear()

        async def retiring_read():
            # The controller lock must be released before entering the Host.
            async with controller._lock:
                controller._by_session.pop("live")
            return value

        read.side_effect = retiring_read
        assert await controller.read_context_usage("live") == {
            "state": "unavailable",
            "connection_id": None,
        }
        controller._closing = True
        assert await controller.read_context_usage("cold") == {
            "state": "unavailable",
            "connection_id": None,
        }
        resume.assert_not_awaited()

    asyncio.run(scenario())
