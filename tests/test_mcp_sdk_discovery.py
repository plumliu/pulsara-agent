from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from pulsara_agent.runtime.mcp.sdk import (
    SdkMcpConnectError,
    SdkMcpClientManager,
    SdkMcpConnection,
    _SdkServerConnection,
    discover_mcp_server,
)
from pulsara_agent.runtime.mcp.types import McpServerConfig, McpStdioConfig
from pulsara_agent.runtime.mcp.types import McpDrainError


class _ServerInfo:
    def model_dump(self, **_kwargs):
        return {"name": "fake", "version": "1"}


class _DiscoverySession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered: dict[str, asyncio.Event] = {
            name: asyncio.Event()
            for name in ("tools", "resources", "templates", "prompts")
        }
        self.release = asyncio.Event()
        self.cancelled: set[str] = set()
        self.fail_method: str | None = None

    async def _page(self, name: str, item_attr: str):
        self.calls.append(name)
        self.entered[name].set()
        try:
            if self.fail_method == name:
                await asyncio.sleep(0)
                raise RuntimeError(f"{name} discovery failed")
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.add(name)
            raise
        return SimpleNamespace(**{item_attr: [], "next_cursor": None})

    async def list_tools(self, **_kwargs):
        return await self._page("tools", "tools")

    async def list_resources(self, **_kwargs):
        return await self._page("resources", "resources")

    async def list_resource_templates(self, **_kwargs):
        return await self._page("templates", "resource_templates")

    async def list_prompts(self, **_kwargs):
        return await self._page("prompts", "prompts")


def _connection(
    session: _DiscoverySession,
    *,
    tools: bool = True,
    resources: bool = False,
    prompts: bool = False,
) -> SdkMcpConnection:
    client = SimpleNamespace(
        session=session,
        server_capabilities=SimpleNamespace(
            tools=object() if tools else None,
            resources=object() if resources else None,
            prompts=object() if prompts else None,
        ),
        server_info=_ServerInfo(),
        protocol_version="2026-07-28",
        instructions=None,
    )
    config = McpServerConfig(
        server_id="fake",
        transport=McpStdioConfig(command="fake"),
    )
    return SdkMcpConnection(_SdkServerConnection(config=config, client=client))


async def _discover(connection: SdkMcpConnection, *, timeout_seconds: float = 1.0):
    now = time.monotonic()
    return await discover_mcp_server(
        connection,
        config_epoch=1,
        reconcile_attempt_id="mcp_attempt:test",
        discovery_generation=1,
        queued_at_utc="2026-01-01T00:00:00Z",
        queued_monotonic=now,
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00Z",
        connect_duration_seconds=0,
        discovery_started_at_utc="2026-01-01T00:00:00Z",
        discovery_started_monotonic=now,
        timeout_seconds=timeout_seconds,
    )


def test_discovery_calls_only_declared_capabilities() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True, resources=False, prompts=False)
        session.release.set()
        snapshot, request_count, page_count = await _discover(connection)
        assert session.calls == ["tools"]
        assert snapshot.status.value == "ready"
        assert request_count == page_count == 1

    asyncio.run(run())


def test_discovery_methods_run_concurrently_under_one_deadline() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True, resources=True, prompts=True)
        task = asyncio.create_task(_discover(connection))
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in session.entered.values())),
            timeout=0.5,
        )
        assert not task.done()
        session.release.set()
        snapshot, request_count, page_count = await task
        assert snapshot.status.value == "ready"
        assert set(session.calls) == {"tools", "resources", "templates", "prompts"}
        assert request_count == page_count == 4

    asyncio.run(run())


def test_discovery_failure_cancels_and_drains_sibling_requests() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        session.fail_method = "tools"
        connection = _connection(session, tools=True, resources=True, prompts=True)
        with pytest.raises(ExceptionGroup) as caught:
            await _discover(connection)
        assert any(
            "tools discovery failed" in str(error)
            for error in caught.value.exceptions
        )
        assert {"resources", "templates", "prompts"}.issubset(session.cancelled)

    asyncio.run(run())


def test_discovery_methods_share_one_absolute_deadline() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True, resources=True, prompts=True)
        with pytest.raises(TimeoutError):
            await _discover(connection, timeout_seconds=0.01)
        assert session.cancelled == {"tools", "resources", "templates", "prompts"}

    asyncio.run(run())


def test_sdk_manager_has_no_legacy_start_entrypoint() -> None:
    assert not hasattr(SdkMcpClientManager, "start")


def test_blocked_sdk_owner_close_is_retryable_and_preserves_connection() -> None:
    async def run() -> None:
        release = asyncio.Event()
        owner_task = asyncio.create_task(release.wait())
        config = McpServerConfig(
            server_id="fake",
            transport=McpStdioConfig(command="fake"),
        )
        connection = _SdkServerConnection(
            config=config,
            client=SimpleNamespace(),
            close_requested=asyncio.Event(),
            owner_task=owner_task,
        )
        manager = SdkMcpClientManager(
            _snapshots=(),
            _connections={"fake": connection},
        )

        with pytest.raises(McpDrainError, match="owner task"):
            await manager.aclose(timeout_seconds=0.01)
        assert "fake" in manager._connections
        assert not manager._closed

        release.set()
        await owner_task
        await manager.aclose(timeout_seconds=1)
        assert manager._connections == {}
        assert manager._closed

    asyncio.run(run())


def test_connect_timeout_carries_retryable_sdk_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.mcp.sdk as sdk_module

    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    class CancellationResistantClient:
        _exit_stack = None

        async def __aenter__(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return self

        async def __aexit__(self, *_args):
            exited.set()

    client = CancellationResistantClient()
    monkeypatch.setattr(
        sdk_module,
        "_build_sdk_client",
        lambda _config: (client, None),
    )

    async def run() -> None:
        config = McpServerConfig(
            server_id="fake",
            transport=McpStdioConfig(command="fake"),
        )
        with pytest.raises(SdkMcpConnectError) as caught:
            await SdkMcpConnection.connect(config, timeout_seconds=0.01)
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        connection = caught.value.connection
        assert connection._connection.owner_task is not None
        assert not connection._connection.owner_task.done()

        release.set()
        await connection.aclose(timeout_seconds=1)
        assert connection._closed
        assert connection._connection.owner_task.done()
        assert exited.is_set()

    asyncio.run(run())
