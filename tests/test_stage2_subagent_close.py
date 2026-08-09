from __future__ import annotations

import asyncio

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager


class _Repository:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []

    def accept_subagent_task(self, *_args, **_kwargs):
        return None

    def set_subagent_task_status(self, *_args, **kwargs):
        self.statuses.append((str(kwargs["status"]), kwargs["reason"]))
        return True


class _BlockingRunner:
    async def run_subagent_turn(self, **_kwargs):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_host_close_interrupts_subagent_instead_of_user_cancelling_it() -> None:
    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
        )
        manager.bind_runner_factory(lambda: _BlockingRunner())  # type: ignore[arg-type]
        result = await manager.invoke(
            tool_name="spawn_agent",
            arguments={"task": "wait forever"},
            parent_turn_id="turn:test",
        )
        assert result.state == "SUCCESS"
        await manager.aclose(timeout_seconds=1)
        assert repository.statuses == [
            ("ACTIVE", None),
            ("INTERRUPTED", "HOST_CLOSING"),
        ]

    asyncio.run(exercise())
