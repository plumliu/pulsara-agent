"""MCP refresh/permission and Plugin data-owner hard-cut contracts."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import anyio
import mcp_types as types
from mcp import ClientSession
import pytest

from tests import test_round6_mcp_production as mcp_fixture
from tests import test_round9_3_plugin_physical_semantics as plugin_fixture
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    BoundedMcpSdkClient,
    _SdkHttpTransport,
)
from pulsara_agent.conversation_kernel.mcp.supervisor import (
    McpHostSupervisor,
    _effect,
    discover_mcp_catalog,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    McpEffectKind,
    McpPolicyClassificationSource,
)
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.plugins.contracts import (
    InstallLocalPluginRequest,
    InspectLocalPluginsRequest,
    NeverCancelPluginOperation,
    PluginScopeKind,
)
from pulsara_agent.plugins.mcp_adapter import normalize_plugin_mcp_configs
from pulsara_agent.plugins.view import EnabledPluginViewOwner


@pytest.mark.parametrize("hint", [None, False, True])
@pytest.mark.parametrize("open_world", [None, False, True])
def test_auto_hints_never_grant_read_only(tmp_path, hint, open_world):
    config = mcp_fixture._config(tmp_path)
    tool = types.Tool(
        name="probe",
        inputSchema={},
        annotations=types.ToolAnnotations(
            readOnlyHint=hint,
            openWorldHint=open_world,
            destructiveHint=False,
        ),
    )
    assert _effect(config, tool) == (
        McpEffectKind.EXTERNAL_EFFECT,
        McpPolicyClassificationSource.HOST_DEFAULT,
    )
    server_override = mcp_fixture._config(tmp_path, default_effect="READ_ONLY")
    assert _effect(server_override, tool) == (
        McpEffectKind.READ_ONLY,
        McpPolicyClassificationSource.SERVER_OVERRIDE,
    )
    tool_override = mcp_fixture._config(
        tmp_path,
        default_effect="READ_ONLY",
        tool_effect_overrides={"probe": "EXTERNAL_EFFECT"},
    )
    assert _effect(tool_override, tool) == (
        McpEffectKind.EXTERNAL_EFFECT,
        McpPolicyClassificationSource.TOOL_OVERRIDE,
    )


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("asserted", [False, True])
@pytest.mark.parametrize("sessionful", [False, True])
@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
def test_http_parallel_gate_preserves_both_host_choices(
    tmp_path, parallel, asserted, sessionful, version
):
    async def exercise():
        config = _parse_server(
            "http",
            {
                "supports_parallel_tool_calls": parallel,
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.invalid/mcp",
                    "stateless_http_asserted": asserted,
                },
            },
        )
        client = BoundedMcpSdkClient(
            config,
            workspace_root=tmp_path,
            notification_callback=lambda _method: asyncio.sleep(0),
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
        )
        transport = _SdkHttpTransport(
            config,
            config.transport,
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
            bounds=mcp_fixture.DEFAULT_MCP_WIRE_BOUNDS,
        )
        transport._client = SimpleNamespace(sessionful=sessionful)
        client._transport = transport
        client.protocol_version = version
        assert client.supports_bounded_stateless_parallelism is (
            parallel and asserted and not sessionful
        )

    asyncio.run(exercise())


def test_old_stateless_configuration_key_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        _parse_server(
            "http",
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.invalid/mcp",
                    "proved_stateless": True,
                }
            },
        )


def test_periodic_refresh_reuses_connection_and_keeps_old_lease_stale(
    tmp_path, monkeypatch
):
    async def exercise():
        mcp_fixture._FakeMcpClient.instances.clear()
        supervisor = McpHostSupervisor(
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
            session_id="periodic",
            workspace_root=tmp_path,
            configs=(
                mcp_fixture._config(tmp_path, catalog_refresh_interval_ms=300000),
            ),
            client_factory=mcp_fixture._FakeMcpClient,
        )
        original_sleep = asyncio.sleep
        tick = asyncio.Event()

        async def controlled_sleep(delay):
            if delay == 300:
                await tick.wait()
                tick.clear()
            else:
                await original_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        second = None
        try:
            slot = first.slot_lease_by_server["fixture"]._slot
            tick.set()
            async with asyncio.timeout(3):
                while "fixture" not in supervisor._refresh_tasks:
                    await original_sleep(0.01)
                await supervisor._refresh_tasks["fixture"]
            assert len(mcp_fixture._FakeMcpClient.instances) == 1
            assert not slot.lease_is_current(first.slot_lease_by_server["fixture"])
            second = supervisor.install_pending_at_safe_point()
            assert second.slot_lease_by_server["fixture"]._slot is slot
            assert slot.lease_is_current(second.slot_lease_by_server["fixture"])
            assert not mcp_fixture._FakeMcpClient.instances[0].closed
        finally:
            first.release()
            if second:
                second.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_relist_drains_bounded_calls_and_retries_notification_race(tmp_path):
    async def exercise():
        started, finish = asyncio.Event(), asyncio.Event()

        class Session(mcp_fixture._FakeMcpSession):
            lists = 0

            async def list_tools(self, **kwargs):
                self.lists += 1
                if self.lists == 2:
                    await client.notification_callback(
                        "notifications/tools/list_changed"
                    )
                return await super().list_tools(**kwargs)

            async def call_tool(self, *args, **kwargs):
                started.set()
                await finish.wait()
                assert (
                    self.lists == 1
                )  # Listing cannot mutate SDK cache before settlement.
                return await super().call_tool(*args, **kwargs)

        class Client(mcp_fixture._FakeMcpClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.session = Session()
                self.supports_bounded_stateless_parallelism = True

        supervisor = McpHostSupervisor(
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
            session_id="drain",
            workspace_root=tmp_path,
            configs=(mcp_fixture._config(tmp_path),),
            client_factory=Client,
        )
        await supervisor.start()
        client = supervisor._slots["fixture"].client
        first = supervisor.install_pending_at_safe_point()
        second = None
        call = None
        try:
            executor = next(iter(first.executors.values()))
            permit = executor.admit(
                session_id="drain",
                scope_kind=mcp_fixture.ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn",
                tool_call_id="call",
            )
            permit.mark_attempt_accepted()
            call = asyncio.create_task(executor.invoke(permit, {"text": "old"}))
            await started.wait()
            await client.notification_callback("notifications/tools/list_changed")
            await asyncio.sleep(0.03)
            assert client.session.lists == 1
            finish.set()
            assert (await call).state == "SUCCESS"
            async with asyncio.timeout(3):
                await supervisor._refresh_tasks["fixture"]
            assert (
                client.session.lists == 3
            )  # The notification during relist forces a new full list.
            second = supervisor.install_pending_at_safe_point()
            assert (
                second.slot_lease_by_server["fixture"]._slot
                is first.slot_lease_by_server["fixture"]._slot
            )
            assert not client.closed
        finally:
            finish.set()
            if call:
                await call
            first.release()
            if second:
                second.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_late_session_id_fences_parallel_slot_but_accepted_call_settles(tmp_path):
    async def exercise():
        class Session(mcp_fixture._FakeMcpSession):
            async def call_tool(self, *args, **kwargs):
                await client.notification_callback("pulsara/sessionful_transport")
                return await super().call_tool(*args, **kwargs)

        class Client(mcp_fixture._FakeMcpClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.session = Session()
                self.supports_bounded_stateless_parallelism = True

        supervisor = McpHostSupervisor(
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
            session_id="late",
            workspace_root=tmp_path,
            configs=(mcp_fixture._config(tmp_path),),
            client_factory=Client,
        )
        await supervisor.start()
        client = supervisor._slots["fixture"].client
        runtime = supervisor.install_pending_at_safe_point()
        try:
            executor = next(iter(runtime.executors.values()))
            permit = executor.admit(
                session_id="late",
                scope_kind=mcp_fixture.ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn",
                tool_call_id="call",
            )
            permit.mark_attempt_accepted()
            assert (await executor.invoke(permit, {})).state == "SUCCESS"
            with pytest.raises(mcp_fixture.McpSnapshotStale):
                executor.admit(
                    session_id="late",
                    scope_kind=mcp_fixture.ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn",
                    tool_call_id="next",
                )
            assert "fixture" not in supervisor._retry_tasks
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_complete_paginated_catalog_prunes_sdk_owned_caches(tmp_path, monkeypatch):
    async def exercise():
        read_send, read_receive = anyio.create_memory_object_stream(1)
        write_send, write_receive = anyio.create_memory_object_stream(1)
        session = ClientSession(read_receive, write_send)
        session._negotiated_version = "2026-07-28"
        names = ["alpha", "beta"]

        async def send(request, _result_type, **kwargs):
            index = (
                int(request.params.cursor)
                if request.params and request.params.cursor
                else 0
            )
            return types.ListToolsResult(
                resultType="complete",
                nextCursor="1" if index == 0 else None,
                tools=[
                    types.Tool(
                        name=names[index],
                        inputSchema={"type": "object"},
                        outputSchema={"type": "object"},
                    )
                ],
            )

        monkeypatch.setattr(session, "send_request", send)
        client = BoundedMcpSdkClient(
            mcp_fixture._config(tmp_path),
            workspace_root=tmp_path,
            notification_callback=lambda _method: asyncio.sleep(0),
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
        )
        client._session = session
        client.protocol_version = "2026-07-28"
        client.advertised_capabilities = mcp_fixture.McpAdvertisedCapabilities(
            tools=True, resources=False, prompts=False
        )
        try:
            await discover_mcp_catalog(client, client.config)
            await session.validate_tool_result(
                "beta", types.CallToolResult(content=[], structuredContent={})
            )
            assert "beta" in session._tool_output_validators
            names[:] = ["gamma", "delta"]
            for _ in range(3):
                await discover_mcp_catalog(client, client.config)
                assert set(session._tool_output_schemas) == {"gamma", "delta"}
                assert set(session._x_mcp_header_maps) == {"gamma", "delta"}
                assert "beta" not in session._tool_output_validators
        finally:
            await read_send.aclose()
            await read_receive.aclose()
            await write_send.aclose()
            await write_receive.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "kind,hooks,creates",
    [
        ("none", False, False),
        ("http", False, False),
        ("stdio", False, True),
        ("none", True, True),
    ],
)
def test_plugin_enable_and_inspection_do_not_prepare_data(
    tmp_path, kind, hooks, creates
):
    source = plugin_fixture._make_package(
        tmp_path / "source", mcp_kind=kind, hooks=hooks
    )
    _home, boundary, service, store = plugin_fixture._owners(tmp_path)
    deadline = monotonic() + 30
    installed = plugin_fixture._install(
        service, InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    layout = store.layout(installed.identity)
    plugin_fixture._enable(service, installed, "physical-plugin", deadline)
    assert not layout.data_root.exists()
    inspection = service.inspect_local_plugins(
        InspectLocalPluginsRequest(deadline, workspace_root=tmp_path)
    )
    inspection.close()
    owner = EnabledPluginViewOwner(store=store, credential_boundary=boundary)
    view = owner.observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    view.close()
    assert not layout.data_root.exists()
    runtime = owner.observe_for_runtime(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    runtime.close()
    assert layout.data_root.exists() is creates
    if creates:
        assert layout.data_root.stat().st_mode & 0o777 == 0o700


def test_unavailable_data_preserves_http_skills_and_declared_shadowing(tmp_path):
    source = plugin_fixture._make_package(tmp_path / "source", hooks=True)
    path = source / "mcp.json"
    document = json.loads(path.read_text())
    document["mcpServers"]["remote"] = {
        "type": "streamable-http",
        "url": "https://example.invalid/mcp",
    }
    path.write_text(json.dumps(document))
    _home, boundary, service, store = plugin_fixture._owners(tmp_path)
    deadline = monotonic() + 30
    installed = plugin_fixture._install(
        service, InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    plugin_fixture._enable(service, installed, "physical-plugin", deadline)
    layout = store.layout(installed.identity)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.data_root.parent.mkdir(parents=True)
    layout.data_root.symlink_to(outside, target_is_directory=True)
    owner = EnabledPluginViewOwner(store=store, credential_boundary=boundary)
    query = owner.observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    try:
        assert len(query.instances[0].mcp.parsed.valid_servers) == 2
        assert query.instances[0].hooks.parsed.definitions
    finally:
        query.close()
    runtime = owner.observe_for_runtime(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    result = None
    try:
        instance = runtime.instances[0]
        assert instance.skills.candidates
        assert instance.mcp.parsed.declared_server_ids == ("fixture", "remote")
        assert [
            server.local_server_id for server in instance.mcp.parsed.valid_servers
        ] == ["remote"]
        assert instance.hooks.disposition.value == "UNAVAILABLE"
        assert any(
            item.code.value == "plugin_data_root_unavailable"
            for item in instance.diagnostics
        )
        result = normalize_plugin_mcp_configs(existing_configs=(), view=runtime)
        assert len(result.plugin_configs) == 1
        assert result.plugin_configs[0].transport.kind.value == "streamable_http"
        assert list(outside.iterdir()) == []
    finally:
        if result:
            result.close_plugin_anchors()
        runtime.close()


@pytest.mark.parametrize("asserted", [False, True])
def test_http_cli_add_uses_new_stateless_assertion(tmp_path, monkeypatch, asserted):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    parser = mcp_fixture.build_parser()
    command = ["mcp", "add", "http", "--url", "https://example.invalid/mcp"]
    if asserted:
        command.append("--stateless-http-asserted")
    result = asyncio.run(mcp_fixture._mcp_command(parser.parse_args(command)))
    assert result["status"] == "ok"
    from pulsara_agent.mcp_config import load_mcp_server_configs

    (configured,) = load_mcp_server_configs(
        user_config_path=tmp_path / "home" / "mcp.yaml"
    )
    assert configured.transport.stateless_http_asserted is asserted
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "mcp",
                "add",
                "other",
                "--url",
                "https://example.invalid/mcp",
                "--proved-stateless",
            ]
        )


def test_safe_point_defers_other_server_while_dirty_call_drains(tmp_path):
    async def exercise():
        started, finish = asyncio.Event(), asyncio.Event()

        class Session(mcp_fixture._FakeMcpSession):
            async def call_tool(self, *args, **kwargs):
                started.set()
                await finish.wait()
                return await super().call_tool(*args, **kwargs)

        class Client(mcp_fixture._FakeMcpClient):
            def __init__(self, config, **kwargs):
                super().__init__(config, **kwargs)
                if config.server_id == "a":
                    self.session = Session()

        configs = tuple(
            _parse_server(
                name,
                {
                    "required": True,
                    "catalog_refresh_interval_ms": "DISABLED",
                    "transport": {"type": "stdio", "command": "/usr/bin/true"},
                },
            )
            for name in ("a", "b")
        )
        supervisor = McpHostSupervisor(
            session_id="two",
            workspace_root=tmp_path,
            configs=configs,
            credential_boundary=mcp_fixture._TEST_API_KEY_BOUNDARY,
            client_factory=Client,
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        second = None
        clients = {name: supervisor._slots[name].client for name in ("a", "b")}
        executor = next(
            item for item in first.executors.values() if item.semantic.server_id == "a"
        )
        permit = executor.admit(
            session_id="two",
            scope_kind=mcp_fixture.ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            turn_id="turn",
            tool_call_id="call",
        )
        permit.mark_attempt_accepted()
        call = asyncio.create_task(executor.invoke(permit, {}))
        try:
            await started.wait()
            await clients["a"].notification_callback("notifications/tools/list_changed")
            await clients["b"].notification_callback("notifications/tools/list_changed")
            await supervisor._refresh_tasks["b"]
            assert supervisor.install_pending_at_safe_point() is None
            assert "b" in supervisor._pending
            finish.set()
            assert (await call).state == "SUCCESS"
            await supervisor._refresh_tasks["a"]
            second = supervisor.install_pending_at_safe_point()
            assert second is not None
            for name in ("a", "b"):
                assert (
                    second.slot_lease_by_server[name]._slot
                    is first.slot_lease_by_server[name]._slot
                )
                assert second.slot_lease_by_server[name]._slot.lease_is_current(
                    second.slot_lease_by_server[name]
                )
        finally:
            finish.set()
            await call
            first.release()
            if second:
                second.release()
            await supervisor.aclose()

    asyncio.run(exercise())
