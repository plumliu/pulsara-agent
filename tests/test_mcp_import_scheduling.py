"""Background MCP startup preserves stable discovery admission and ownership."""

import asyncio
from tests.support.mcp import finish_connection_attempts
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
            waiter = asyncio.create_task(finish_connection_attempts(owner))
            await asyncio.sleep(0)
            assert preparations == ["mcp-connect:a_first:1"]
            assert opening == [] and not waiter.done()
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
