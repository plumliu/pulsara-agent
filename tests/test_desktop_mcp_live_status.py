"""Device MCP status follows real physical owners, not frozen provider semantics."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
import mcp_types as types
import pytest

from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService, LocalMcpTarget, config_guard, config_to_entry,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor, McpSnapshotStale
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.desktop_app import session_controller as controller_module
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.settings import LocalSettingsStore
from tests.test_mcp_catalog_subscriptions import _sdk_http
from tests.test_mcp_scheduling_isolation import _invoke, _until


def _session(supervisor):
    # Only unrelated Skill/Postgres construction is omitted. The production
    # Session -> tool port -> supervisor inspection chain runs unchanged.
    port = object.__new__(DirectKernelToolPort)
    port._mcp_supervisor = supervisor
    session = object.__new__(KernelHostSession)
    session._writer_renewal_failure = None
    session._closing = False
    session._tools = port
    session._capabilities = SimpleNamespace(
        configured_active_skill_names=frozenset(),
        freeze_owner_snapshot=lambda **_: SimpleNamespace(inspection=None),
    )
    return session


def _controller(service, monkeypatch):
    monkeypatch.setattr(controller_module, "_inspect_user_skills", lambda: {})
    monkeypatch.setattr(controller_module, "_user_plugins_payload", lambda *_: {})
    controller = object.__new__(LocalSessionController)
    controller._lock = asyncio.Lock()
    controller._operations = {}
    controller._by_session = {}
    controller.core = SimpleNamespace(
        mcp_management=service, inspect_user_plugins=AsyncMock(return_value=object()),
    )
    return controller


async def _read(controller):
    payload = await controller.inspect_user_capabilities()
    return payload["mcp"]["servers"][0]


def _assert_unavailable(row, status):
    assert row["status"] == status
    assert row["tools"] == []
    assert row["tool_count"] == row["resource_count"] == row["prompt_count"] == 0


def test_failed_reconnect_is_not_ready_and_other_working_owner_still_wins(tmp_path, monkeypatch):
    async def scenario():
        blocked = False
        listing_started = asyncio.Event()
        release_listing = asyncio.Event()

        async def tools(_ctx, _params):
            if blocked:
                listing_started.set()
                await release_listing.wait()
            return types.ListToolsResult(tools=[types.Tool(
                name="probe", input_schema={"type": "object"},
            )])

        async def call(_ctx, _params):
            return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])

        server = Server("live-status", on_list_tools=tools, on_call_tool=call)
        async with _sdk_http(server) as endpoint:
            service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
                                                user_config_path=tmp_path / "mcp.yaml")
            await service.create(LocalMcpTarget("catalog"), {
                "required": True, "effect_policy": {"default_effect": "READ_ONLY"},
                "transport": {"type": "streamable_http", "endpoint": endpoint,
                              "allow_http_localhost": True},
            })
            supervisors = [McpHostSupervisor(
                session_id="session:scheduling", workspace_root=tmp_path,
                configs=service.load_configs(), credential_boundary=service.credential_boundary,
                connect_attempt_timeout_seconds=0.25,
            ) for _ in range(2)]
            controller = _controller(service, monkeypatch)
            runtimes = []
            try:
                for supervisor in supervisors:
                    await supervisor.start()
                    runtimes.append(supervisor.install_pending_at_safe_point())
                first, second = supervisors
                controller._by_session["first"] = SimpleNamespace(session=_session(first))
                assert (await _read(controller))["status"] == "READY"
                semantic_before = first.catalog_snapshot().semantic_fingerprint
                tools_before = runtimes[0].root_tool_specs
                blocked = True
                first.reconnect("catalog")
                _assert_unavailable(await _read(controller), "CONNECTING")
                await asyncio.wait_for(listing_started.wait(), 3)
                await asyncio.wait_for(first._tasks["catalog"], 3)
                row = await _read(controller)
                _assert_unavailable(row, "FAILED_RETRYABLE")
                assert row["has_failure"] and row["failure_category"] == "TimeoutError"
                assert first.catalog_snapshot().servers[0].status is McpServerState.READY
                assert first.catalog_snapshot().semantic_fingerprint == semantic_before
                assert runtimes[0].root_tool_specs == tools_before
                with pytest.raises(McpSnapshotStale):
                    _invoke(next(iter(runtimes[0].executors.values())), "call:failed-rebind")

                # Another exact config owner that is actually callable wins,
                # independently of conversation selection or insertion order.
                controller._by_session["second"] = SimpleNamespace(session=_session(second))
                row = await _read(controller)
                assert row["status"] == "READY" and row["tool_count"] == len(row["tools"]) == 1
                assert not row["has_failure"]
                controller._by_session = dict(reversed(tuple(controller._by_session.items())))
                assert (await _read(controller)) == row
                assert (await _invoke(next(iter(runtimes[1].executors.values())), "call:healthy")).state == "SUCCESS"
                controller._operations["second"] = object()
                _assert_unavailable(await _read(controller), "FAILED_RETRYABLE")

                blocked = False
                release_listing.set()
                first.reconnect("catalog")
                await asyncio.wait_for(first._tasks["catalog"], 3)
                row = await _read(controller)
                assert row["status"] == "READY" and row["tool_count"] == len(row["tools"]) == 1
                assert not row["has_failure"]
                assert first.catalog_snapshot().semantic_fingerprint == semantic_before
                replacement = first.install_pending_at_safe_point()
                assert replacement is not None
                runtimes.append(replacement)
                assert (await _invoke(next(iter(replacement.executors.values())), "call:restored")).state == "SUCCESS"
                first.stop_admission()
                _assert_unavailable(await _read(controller), "RETIRING")
            finally:
                release_listing.set()
                for runtime in runtimes:
                    if runtime is not None:
                        runtime.release()
                await asyncio.gather(*(owner.aclose() for owner in supervisors))
                await service.aclose()

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    asyncio.run(scenario())


def test_subscription_relist_fences_management_tools_without_changing_provider_catalog(tmp_path, monkeypatch):
    async def scenario():
        bus = InMemorySubscriptionBus()
        listener = ListenHandler(bus)
        refreshing = False
        release = asyncio.Event()
        started = asyncio.Event()

        async def tools(_ctx, _params):
            if refreshing:
                started.set()
                await release.wait()
            return types.ListToolsResult(tools=[types.Tool(
                name="probe", input_schema={"type": "object"},
            )])

        server = Server("live-relist", on_list_tools=tools, on_subscriptions_listen=listener)
        async with _sdk_http(server) as endpoint:
            service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
                                                user_config_path=tmp_path / "mcp.yaml")
            await service.create(LocalMcpTarget("catalog"), {
                "required": True,
                "transport": {"type": "streamable_http", "endpoint": endpoint,
                              "allow_http_localhost": True},
            })
            supervisor = McpHostSupervisor(
                session_id="session:scheduling", workspace_root=tmp_path,
                configs=service.load_configs(), credential_boundary=service.credential_boundary,
            )
            controller = _controller(service, monkeypatch)
            runtime = None
            try:
                await supervisor.start()
                runtime = supervisor.install_pending_at_safe_point()
                controller._by_session["owner"] = SimpleNamespace(session=_session(supervisor))
                semantic_before = supervisor.catalog_snapshot().semantic_fingerprint
                refreshing = True
                await bus.publish(ToolsListChanged())
                await asyncio.wait_for(started.wait(), 3)
                _assert_unavailable(await _read(controller), "DISCOVERING")
                assert supervisor.inspect_discovery_catalog().catalog_snapshot.servers[0].status is McpServerState.READY
                assert supervisor.catalog_snapshot().semantic_fingerprint == semantic_before
                release.set()
                await _until(lambda: supervisor._refresh_tasks["catalog"].done())
                row = await _read(controller)
                assert row["status"] == "READY" and row["tool_count"] == len(row["tools"]) == 1
                assert supervisor.catalog_snapshot().semantic_fingerprint == semantic_before
            finally:
                release.set()
                if runtime is not None:
                    runtime.release()
                await supervisor.aclose()
                await service.aclose()

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    asyncio.run(scenario())


def test_replaced_connect_task_cannot_supply_status_for_new_config(tmp_path, monkeypatch):
    async def scenario():
        blocked = False
        started = asyncio.Event()
        release = asyncio.Event()

        async def tools(_ctx, _params):
            if blocked:
                started.set()
                await release.wait()
            return types.ListToolsResult(tools=[types.Tool(
                name="probe", input_schema={"type": "object"},
            )])

        server = Server("replaced-owner", on_list_tools=tools)
        async with _sdk_http(server) as endpoint:
            service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
                                                user_config_path=tmp_path / "mcp.yaml")
            target = LocalMcpTarget("catalog")
            await service.create(target, {
                "required": True,
                "transport": {"type": "streamable_http", "endpoint": endpoint,
                              "allow_http_localhost": True},
            })
            supervisor = McpHostSupervisor(
                session_id="session:scheduling", workspace_root=tmp_path,
                configs=service.load_configs(), credential_boundary=service.credential_boundary,
                connect_attempt_timeout_seconds=5,
            )
            controller = _controller(service, monkeypatch)
            runtime = None
            try:
                await supervisor.start()
                runtime = supervisor.install_pending_at_safe_point()
                controller._by_session["owner"] = SimpleNamespace(session=_session(supervisor))
                blocked = True
                supervisor.reconnect("catalog")
                await asyncio.wait_for(started.wait(), 3)
                old_task = supervisor._tasks["catalog"]
                old_config = service.inspect(target)
                entry = config_to_entry(old_config)
                entry["default_tool_timeout_ms"] = 10_000
                await service.update(target, entry, expected=config_guard(old_config))
                _assert_unavailable(await _read(controller), "CONFIGURED")
                supervisor.reload_configs(service.load_configs())
                new_task = supervisor._tasks["catalog"]
                assert new_task is not old_task
                await asyncio.wait_for(old_task, 3)
                assert old_task.result().failure == "CancelledError"
                _assert_unavailable(await _read(controller), "CONNECTING")
                observed = supervisor.inspect_discovery_catalog(live=True)
                assert observed.configured_servers[0].status_matches_config
                assert observed.configured_servers[0].resolved_config_identity == service.inspect(target).resolved_config_identity
                release.set()
                await asyncio.wait_for(new_task, 3)
                row = await _read(controller)
                assert row["status"] == "READY" and row["tool_count"] == len(row["tools"]) == 1
            finally:
                release.set()
                if runtime is not None:
                    runtime.release()
                await supervisor.aclose()
                await service.aclose()

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    asyncio.run(scenario())
