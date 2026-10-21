"""Cold SDK preparation retains required-first discovery scheduling."""

import asyncio
import sys

from pulsara_agent.conversation_kernel.mcp import supervisor as module
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round6_mcp_production import _FakeMcpClient


def test_required_discovery_reserves_before_cold_sdk_preparation(tmp_path, monkeypatch):
    async def run():
        preparing, release = asyncio.Event(), asyncio.Event()
        optional_opening, release_optional = asyncio.Event(), asyncio.Event()
        preparations, opening = [], []

        class Client(_FakeMcpClient):
            async def open(self):
                opening.append(self.config.server_id)
                if not self.config.required:
                    optional_opening.set()
                    await release_optional.wait()

        async def prepare():
            preparations.append(asyncio.current_task().get_name())
            if len(preparations) == 1:
                preparing.set()
                await release.wait()
            return Client

        monkeypatch.setattr(module, "_prepare_sdk_client_factory", prepare)
        configs = tuple(_parse_server(server_id, {
            "required": required,
            "transport": {"type": "stdio", "command": sys.executable},
        }) for server_id, required in (("a_optional", False), ("z_required", True)))
        owner = module.McpHostSupervisor(
            session_id="session:cold-priority", workspace_root=tmp_path,
            configs=configs, credential_boundary=ProcessCredentialBoundary(),
        )
        start = asyncio.create_task(owner.start())
        runtime = None
        try:
            await asyncio.wait_for(preparing.wait(), 1)
            await asyncio.sleep(0)
            assert preparations == ["mcp-connect:z_required:1"]
            assert opening == []
            assert not start.done()
            release.set()
            await asyncio.wait_for(start, 1)
            await asyncio.wait_for(optional_opening.wait(), 1)
            assert opening == ["z_required", "a_optional"]
            runtime = owner.install_pending_at_safe_point()
            assert tuple(item.server_id for item in runtime.root_tool_specs) == ("z_required",)
            assert not owner._tasks["a_optional"].done()
        finally:
            release.set()
            release_optional.set()
            if runtime is not None:
                runtime.release()
            await start
            await owner.aclose()
        assert owner._discovery_reservations._value == 1
        assert not owner._connection_tasks

    asyncio.run(run())


def test_optional_only_start_does_not_wait_for_cold_sdk_preparation(tmp_path, monkeypatch):
    async def run():
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            preparing.set()
            await release.wait()
            return _FakeMcpClient

        monkeypatch.setattr(module, "_prepare_sdk_client_factory", prepare)
        owner = module.McpHostSupervisor(
            session_id="session:optional-cold", workspace_root=tmp_path,
            configs=(_parse_server("optional", {
                "transport": {"type": "stdio", "command": sys.executable},
            }),), credential_boundary=ProcessCredentialBoundary(),
        )
        try:
            await asyncio.wait_for(owner.start(), 1)
            await asyncio.wait_for(preparing.wait(), 1)
            assert not owner._tasks["optional"].done()
        finally:
            release.set()
            await owner.aclose()
        assert owner._discovery_reservations._value == 1
        assert not owner._connection_tasks

    asyncio.run(run())
