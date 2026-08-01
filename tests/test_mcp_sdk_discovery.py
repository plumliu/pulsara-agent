from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import mcp.types as sdk_types
import pytest

from pulsara_agent.primitives.mcp_protocol import McpCacheableMethod
from pulsara_agent.runtime.mcp.contracts import (
    McpSdkNegotiatedProtocolBinding,
    McpSdkProtocolBinding,
)
from pulsara_agent.runtime.mcp.sdk import (
    McpSnapshotReconcileRequired,
    SdkMcpConnectError,
    SdkMcpClientManager,
    SdkMcpConnection,
    _SdkServerConnection,
    _install_stable_negotiation_authority,
    _mcp_operation_lane,
    discover_mcp_server,
)
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpDrainError,
    McpManagerLease,
    McpServerConfig,
    McpStdioConfig,
    McpStreamableHttpConfig,
)


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
        self.initialize_result = None
        self.adopted: sdk_types.DiscoverResult | None = None

    async def send_discover(self, revision: str):
        return {
            "supportedVersions": [revision],
            "capabilities": {},
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def adopt(self, result: sdk_types.DiscoverResult) -> None:
        self.adopted = result

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
        return SimpleNamespace(
            **{
                item_attr: [],
                "next_cursor": None,
                "ttl_ms": 0,
                "cache_scope": "private",
            }
        )

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
    transport: McpStdioConfig | McpStreamableHttpConfig | None = None,
) -> SdkMcpConnection:
    capabilities = sdk_types.ServerCapabilities(
        tools=(sdk_types.ToolsCapability() if tools else None),
        resources=(sdk_types.ResourcesCapability() if resources else None),
        prompts=(sdk_types.PromptsCapability() if prompts else None),
    )
    client = SimpleNamespace(
        session=session,
        server_capabilities=capabilities,
        server_info=_ServerInfo(),
        protocol_version="2026-07-28",
        instructions=None,
    )
    config = McpServerConfig(
        server_id="fake",
        transport=transport or McpStdioConfig(command="fake"),
    )
    return SdkMcpConnection(_SdkServerConnection(config=config, client=client))


async def _discover(connection: SdkMcpConnection, *, timeout_seconds: float = 1.0):
    await _install_stable_negotiation_authority(connection._connection)
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
        generation = connection._connection.client_generation
        binding = connection._connection.protocol_binding
        assert generation is not None
        assert generation.accepting_operations
        assert binding is generation.sdk_protocol_binding
        assert (
            binding.complete_listing_accumulator
            == generation.complete_tool_listing_accumulator
        )

    asyncio.run(run())


def test_negotiated_binding_cannot_install_before_complete_listing() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True)

        await _install_stable_negotiation_authority(connection._connection)

        binding = connection._connection.protocol_binding
        assert isinstance(binding, McpSdkNegotiatedProtocolBinding)
        assert not isinstance(binding, McpSdkProtocolBinding)
        assert connection._connection.client_generation is None

        with pytest.raises(RuntimeError, match="complete client generation"):
            SdkMcpClientManager.from_connected_server(
                connection=connection,
                snapshot=SimpleNamespace(authority=None),
            )

    asyncio.run(run())


def test_stateless_listing_rejects_wire_missing_cache_hints() -> None:
    async def run() -> None:
        manager = SdkMcpClientManager(_snapshots=(), _connections={})

        async def fetch(_cursor):
            return sdk_types.ListToolsResult(tools=[])

        with pytest.raises(RuntimeError, match="omitted cache hints"):
            await manager._list_all(
                "tools/list",
                McpCacheableMethod.TOOLS_LIST,
                fetch,
                [],
                item_attr="tools",
                metrics={"request_count": 0, "page_count": 0},
                strict_cache_hints=True,
            )

    asyncio.run(run())


def test_listing_rejects_mixed_cache_scope_across_pages() -> None:
    async def run() -> None:
        manager = SdkMcpClientManager(_snapshots=(), _connections={})
        pages = {
            None: sdk_types.ListToolsResult(
                tools=[],
                next_cursor="page:2",
                ttl_ms=1000,
                cache_scope="public",
            ),
            "page:2": sdk_types.ListToolsResult(
                tools=[],
                next_cursor=None,
                ttl_ms=1000,
                cache_scope="private",
            ),
        }

        async def fetch(cursor):
            return pages[cursor]

        with pytest.raises(RuntimeError, match="mixed cache scopes"):
            await manager._list_all(
                "tools/list",
                McpCacheableMethod.TOOLS_LIST,
                fetch,
                [],
                item_attr="tools",
                metrics={"request_count": 0, "page_count": 0},
                strict_cache_hints=True,
            )

    asyncio.run(run())


def test_zero_ttl_is_synchronously_revalidated_before_one_dispatch() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True)
        session.release.set()
        snapshot, _request_count, _page_count = await _discover(connection)
        manager = SdkMcpClientManager.from_connected_server(
            connection=connection,
            snapshot=snapshot,
        )
        raw = manager._connections["fake"]
        generation = raw.client_generation
        assert generation is not None
        assert snapshot.authority is not None
        binding = McpBindingIdentity(
            server_id="fake",
            slot_id="mcp_slot:ttl-test",
            snapshot_id=snapshot.snapshot_id,
            discovery_generation=snapshot.discovery_generation,
        )
        lease = McpManagerLease(
            lease_id="mcp_lease:ttl-test",
            slot_id=binding.slot_id,
            binding_identity=binding,
        )

        permit = await manager._prepare_dispatch_freshness(
            raw,
            timeout_seconds=1,
            dispatch_operation_id="mcp_operation:ttl-permitted",
        )
        later_permit = await manager._prepare_dispatch_freshness(
            raw,
            timeout_seconds=1,
            dispatch_operation_id="mcp_operation:ttl-later",
        )

        assert permit is not None
        assert later_permit is not None
        assert permit.freshness_generation < later_permit.freshness_generation
        receipt = permit.receipt
        assert receipt.installed_snapshot_id == snapshot.snapshot_id
        assert (
            receipt.installed_snapshot_authority_fingerprint
            == generation.snapshot_authority_fingerprint
        )
        assert receipt.refreshed_snapshot_id == snapshot.snapshot_id
        assert session.calls == ["tools", "tools", "tools"]
        async with _mcp_operation_lane(
            raw,
            binding_lease=lease,
            operation_id="mcp_operation:ttl-permitted",
            target_kind="tool",
            target_semantic_fingerprint="sha256:tool",
            freshness_permit=permit,
        ) as borrow:
            assert (
                borrow.snapshot_authority_fingerprint
                == generation.snapshot_authority_fingerprint
            )
            assert (
                borrow.freshness_revalidation_receipt_fingerprint
                == receipt.receipt_fingerprint
            )
        async with _mcp_operation_lane(
            raw,
            binding_lease=lease,
            operation_id="mcp_operation:ttl-later",
            target_kind="tool",
            target_semantic_fingerprint="sha256:tool",
            freshness_permit=later_permit,
        ) as later_borrow:
            assert (
                later_borrow.freshness_revalidation_receipt_fingerprint
                == later_permit.receipt.receipt_fingerprint
            )
        assert manager.snapshots[0] is snapshot
        assert (
            manager.snapshots[0].authority.authority_fingerprint
            == generation.snapshot_authority_fingerprint
        )
        with pytest.raises(RuntimeError, match="already consumed"):
            async with _mcp_operation_lane(
                raw,
                binding_lease=lease,
                operation_id="mcp_operation:ttl-reused",
                target_kind="tool",
                target_semantic_fingerprint="sha256:tool",
                freshness_permit=permit,
            ):
                pass
        with pytest.raises(McpSnapshotReconcileRequired):
            async with _mcp_operation_lane(
                raw,
                binding_lease=lease,
                operation_id="mcp_operation:ttl-blocked",
                target_kind="tool",
                target_semantic_fingerprint="sha256:tool",
            ):
                pass

    asyncio.run(run())


def test_discovery_methods_run_concurrently_under_one_deadline() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(
            session,
            tools=True,
            resources=True,
            prompts=True,
            transport=McpStreamableHttpConfig(url="https://mcp.example.test"),
        )
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


def test_stdio_discovery_methods_are_physically_serialized() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(session, tools=True, resources=True, prompts=True)
        task = asyncio.create_task(_discover(connection))

        await asyncio.wait_for(session.entered["tools"].wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert session.calls == ["tools"]
        assert not any(
            session.entered[name].is_set()
            for name in ("resources", "templates", "prompts")
        )

        session.release.set()
        snapshot, request_count, page_count = await task
        assert snapshot.status.value == "ready"
        assert session.calls == ["tools", "resources", "templates", "prompts"]
        assert request_count == page_count == 4

    asyncio.run(run())


def test_discovery_failure_cancels_and_drains_sibling_requests() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        session.fail_method = "tools"
        connection = _connection(
            session,
            tools=True,
            resources=True,
            prompts=True,
            transport=McpStreamableHttpConfig(url="https://mcp.example.test"),
        )
        with pytest.raises(ExceptionGroup) as caught:
            await _discover(connection)
        assert any(
            "tools discovery failed" in str(error) for error in caught.value.exceptions
        )
        assert {"resources", "templates", "prompts"}.issubset(session.cancelled)

    asyncio.run(run())


def test_discovery_methods_share_one_absolute_deadline() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        connection = _connection(
            session,
            tools=True,
            resources=True,
            prompts=True,
            transport=McpStreamableHttpConfig(url="https://mcp.example.test"),
        )
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
        lambda _config, *, client_input_binding=None: (client, None),
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
