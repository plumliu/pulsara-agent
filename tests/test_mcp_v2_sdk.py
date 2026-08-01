from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import mcp.types as sdk_types
import pytest
from pydantic import TypeAdapter

from pulsara_agent.ports.mcp_secret import build_retryable_tool_call_payload
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.mcp_protocol import (
    McpFinalDiscoverWireReceiptFact,
    McpLegacyInitializeWireReceiptFact,
    McpProtocolBehaviorEra,
)
from pulsara_agent.runtime.mcp.contracts import McpSdkConcurrencyMode
from pulsara_agent.runtime.mcp.sdk import (
    McpUnsupportedResultTypeError,
    SdkMcpClientManager,
    _SdkServerConnection,
    _install_stable_negotiation_authority,
    _mcp_sdk_concurrency_mode,
    _mcp_operation_lane,
)
from pulsara_agent.runtime.mcp.telemetry import (
    inject_mcp_trace_headers_safely,
    mcp_operation_trace_scope,
)
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpManagerLease,
    McpServerConfig,
    McpStdioConfig,
    McpStreamableHttpConfig,
    runtime_mcp_config_fingerprint,
)


class _ModernSession:
    initialize_result = None

    def __init__(self) -> None:
        self.send_discover_calls: list[str] = []
        self.adopted: sdk_types.DiscoverResult | None = None

    async def send_discover(self, revision: str):
        self.send_discover_calls.append(revision)
        return {
            "supportedVersions": [revision],
            "capabilities": {"tools": {"listChanged": True}},
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def adopt(self, result: sdk_types.DiscoverResult) -> None:
        self.adopted = result


def _config() -> McpServerConfig:
    return McpServerConfig(
        server_id="server",
        transport=McpStdioConfig(command="server"),
    )


@pytest.mark.parametrize(
    ("transport", "era", "expected"),
    (
        (
            McpStreamableHttpConfig(url="https://mcp.example.test"),
            McpProtocolBehaviorEra.STATELESS_PER_REQUEST,
            McpSdkConcurrencyMode.BOUNDED_PARALLEL,
        ),
        (
            McpStreamableHttpConfig(url="https://mcp.example.test"),
            McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL,
            McpSdkConcurrencyMode.SERIALIZED,
        ),
        (
            McpStdioConfig(command="mcp-server"),
            McpProtocolBehaviorEra.STATELESS_PER_REQUEST,
            McpSdkConcurrencyMode.SERIALIZED,
        ),
        (
            McpStdioConfig(command="mcp-server"),
            McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL,
            McpSdkConcurrencyMode.SERIALIZED,
        ),
    ),
)
def test_protocol_and_transport_resolve_one_concurrency_mode(
    transport: McpStdioConfig | McpStreamableHttpConfig,
    era: McpProtocolBehaviorEra,
    expected: McpSdkConcurrencyMode,
) -> None:
    connection = SimpleNamespace(
        config=SimpleNamespace(transport=transport),
        protocol_binding=SimpleNamespace(
            protocol_semantic=SimpleNamespace(behavior_era=era),
        ),
    )
    assert _mcp_sdk_concurrency_mode(connection) is expected


def test_final_discover_wire_receipt_is_required_for_modern_generation() -> None:
    async def run() -> None:
        session = _ModernSession()
        client = SimpleNamespace(
            protocol_version="2026-07-28",
            session=session,
            server_capabilities=sdk_types.ServerCapabilities(
                tools=sdk_types.ToolsCapability(list_changed=True)
            ),
            server_info=None,
        )
        connection = _SdkServerConnection(config=_config(), client=client)
        await _install_stable_negotiation_authority(connection)
        assert session.send_discover_calls == ["2026-07-28"]
        assert session.adopted is not None
        assert isinstance(
            connection.protocol_binding.negotiation_wire_receipt,
            McpFinalDiscoverWireReceiptFact,
        )
        assert connection.protocol_binding.protocol_semantic.behavior_era is (
            McpProtocolBehaviorEra.STATELESS_PER_REQUEST
        )

    asyncio.run(run())


def test_legacy_generation_uses_exact_initialize_receipt() -> None:
    async def run() -> None:
        initialize = sdk_types.InitializeResult(
            protocol_version="2025-11-25",
            capabilities=sdk_types.ServerCapabilities(),
            server_info=sdk_types.Implementation(name="server", version="1"),
        )
        session = SimpleNamespace(initialize_result=initialize)
        client = SimpleNamespace(
            protocol_version="2025-11-25",
            session=session,
            server_capabilities=initialize.capabilities,
            server_info=initialize.server_info,
        )
        connection = _SdkServerConnection(config=_config(), client=client)
        await _install_stable_negotiation_authority(connection)
        assert isinstance(
            connection.protocol_binding.negotiation_wire_receipt,
            McpLegacyInitializeWireReceiptFact,
        )
        assert connection.protocol_binding.protocol_semantic.behavior_era is (
            McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL
        )

    asyncio.run(run())


def test_stateless_operation_lane_is_bounded_not_globally_serialized() -> None:
    async def run() -> None:
        from tests.test_mcp_sdk_discovery import (
            _DiscoverySession,
            _connection,
            _discover,
        )

        entered = 0
        maximum = 0
        borrows = []
        release = asyncio.Event()
        session = _DiscoverySession()
        session.release.set()
        pending_connection = _connection(
            session,
            tools=True,
            transport=McpStreamableHttpConfig(
                url="https://mcp.example.test",
            ),
        )
        snapshot, _request_count, _page_count = await _discover(pending_connection)
        manager = SdkMcpClientManager.from_connected_server(
            connection=pending_connection,
            snapshot=snapshot,
        )
        connection = manager._connections[snapshot.server_id]
        connection.stateless_semaphore = asyncio.Semaphore(2)
        connection.freshness_deadline_monotonic = None
        protocol = connection.protocol_binding
        assert protocol is not None
        binding = McpBindingIdentity(
            server_id=snapshot.server_id,
            slot_id="mcp_slot:concurrency",
            snapshot_id=snapshot.snapshot_id,
            discovery_generation=snapshot.discovery_generation,
        )
        lease = McpManagerLease(
            lease_id="mcp_lease:concurrency",
            slot_id=binding.slot_id,
            binding_identity=binding,
        )

        async def operation(index: int) -> None:
            nonlocal entered, maximum
            async with _mcp_operation_lane(
                connection,
                binding_lease=lease,
                operation_id=f"mcp_operation:{index}",
                target_kind="tool",
                target_semantic_fingerprint="sha256:tool",
            ) as borrow:
                assert borrow.active
                borrows.append(borrow)
                entered += 1
                maximum = max(maximum, entered)
                await release.wait()
                entered -= 1

        tasks = [asyncio.create_task(operation(index)) for index in range(3)]
        while entered < 2:
            await asyncio.sleep(0)
        assert maximum == 2
        assert sum(task.done() for task in tasks) == 0
        release.set()
        await asyncio.gather(*tasks)
        assert len(borrows) == 3
        assert all(not borrow.active for borrow in borrows)
        assert all(borrow.snapshot_id == connection.snapshot_id for borrow in borrows)
        assert all(
            borrow.protocol_semantic_fingerprint
            == protocol.protocol_semantic.semantic_fingerprint
            for borrow in borrows
        )
        wrong_binding = McpBindingIdentity(
            server_id="server",
            slot_id="mcp_slot:wrong",
            snapshot_id="mcp_snapshot:wrong",
            discovery_generation=1,
        )
        wrong_lease = McpManagerLease(
            lease_id="mcp_lease:wrong",
            slot_id=wrong_binding.slot_id,
            binding_identity=wrong_binding,
        )
        with pytest.raises(RuntimeError, match="installed binding authority"):
            async with _mcp_operation_lane(
                connection,
                binding_lease=wrong_lease,
                operation_id="mcp_operation:wrong",
                target_kind="tool",
                target_semantic_fingerprint="sha256:tool",
            ):
                pass
        assert connection.admitted_operation_count == 0
        await manager.aclose()

    asyncio.run(run())


def test_stdio_operation_lane_serializes_modern_protocol() -> None:
    async def run() -> None:
        from tests.test_mcp_sdk_discovery import (
            _DiscoverySession,
            _connection,
            _discover,
        )

        session = _DiscoverySession()
        session.release.set()
        pending_connection = _connection(session, tools=True)
        snapshot, _request_count, _page_count = await _discover(pending_connection)
        manager = SdkMcpClientManager.from_connected_server(
            connection=pending_connection,
            snapshot=snapshot,
        )
        connection = manager._connections[snapshot.server_id]
        connection.freshness_deadline_monotonic = None
        binding = McpBindingIdentity(
            server_id=snapshot.server_id,
            slot_id="mcp_slot:stdio-serialized",
            snapshot_id=snapshot.snapshot_id,
            discovery_generation=snapshot.discovery_generation,
        )
        lease = McpManagerLease(
            lease_id="mcp_lease:stdio-serialized",
            slot_id=binding.slot_id,
            binding_identity=binding,
        )
        release = asyncio.Event()
        first_entered = asyncio.Event()
        entered = 0
        maximum = 0

        async def operation(index: int) -> None:
            nonlocal entered, maximum
            async with _mcp_operation_lane(
                connection,
                binding_lease=lease,
                operation_id=f"mcp_operation:stdio:{index}",
                target_kind="tool",
                target_semantic_fingerprint="sha256:tool",
            ):
                entered += 1
                maximum = max(maximum, entered)
                first_entered.set()
                await release.wait()
                entered -= 1

        tasks = tuple(asyncio.create_task(operation(index)) for index in range(2))
        await asyncio.wait_for(first_entered.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert entered == maximum == 1
        release.set()
        await asyncio.gather(*tasks)
        assert maximum == 1
        assert connection.admitted_operation_count == 0
        await manager.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "result", "complete_type"),
    (
        (
            "tools/call",
            sdk_types.CallToolResult(
                content=[],
                result_type="future_async_result",
            ),
            sdk_types.CallToolResult,
        ),
        (
            "resources/read",
            sdk_types.ReadResourceResult(
                contents=[],
                result_type="future_async_result",
            ),
            sdk_types.ReadResourceResult,
        ),
        (
            "prompts/get",
            sdk_types.GetPromptResult(
                messages=[],
                result_type="future_async_result",
            ),
            sdk_types.GetPromptResult,
        ),
    ),
)
def test_unknown_result_type_fails_closed(
    method: str,
    result: object,
    complete_type: type[object],
) -> None:
    async def run() -> None:
        from tests.test_mcp_sdk_discovery import (
            _DiscoverySession,
            _connection,
            _discover,
        )

        session = _DiscoverySession()
        session.release.set()
        pending_connection = _connection(session, tools=True)
        snapshot, _request_count, _page_count = await _discover(pending_connection)
        manager = SdkMcpClientManager.from_connected_server(
            connection=pending_connection,
            snapshot=snapshot,
        )
        connection = manager._connections[snapshot.server_id]
        connection.freshness_deadline_monotonic = None

        async def send_request(*_args, **_kwargs):
            return result

        session.send_request = send_request  # type: ignore[attr-defined]
        binding = McpBindingIdentity(
            server_id=snapshot.server_id,
            slot_id="mcp_slot:unknown-result",
            snapshot_id=snapshot.snapshot_id,
            discovery_generation=snapshot.discovery_generation,
        )
        lease = McpManagerLease(
            lease_id="mcp_lease:unknown-result",
            slot_id=binding.slot_id,
            binding_identity=binding,
        )
        adapter = TypeAdapter(complete_type | sdk_types.InputRequiredResult)
        retryable = build_retryable_tool_call_payload(
            tool_name="lookup",
            arguments={},
            source_method_schema_fingerprint=context_fingerprint(
                "test-mcp-unknown-result-schema:v1",
                method,
            ),
        )
        with pytest.raises(
            McpUnsupportedResultTypeError,
            match="MCP_UNSUPPORTED_RESULT_TYPE",
        ):
            await manager._drive_input_required_request(
                connection=connection,
                binding_lease=lease,
                request_factory=lambda _responses, _state: object(),
                result_adapter=adapter,
                complete_result_type=complete_type,
                retryable_payload=retryable,
                timeout_ms=1000,
                input_responses=None,
                request_state=None,
                leg_ordinal=1,
                interaction_id=None,
                trace_method=method,
                target_kind="test",
                target_semantic_fingerprint="sha256:test-target",
            )
        await manager.aclose()

    asyncio.run(run())


def test_static_http_config_cannot_override_protocol_or_trace_headers() -> None:
    for header in (
        "Mcp-Protocol-Version",
        "Mcp-Param-region",
        "traceparent",
        "baggage",
    ):
        with pytest.raises(ValueError, match="protocol-managed"):
            McpStreamableHttpConfig(
                url="https://mcp.example.test/api",
                headers={header: "attacker-controlled"},
            )
    with pytest.raises(ValueError, match="second Authorization owner"):
        McpStreamableHttpConfig(
            url="https://mcp.example.test/api",
            bearer_token_env_var="MCP_TOKEN",
            headers={"Authorization": "Bearer duplicate"},
        )


def test_runtime_config_uses_keyed_secret_commitments(monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "alpha-low-entropy-token")
    config = McpServerConfig(
        server_id="server",
        transport=McpStreamableHttpConfig(
            url="https://mcp.example.test/api",
            bearer_token_env_var="MCP_TEST_TOKEN",
        ),
    )
    first = runtime_mcp_config_fingerprint(config)
    monkeypatch.setenv("MCP_TEST_TOKEN", "beta-low-entropy-token")
    second = runtime_mcp_config_fingerprint(config)
    assert first.startswith("sha256:")
    assert second.startswith("sha256:")
    assert first != second


def test_live_transport_config_repr_is_secret_safe() -> None:
    stdio = McpStdioConfig(
        command="server",
        args=("--token", "stdio-secret-canary"),
        env={"TOKEN": "stdio-env-secret-canary"},
    )
    http = McpStreamableHttpConfig(
        url="https://mcp.example.test/api?token=url-secret-canary",
        headers={"Authorization": "Bearer header-secret-canary"},
        env_headers={"X-Api-Key": "MCP_API_KEY"},
    )
    rendered = repr((stdio, http))
    for secret in (
        "stdio-secret-canary",
        "stdio-env-secret-canary",
        "url-secret-canary",
        "header-secret-canary",
    ):
        assert secret not in rendered


def test_w3c_trace_hook_is_process_local_and_failure_isolated(monkeypatch) -> None:
    async def run() -> None:
        request = SimpleNamespace(headers={})
        with mcp_operation_trace_scope(server_id="docs", method="tools/call"):
            await inject_mcp_trace_headers_safely(request)
        assert re.fullmatch(
            r"00-[0-9a-f]{32}-[0-9a-f]{16}-01",
            request.headers["traceparent"],
        )
        assert "pulsara.mcp.method=tools/call" in request.headers["baggage"]
        assert "pulsara.mcp.server=docs" in request.headers["baggage"]

        import pulsara_agent.runtime.mcp.telemetry as telemetry

        def broken_exporter():
            raise RuntimeError("telemetry exporter unavailable")

        monkeypatch.setattr(telemetry, "current_mcp_trace_headers", broken_exporter)
        failed_request = SimpleNamespace(headers={})
        await inject_mcp_trace_headers_safely(failed_request)
        assert failed_request.headers == {}

    asyncio.run(run())
