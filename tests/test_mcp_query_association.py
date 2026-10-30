"""Query association uses frozen Host provenance, independently of discovery."""

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.capability.source_query import CapabilityQueryError
from pulsara_agent.capability.contracts import CapabilitySourceKind, CapabilitySourceSnapshotDisposition
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.mcp_config import (
    LocalConfiguredMcpRuntimeSource,
    ManagedPackageMcpRuntimeSource,
    McpLocalConfigSourceKind,
    _parse_server,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_capability_management_preparation import preparation, install_plugin


def saved_connection(service, *, scope="USER", enabled=False, subagents=False):
    raw = {"enabled": enabled, "scope_policy": "ROOT_AND_SUBAGENTS" if subagents else "ROOT_ONLY", "transport": {
        "type": "streamable_http", "endpoint": "https://example.org/mcp",
    }}
    root = service.workspace_root if scope == "WORKSPACE" else None
    path = service.mcp.path(LocalMcpTarget("firecrawl", root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"servers": {"firecrawl": raw}}))
    return _parse_server("firecrawl", raw, runtime_source=LocalConfiguredMcpRuntimeSource(
        McpLocalConfigSourceKind(scope)))


def supervisor_for(service, config):
    return McpHostSupervisor(
        session_id="session:association-test", workspace_root=service.workspace_root,
        configs=(config,), credential_boundary=ProcessCredentialBoundary(),
    )


async def query(service, runtime, arguments, *, scope=ModelInputScopeKind.ROOT):
    catalog = runtime.catalog_for_scope(scope)
    # The model-facing query receives the same frozen catalog/route join as the
    # Host. No candidate is fabricated to make an undiscovered service link.
    plan = SimpleNamespace(
        mcp_catalog_route_projection=SimpleNamespace(
            joined_catalog_semantic_fingerprint=catalog.semantic_fingerprint, routes=()),
        dispatch_view=SimpleNamespace(planning_input=SimpleNamespace(
            mcp=SimpleNamespace(source_snapshots=tuple(
                SimpleNamespace(registration=SimpleNamespace(source=SimpleNamespace(
                    kind=CapabilitySourceKind.MCP_SERVER, stable_source_id=server_id)),
                    disposition=CapabilitySourceSnapshotDisposition.COMPLETE)
                for server_id in runtime.candidates
            )))),
    )
    return await CapabilitySourceQuery(service, runtime=runtime, plan=plan, scope=scope).execute(arguments)


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
@pytest.mark.parametrize("state", [McpServerState.DISABLED, McpServerState.CONNECTING,
                                  McpServerState.DISCOVERING, McpServerState.FAILED_RETRYABLE])
def test_undiscovered_local_connection_is_one_model_visible_item(tmp_path, scope, state):
    async def run():
        service = preparation(tmp_path)
        config = saved_connection(service, scope=scope, enabled=state is not McpServerState.DISABLED)
        supervisor = supervisor_for(service, config)
        # Drive the production catalog publisher without starting any transport.
        supervisor._set_catalog_state_locked(config.server_id, state)
        runtime = supervisor.install_pending_at_safe_point()
        try:
            assert not runtime.candidates and not runtime.root_tool_specs
            page = await query(service, runtime, {"kind": "MCP_SERVER", "limit": 1})
            assert page["total_count"] == page["returned_count"] == 1
            assert page["next_offset"] is None
            row, = page["items"]
            assert row["target"] == {"kind": "MCP_CONNECTION", "scope": scope,
                                     "source_kind": "LOCAL", "server_id": "firecrawl"}
            assert state.value in row["status"]
            detail = await query(service, runtime, {"target": row["target"]})
            assert detail["runtime_target"] == {"kind": "MCP_SERVER", "runtime_server_id": "firecrawl"}
            assert detail["catalog_discovered"] is False
            children = await query(service, runtime, {"parent": row["target"], "kind": "MCP_TOOL"})
            assert children["items"] == [] and children["completeness"] == "PARTIAL"
            assert {d["code"] for d in children["diagnostics"]} == {"MCP_CATALOG_NOT_DISCOVERED"}
            live = await query(service, runtime, {"target": detail["runtime_target"]})
            assert live["source"] == {"kind": "LOCAL", "scope": scope}
            assert "connection" not in live  # Provenance is not a configuration summary.
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


def test_retained_discovery_keeps_its_own_source_during_config_reconnect(tmp_path):
    async def run():
        from tests.test_round6_mcp_production import _FakeMcpClient

        service = preparation(tmp_path)
        config = saved_connection(service, enabled=True)
        raw = {"enabled": True, "required": True,
               "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"}}
        config = _parse_server("firecrawl", raw, runtime_source=config.runtime_source)
        supervisor = McpHostSupervisor(session_id="session:retained-source",
            workspace_root=service.workspace_root, configs=(config,),
            credential_boundary=ProcessCredentialBoundary(), client_factory=_FakeMcpClient)
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        try:
            page = await query(service, runtime, {"kind": "MCP_SERVER"})
            assert page["total_count"] == 1
            detail = await query(service, runtime, {"target": page["items"][0]["target"]})
            assert detail["catalog_discovered"] is True
            replacement = _parse_server("firecrawl", raw, runtime_source=LocalConfiguredMcpRuntimeSource(
                McpLocalConfigSourceKind.HOST_OVERRIDE))
            assert replacement.semantic_config_fingerprint == config.semantic_config_fingerprint
            supervisor.reload_configs((replacement,))
            # Reconnection may retain the old tool surface. Its provenance must
            # remain the old candidate's, never the new same-named declaration.
            assert supervisor.catalog_snapshot().servers[0].runtime_source == config.runtime_source
            assert runtime.catalog_snapshot.servers[0].runtime_source == config.runtime_source
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


def test_same_name_in_different_scopes_is_not_collapsed(tmp_path):
    async def run():
        service = preparation(tmp_path)
        saved_connection(service, scope="USER")
        config = saved_connection(service, scope="WORKSPACE")
        supervisor = supervisor_for(service, config)
        runtime = supervisor.install_pending_at_safe_point()
        try:
            page = await query(service, runtime, {"kind": "MCP_SERVER"})
            assert page["total_count"] == 2
            assert {r["target"]["scope"] for r in page["items"]} == {"USER", "WORKSPACE"}
            for row in page["items"]:
                detail = await query(service, runtime, {"target": row["target"]})
                assert ("runtime_target" in detail) == (row["target"]["scope"] == "WORKSPACE")
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


def test_host_override_is_not_merged_with_same_named_user_connection(tmp_path):
    async def run():
        service = preparation(tmp_path)
        saved_connection(service)
        config = _parse_server("firecrawl", {"enabled": False,
            "transport": {"type": "stdio", "command": "unused"}},
            runtime_source=LocalConfiguredMcpRuntimeSource(McpLocalConfigSourceKind.HOST_OVERRIDE))
        supervisor = supervisor_for(service, config)
        runtime = supervisor.install_pending_at_safe_point()
        try:
            page = await query(service, runtime, {"kind": "MCP_SERVER"})
            assert page["total_count"] == 2
            assert {r["target"]["kind"] for r in page["items"]} == {"MCP_SERVER", "MCP_CONNECTION"}
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("same_install", [True, False])
def test_plugin_connection_requires_exact_package_provenance(tmp_path, same_install):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        origin = ManagedPackageMcpRuntimeSource(
            "user", "example", installed.package_install_id if same_install else "package:" + "a" * 26)
        runtime_id = framed_plugin_mcp_server_id("example", "docs")
        config = _parse_server(runtime_id, {"enabled": False,
            "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"}},
            runtime_source=origin)
        supervisor = supervisor_for(service, config)
        runtime = supervisor.install_pending_at_safe_point()
        try:
            assert not runtime.candidates
            page = await query(service, runtime, {"kind": "MCP_SERVER"})
            assert page["total_count"] == (1 if same_install else 2)
            row = next(r for r in page["items"] if r["target"]["kind"] == "MCP_CONNECTION")
            detail = await query(service, runtime, {"target": row["target"]})
            assert ("runtime_target" in detail) == same_install
            if same_install:
                assert detail["runtime_target"]["runtime_server_id"] == runtime_id
                assert detail["catalog_discovered"] is False
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("subagents", [False, True])
def test_runtime_provenance_is_frozen_and_child_query_keeps_scope(tmp_path, subagents):
    async def run():
        service = preparation(tmp_path)
        config = saved_connection(service, subagents=subagents)
        supervisor = supervisor_for(service, config)
        old = supervisor.install_pending_at_safe_point()
        new = None
        try:
            # A supervisor reload cannot rewrite the already borrowed epoch view.
            replacement = _parse_server("firecrawl", {"enabled": False,
                "transport": {"type": "stdio", "command": "unused"}},
                runtime_source=LocalConfiguredMcpRuntimeSource(McpLocalConfigSourceKind.HOST_OVERRIDE))
            supervisor.reload_configs((replacement,))
            new = supervisor.install_pending_at_safe_point()
            assert (await query(service, old, {"kind": "MCP_SERVER"}))["total_count"] == 1
            assert (await query(service, new, {"kind": "MCP_SERVER"}))["total_count"] == 2
            # No provenance field is serialized into the scoped runtime result.
            target = {"kind": "MCP_SERVER", "runtime_server_id": "firecrawl"}
            if subagents:
                detail = await query(None, old, {"target": target}, scope=ModelInputScopeKind.SUBAGENT)
                assert detail["source"] is None
                assert "connection" not in detail and "config_path" not in detail
            else:
                with pytest.raises(CapabilityQueryError) as error:
                    await query(None, old, {"target": target}, scope=ModelInputScopeKind.SUBAGENT)
                assert error.value.code == "CAPABILITY_NOT_FOUND"
        finally:
            old.release()
            if new is not None:
                new.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())
