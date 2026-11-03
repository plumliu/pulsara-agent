"""Background MCP startup preserves stable discovery admission and ownership."""

import asyncio
import sys

from pulsara_agent.conversation_kernel.mcp import supervisor as module
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round6_mcp_production import _FakeMcpClient


def test_discovery_reserves_config_order_before_cold_sdk_preparation(tmp_path, monkeypatch):
    async def run():
        preparing, release = asyncio.Event(), asyncio.Event()
        preparations, opening = [], []

        class Client(_FakeMcpClient):
            async def open(self):
                opening.append(self.config.server_id)

        async def prepare():
            preparations.append(asyncio.current_task().get_name())
            if len(preparations) == 1:
                preparing.set()
                await release.wait()
            return Client

        monkeypatch.setattr(module, "_prepare_sdk_client_factory", prepare)
        configs = tuple(_parse_server(server_id, {
            "transport": {"type": "stdio", "command": sys.executable},
        }) for server_id in ("z_last", "a_first"))
        owner = module.McpHostSupervisor(
            workspace_root=tmp_path, configs=configs,
            credential_boundary=ProcessCredentialBoundary(),
        )
        runtime = None
        waiter = None
        try:
            await asyncio.wait_for(owner.start(), 1)
            await asyncio.wait_for(preparing.wait(), 1)
            waiter = asyncio.create_task(owner.wait_initial_readiness())
            await asyncio.sleep(0)
            assert preparations == ["mcp-connect:a_first:1"]
            assert opening == [] and not waiter.done()
            assert owner.readiness_progress()['waiting'] is True
            release.set()
            await asyncio.wait_for(waiter, 1)
            assert opening == ["a_first", "z_last"]
            runtime = owner.install_pending_at_safe_point()
            assert tuple(item.server_id for item in runtime.root_tool_specs) == ("a_first", "z_last")
        finally:
            release.set()
            if waiter is not None:
                await waiter
            if runtime is not None:
                runtime.release()
            await owner.aclose()
        assert owner._discovery_reservations._value == 1
        assert not owner._connection_tasks

    asyncio.run(run())


def test_cancel_readiness_does_not_cancel_cold_sdk_preparation(tmp_path, monkeypatch):
    async def run():
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            preparing.set()
            await release.wait()
            return _FakeMcpClient

        monkeypatch.setattr(module, "_prepare_sdk_client_factory", prepare)
        owner = module.McpHostSupervisor(
            workspace_root=tmp_path,
            configs=(_parse_server("fixture", {
                "transport": {"type": "stdio", "command": sys.executable},
            }),), credential_boundary=ProcessCredentialBoundary(),
        )
        try:
            await asyncio.wait_for(owner.start(), 1)
            await asyncio.wait_for(preparing.wait(), 1)
            waiter = asyncio.create_task(owner.wait_initial_readiness())
            await asyncio.sleep(0)
            waiter.cancel()
            result = await asyncio.gather(waiter, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert not owner._tasks["fixture"].done()
            assert owner.readiness_progress() is None
            release.set()
            await asyncio.wait_for(owner.wait_initial_readiness(), 1)
            assert owner._tasks["fixture"].result().state is module.McpServerState.READY
        finally:
            release.set()
            await owner.aclose()
        assert owner._discovery_reservations._value == 1
        assert not owner._connection_tasks

    asyncio.run(run())
