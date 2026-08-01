from __future__ import annotations

import asyncio
import socket
import sys
import time

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from pulsara_agent.primitives.mcp_protocol import (
    McpFinalDiscoverWireReceiptFact,
    McpLegacyInitializeWireReceiptFact,
    McpProtocolBehaviorEra,
)
from pulsara_agent.runtime.mcp.sdk import (
    SdkMcpClientManager,
    SdkMcpConnection,
    _SdkServerConnection,
    _install_stable_negotiation_authority,
    _start_sdk_client_owner,
    discover_mcp_server,
)
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpManagerLease,
    McpServerConfig,
    McpStdioConfig,
    McpStreamableHttpConfig,
)


_SERVER_MODULE = "tests.support.mcp_v2_server"


def _test_binding_lease(snapshot) -> McpManagerLease:
    binding = McpBindingIdentity(
        server_id=snapshot.server_id,
        slot_id=f"mcp_slot:test:{snapshot.server_id}",
        snapshot_id=snapshot.snapshot_id,
        discovery_generation=snapshot.discovery_generation,
    )
    return McpManagerLease(
        lease_id=f"mcp_lease:test:{snapshot.server_id}",
        slot_id=binding.slot_id,
        binding_identity=binding,
    )


async def _connect_discover_manage(config: McpServerConfig):
    started = time.monotonic()
    connection = await SdkMcpConnection.connect(config, timeout_seconds=5)
    try:
        snapshot, _requests, _pages = await discover_mcp_server(
            connection,
            config_epoch=1,
            reconcile_attempt_id="mcp_real_transport:test",
            discovery_generation=1,
            queued_at_utc="2026-07-31T00:00:00Z",
            queued_monotonic=started,
            connect_started_at_utc="2026-07-31T00:00:00Z",
            connect_ended_at_utc="2026-07-31T00:00:00Z",
            connect_duration_seconds=max(0.0, time.monotonic() - started),
            discovery_started_at_utc="2026-07-31T00:00:00Z",
            discovery_started_monotonic=time.monotonic(),
            timeout_seconds=5,
        )
        return SdkMcpClientManager.from_connected_server(
            connection=connection,
            snapshot=snapshot,
        ), snapshot
    except BaseException:
        await connection.aclose(timeout_seconds=5)
        raise


def test_real_sdk_v2_stdio_generation_and_tool_call() -> None:
    async def run() -> None:
        manager, snapshot = await _connect_discover_manage(
            McpServerConfig(
                server_id="real-stdio",
                transport=McpStdioConfig(
                    command=sys.executable,
                    args=("-m", _SERVER_MODULE, "stdio"),
                ),
                connect_timeout_ms=5_000,
                discovery_timeout_ms=5_000,
                tool_timeout_ms=5_000,
            )
        )
        try:
            assert tuple(tool.name for tool in snapshot.tools) == ("echo_region",)
            result = await manager.call_tool(
                _test_binding_lease(snapshot),
                "echo_region",
                {"region": "cn", "payload": "stdio"},
                timeout_ms=5_000,
            )
            assert "cn:stdio" in result.output
            authority = snapshot.authority
            assert authority is not None
            protocol = authority.surface_semantic.protocol_semantic
            binding = manager._connections["real-stdio"].protocol_binding
            assert binding is not None
            receipt = binding.negotiation_wire_receipt
            assert (
                authority.discovery_attribution.negotiation.negotiation_wire_receipt_fingerprint
                == receipt.receipt_fingerprint
            )
            if protocol.behavior_era is (McpProtocolBehaviorEra.STATELESS_PER_REQUEST):
                assert isinstance(receipt, McpFinalDiscoverWireReceiptFact)
            else:
                assert isinstance(receipt, McpLegacyInitializeWireReceiptFact)
        finally:
            await manager.aclose(timeout_seconds=5)

    asyncio.run(run())


def test_real_sdk_v2_handshake_stdio_generation_and_tool_call() -> None:
    async def run() -> None:
        config = McpServerConfig(
            server_id="real-handshake-stdio",
            transport=McpStdioConfig(
                command=sys.executable,
                args=("-m", _SERVER_MODULE, "stdio"),
            ),
            connect_timeout_ms=5_000,
            discovery_timeout_ms=5_000,
            tool_timeout_ms=5_000,
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", _SERVER_MODULE, "stdio"],
        )
        client = Client(
            stdio_client(params),
            mode="legacy",
            cache=None,
            read_timeout_seconds=5,
        )
        close_requested, owner_task = await _start_sdk_client_owner(
            client,
            timeout_seconds=5,
        )
        connection = SdkMcpConnection(
            _SdkServerConnection(
                config=config,
                client=client,
                close_requested=close_requested,
                owner_task=owner_task,
            )
        )
        try:
            await _install_stable_negotiation_authority(connection._connection)
            started = time.monotonic()
            snapshot, _requests, _pages = await discover_mcp_server(
                connection,
                config_epoch=1,
                reconcile_attempt_id="mcp_real_transport:handshake",
                discovery_generation=1,
                queued_at_utc="2026-07-31T00:00:00Z",
                queued_monotonic=started,
                connect_started_at_utc="2026-07-31T00:00:00Z",
                connect_ended_at_utc="2026-07-31T00:00:00Z",
                connect_duration_seconds=0,
                discovery_started_at_utc="2026-07-31T00:00:00Z",
                discovery_started_monotonic=started,
                timeout_seconds=5,
            )
            manager = SdkMcpClientManager.from_connected_server(
                connection=connection,
                snapshot=snapshot,
            )
            try:
                authority = snapshot.authority
                assert authority is not None
                assert authority.surface_semantic.protocol_semantic.behavior_era is (
                    McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL
                )
                binding = manager._connections[config.server_id].protocol_binding
                assert binding is not None
                assert isinstance(
                    binding.negotiation_wire_receipt,
                    McpLegacyInitializeWireReceiptFact,
                )
                result = await manager.call_tool(
                    _test_binding_lease(snapshot),
                    "echo_region",
                    {"region": "cn", "payload": "handshake"},
                    timeout_ms=5_000,
                )
                assert "cn:handshake" in result.output
            finally:
                await manager.aclose(timeout_seconds=5)
        except BaseException:
            if not connection._closed:
                await connection.aclose(timeout_seconds=5)
            raise

    asyncio.run(run())


def test_real_sdk_v2_stateless_http_emits_mcp_param_header() -> None:
    async def run() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _SERVER_MODULE,
            "http",
            "--port",
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    if process.returncode is not None:
                        raise RuntimeError("real MCP HTTP test server exited early")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("real MCP HTTP test server did not start")
                    await asyncio.sleep(0.02)
                    continue
                writer.close()
                await writer.wait_closed()
                break

            manager, snapshot = await _connect_discover_manage(
                McpServerConfig(
                    server_id="real-http",
                    transport=McpStreamableHttpConfig(
                        url=f"http://127.0.0.1:{port}/mcp"
                    ),
                    connect_timeout_ms=5_000,
                    discovery_timeout_ms=5_000,
                    tool_timeout_ms=5_000,
                )
            )
            try:
                authority = snapshot.authority
                assert authority is not None
                assert authority.surface_semantic.protocol_semantic.behavior_era is (
                    McpProtocolBehaviorEra.STATELESS_PER_REQUEST
                )
                binding = manager._connections["real-http"].protocol_binding
                assert binding is not None
                assert isinstance(
                    binding.negotiation_wire_receipt,
                    McpFinalDiscoverWireReceiptFact,
                )
                result = await manager.call_tool(
                    _test_binding_lease(snapshot),
                    "echo_region",
                    {"region": "cn", "payload": "http"},
                    timeout_ms=5_000,
                )
                # The SDK server rejects this call if the listed x-mcp-header
                # argument was not mirrored into Mcp-Param-Region.
                assert "cn:http" in result.output
            finally:
                await manager.aclose(timeout_seconds=5)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()

    asyncio.run(run())
