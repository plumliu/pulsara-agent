"""Actual SDK refreshes preserve current MCP search and invocation semantics."""

import asyncio
from contextlib import asynccontextmanager
import json
from threading import Event
from time import monotonic

import httpx2
import mcp_types as types
import pytest
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged

from pulsara_agent.capability.contracts import InstalledCapabilityEpochPredecessor
from pulsara_agent.capability.mcp_embedding_cache import PostgresMcpToolEmbeddingCache
from pulsara_agent.capability import mcp_search
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.conversation_kernel.mcp.tool_embeddings import McpToolEmbeddings
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
from pulsara_agent.identity import random_id
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.retrieval.embedding.validation import MEMORY_EMBEDDING_DIMENSIONS
from pulsara_agent.storage.schema_verification_service import acquire_verified_postgres_access_sync
from tests.support.mcp import finish_connection_attempts
from tests.support.round3 import authorize_direct_tool, prepare_test_direct_tool_surface
from tests.test_capability_management_preparation import preparation
from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor
from tests.test_mcp_schema_validation import _port
from tests.test_mcp_meta_only_integration import invoke
from tests.test_mcp_tool_embeddings import _inject_transport


async def authorize(port, target, arguments=None):
    return await authorize_direct_tool(port, session_id=port._session_id, tool_name='call_mcp_tool',
        arguments={'target':target,'arguments':arguments or {}},tool_call_id=random_id('call'),
        turn_id=random_id('turn'),assistant_entry_id=random_id('entry'))


def test_actual_tool_refreshes_preserve_unaffected_ref_and_frozen_builtin_tools(tmp_path):
    async def scenario():
        bus=InMemorySubscriptionBus()
        handler=ListenHandler(bus)
        revision=0
        calls=[]
        refreshing, release=asyncio.Event(),asyncio.Event()
        async def listing(_ctx,_params):
            if revision==2:
                refreshing.set()
                await release.wait()
            items=[types.Tool(name='stable',description='Stable tool',input_schema={'type':'object'})]
            if revision!=3:
                items.append(types.Tool(name='changing',description='original' if revision==0 else 'updated',
                    input_schema={'type':'object','properties':{'value':{'type':'string' if revision==0 else 'integer'}}}))
            return types.ListToolsResult(tools=items)
        async def call(_ctx,params):
            calls.append((params.name,params.arguments))
            return types.CallToolResult(content=[types.TextContent(type='text',text='done')])
        server=Server('meta-refresh',on_list_tools=listing,on_call_tool=call,on_subscriptions_listen=handler)
        async with _sdk_http(server) as endpoint:
            supervisor=_supervisor(tmp_path,endpoint)
            port=_port(tmp_path,supervisor)
            try:
                await supervisor.start()
                await finish_connection_attempts(supervisor)
                port.prepare_tool_surface_safe_point()
                cold=prepare_test_direct_tool_surface(port,effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                frozen=tuple(canonical_json_bytes(item.wire_tool) for item in cold.capability_exposure_plan.direct_projection_set.projections)
                old={}
                for name in ('stable','changing'):
                    result=await invoke(port,'query_capabilities',{'kind':'MCP','target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':name}})
                    old[name]=result['content'][0]['value']['invocation']['target']
                async def refresh():
                    await bus.publish(ToolsListChanged())
                    async with asyncio.timeout(5):
                        while 'catalog' not in supervisor._pending:
                            await asyncio.sleep(.01)
                    port.prepare_tool_surface_safe_point()
                revision=1
                await refresh()
                rejected=await authorize(port,old['changing'],{'value':'old'})
                assert rejected.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                assert 'not dispatched' in rejected.public_message
                assert not calls
                stable=await invoke(port,'query_capabilities',{'kind':'MCP','target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':'stable'}})
                assert stable['content'][0]['value']['invocation']['target']==old['stable']
                await invoke(port,'call_mcp_tool',{'target':old['stable'],'arguments':{}})
                changed=await invoke(port,'query_capabilities',{'kind':'MCP','target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':'changing'}})
                changed=changed['content'][0]['value']
                assert changed['input_schema']['properties']['value']['type']=='integer'
                assert changed['invocation']['target']!=old['changing']
                current=prepare_test_direct_tool_surface(port,effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    predecessor=InstalledCapabilityEpochPredecessor(1,random_id('epoch'),cold.model_surface,cold.capability_exposure_plan.direct_projection_set))
                assert current.model_surface is cold.model_surface
                assert tuple(canonical_json_bytes(item.wire_tool) for item in current.capability_exposure_plan.direct_projection_set.projections)==frozen
                revision=2
                await bus.publish(ToolsListChanged())
                await asyncio.wait_for(refreshing.wait(),5)
                unknown=await invoke(port,'query_capabilities',{'kind':'MCP','target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':'stable'}})
                assert unknown['content'][0]['value']['invocation']['mode']=='UNAVAILABLE'
                assert (await authorize(port,old['stable'])).kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                release.set()
                async with asyncio.timeout(5):
                    while 'catalog' not in supervisor._pending:
                        await asyncio.sleep(.01)
                port.prepare_tool_surface_safe_point()
                # Temporary dirty discovery did not expire an unchanged binding.
                await invoke(port,'call_mcp_tool',{'target':old['stable'],'arguments':{}})
                revision=3
                await refresh()
                assert (await authorize(port,changed['invocation']['target'])).kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                await invoke(port,'call_mcp_tool',{'target':old['stable'],'arguments':{}})
                assert calls==[('stable',{}),('stable',{}),('stable',{})]
                supervisor.reconnect('catalog')
                await asyncio.wait_for(supervisor._tasks['catalog'],5)
                port.prepare_tool_surface_safe_point()
                reconnected=await invoke(port,'query_capabilities',{'kind':'MCP',
                    'target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':'stable'}})
                assert reconnected['content'][0]['value']['invocation']['target']==old['stable']
                await invoke(port,'call_mcp_tool',{'target':old['stable'],'arguments':{}})
                assert calls==[('stable',{})]*4
            finally:
                release.set()
                supervisor.stop_admission()
                closing=asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing
    asyncio.run(scenario())


class _BlockedDenseScores:
    def __init__(self):
        self.connection = object()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.result = {"changing\nOrchid operations": 1.0}
        self.requests = []

    def current_connection(self):
        return self.connection

    async def scores(self, query, *, connection, texts, deadline_monotonic=None):
        self.requests.append((query, set(texts)))
        self.entered.set()
        await self.release.wait()
        return self.result


def _changing_tool(*, description="Orchid operations", value_type="string"):
    schema = {
        "type": "object",
        "properties": {"value": {"type": value_type}},
        "required": ["value"],
    }
    return types.Tool(
        name="changing", description=description, input_schema=schema, output_schema=schema,
    )


@asynccontextmanager
async def _hybrid_search_catalog(tmp_path, dense, *, service=None):
    bus = InMemorySubscriptionBus()
    stable = types.Tool(
        name="stable", description="Orchid inventory", input_schema={"type": "object"},
    )
    tools = [stable, _changing_tool()]
    calls = []

    async def listing(_ctx, _params):
        return types.ListToolsResult(tools=tools)

    async def call(_ctx, params):
        calls.append((params.name, params.arguments))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="done")],
            structuredContent=params.arguments if params.name == "changing" else None,
        )

    server = Server(
        "hybrid-refresh", on_list_tools=listing, on_call_tool=call,
        on_subscriptions_listen=ListenHandler(bus),
    )
    async with _sdk_http(server) as endpoint:
        if service is None:
            supervisor = _supervisor(tmp_path, endpoint)
        else:
            declaration = {
                "catalog_refresh_interval_ms": "DISABLED",
                "transport": {
                    "type": "streamable_http", "endpoint": endpoint, "allow_http_localhost": True,
                },
            }
            service.mcp.user_config_path.write_text(json.dumps({"servers": {"catalog": declaration}}))
            supervisor = McpHostSupervisor(
                workspace_root=service.workspace_root,
                user_config_path=service.mcp.user_config_path,
                configs=(_parse_server("catalog", declaration),),
                credential_boundary=ProcessCredentialBoundary(),
            )
        port = _port(tmp_path, supervisor)
        port._capability_management = service
        port._mcp_tool_embeddings = dense

        async def refresh(*replacement_tools):
            tools[:] = [stable, *replacement_tools]
            await bus.publish(ToolsListChanged())
            async with asyncio.timeout(5):
                while "catalog" not in supervisor._pending:
                    await asyncio.sleep(.01)
            port.prepare_tool_surface_safe_point()

        try:
            await supervisor.start()
            await finish_connection_attempts(supervisor)
            port.prepare_tool_surface_safe_point()
            yield port, supervisor, refresh, calls
        finally:
            supervisor.stop_admission()
            closing = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=10)
            await closing
            if service is not None:
                await service.mcp.aclose()


def _tool_records(result):
    return [
        part["value"] for part in result["content"]
        if part["type"] == "object" and part["value"]["target"]["kind"] == "MCP_TOOL"
    ]


def test_dense_only_cell_query_uses_schema_and_reference_from_refreshed_catalog(tmp_path):
    async def scenario():
        dense = _BlockedDenseScores()
        async with _hybrid_search_catalog(tmp_path, dense) as (port, _, refresh, calls):
            services = await invoke(port, "query_capabilities", {"kind": "MCP"})
            connection = services["content"][0]["value"]["target"]
            tools = await invoke(port, "query_capabilities", {"kind": "MCP", "target": connection})
            assert {record["target"]["remote_tool_name"] for record in _tool_records(tools)} == {
                "changing", "stable",
            }

            original = {}
            for name in ("changing", "stable"):
                result = await invoke(port, "query_capabilities", {"kind": "MCP", "target": {
                    "kind": "MCP_TOOL", "server_id": "catalog", "remote_tool_name": name,
                }})
                original[name] = _tool_records(result)[0]["invocation"]["target"]
            assert dense.requests == []

            surface = prepare_test_direct_tool_surface(
                port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            directory = port.freeze_code_mode_tool_directory(
                surface, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            replacement = _changing_tool(value_type="integer")
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(invoke(
                    port, "query_capabilities", {"kind": "MCP", "query": "orchidectomy"},
                    cell=random_id("cell"), directory=directory,
                ))
                await asyncio.wait_for(dense.entered.wait(), 5)
                await refresh(replacement)
                dense.release.set()
                result = await asyncio.wait_for(pending, 5)

            assert result["meta"] == {"status": "SUCCESS"}
            record, = _tool_records(result)
            assert record["target"]["remote_tool_name"] == "changing"
            assert record["description"] == "Orchid operations"
            assert record["input_schema"] == replacement.input_schema
            assert record["output_schema"] == replacement.output_schema
            current = record["invocation"]["target"]
            assert current != original["changing"]
            rejected = await authorize(port, original["changing"], {"value": "old"})
            assert rejected.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            assert calls == []
            for target, arguments in [(current, {"value": 7}), (original["stable"], {})]:
                called = await invoke(port, "call_mcp_tool", {"target": target, "arguments": arguments})
                assert called["meta"]["status"] == "SUCCESS", called
            assert calls == [("changing", {"value": 7}), ("stable", {})]
            assert dense.requests == [
                ("orchidectomy", {"changing\nOrchid operations", "stable\nOrchid inventory"}),
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["description", "removal", "scope"])
def test_search_discards_stale_dense_scores_after_catalog_change(tmp_path, change):
    async def scenario():
        dense = _BlockedDenseScores()
        async with _hybrid_search_catalog(tmp_path, dense) as (port, supervisor, refresh, _):
            scope = ModelInputScopeKind.SUBAGENT if change == "scope" else ModelInputScopeKind.ROOT
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(invoke(
                    port, "query_capabilities", {"kind": "MCP", "query": "orchid"}, scope=scope,
                ))
                await asyncio.wait_for(dense.entered.wait(), 5)
                if change == "scope":
                    supervisor.reload_configs((_parse_server("catalog", {
                        "scope_policy": "ROOT_ONLY",
                        "catalog_refresh_interval_ms": "DISABLED",
                        "transport": {
                            "type": "streamable_http", "endpoint": supervisor.configs[0].transport.endpoint,
                            "allow_http_localhost": True,
                        },
                    }),))
                    await finish_connection_attempts(supervisor)
                    port.prepare_tool_surface_safe_point()
                elif change == "description":
                    await refresh(_changing_tool(description="Calendar operations"))
                else:
                    await refresh()
                dense.release.set()
                result = await asyncio.wait_for(pending, 5)

            assert result["meta"] == {"status": "SUCCESS"}
            if change == "scope":
                assert result["content"] == [{"type": "text", "value": "没有找到匹配的 MCP 工具或服务。"}]
                root = await invoke(port, "query_capabilities", {"kind": "MCP", "target": {
                    "kind": "MCP_TOOL", "server_id": "catalog", "remote_tool_name": "changing",
                }})
                assert _tool_records(root)[0]["description"] == "Orchid operations"
            else:
                record, = _tool_records(result)
                assert record["target"]["remote_tool_name"] == "stable"
                assert record["description"] == "Orchid inventory"
                called = await invoke(
                    port, "call_mcp_tool", {"target": record["invocation"]["target"], "arguments": {}},
                )
                assert called["meta"]["status"] == "SUCCESS", called
            assert dense.requests == [
                ("orchid", {"changing\nOrchid operations", "stable\nOrchid inventory"}),
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("dense_enabled", [False, True])
@pytest.mark.parametrize("query", ["orchid", "changing"])
def test_keyword_search_rechecks_schema_after_ranking(tmp_path, monkeypatch, dense_enabled, query):
    ranked, release_rank = Event(), Event()
    search = mcp_search.search_mcp

    def blocked_search(*args, **kwargs):
        matches = search(*args, **kwargs)
        ranked.set()
        assert release_rank.wait(5)
        return matches

    monkeypatch.setattr(mcp_search, "search_mcp", blocked_search)

    async def scenario():
        dense = _BlockedDenseScores() if dense_enabled else None
        async with _hybrid_search_catalog(tmp_path, dense) as (port, _, refresh, calls):
            description = "New calendar operation" if query == "changing" else "Orchid operations"
            replacement = _changing_tool(value_type="boolean", description=description)
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(invoke(
                    port, "query_capabilities", {"kind": "MCP", "query": query},
                ))
                try:
                    if dense is not None:
                        await asyncio.wait_for(dense.entered.wait(), 5)
                        await refresh(_changing_tool(value_type="integer"))
                        dense.release.set()
                    assert await asyncio.to_thread(ranked.wait, 5)
                    await refresh(replacement)
                finally:
                    release_rank.set()
                result = await asyncio.wait_for(pending, 5)

            assert result["meta"] == {"status": "ERROR"}
            assert any("本次搜索不完整" in part["value"] for part in result["content"] if part["type"] == "text")
            records = {record["target"]["remote_tool_name"]: record for record in _tool_records(result)}
            expected_calls = [("changing", {"value": True})]
            if query == "orchid":
                expected_calls.append(("stable", {}))
            assert set(records) == {name for name, _ in expected_calls}
            changed = records["changing"]
            assert changed["description"] == replacement.description
            assert changed["input_schema"] == replacement.input_schema
            assert changed["output_schema"] == replacement.output_schema
            for name, arguments in expected_calls:
                called = await invoke(port, "call_mcp_tool", {
                    "target": records[name]["invocation"]["target"], "arguments": arguments,
                })
                assert called["meta"] == {"status": "SUCCESS"}
            assert calls == expected_calls

    asyncio.run(scenario())


@pytest.mark.parametrize("query_kind", ["dense", "exact"])
def test_query_rechecks_current_facts_after_credential_capture(tmp_path, monkeypatch, query_kind):
    service = preparation(tmp_path)
    capture = service.plugins._capture_scrub_set
    capturing, release_capture = Event(), Event()

    def blocked_capture(request):
        if request.workspace_root is None and not capturing.is_set():
            capturing.set()
            assert release_capture.wait(5)
        return capture(request)

    monkeypatch.setattr(service.plugins, "_capture_scrub_set", blocked_capture)

    async def scenario():
        dense = _BlockedDenseScores()
        dense.release.set()
        async with _hybrid_search_catalog(tmp_path, dense, service=service) as (port, _, refresh, calls):
            arguments = {"kind": "MCP"}
            if query_kind == "dense":
                arguments["query"] = "orchidectomy"
            else:
                arguments["target"] = {
                    "kind": "MCP_TOOL", "server_id": "catalog", "remote_tool_name": "changing",
                }
            replacement = _changing_tool(value_type="integer")
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(invoke(port, "query_capabilities", arguments))
                try:
                    assert await asyncio.to_thread(capturing.wait, 5)
                    if query_kind == "dense":
                        dense.connection = object()
                    await refresh(replacement)
                finally:
                    release_capture.set()
                result = await asyncio.wait_for(pending, 5)

            if query_kind == "dense":
                assert result == {
                    "meta": {"status": "SUCCESS"},
                    "content": [{"type": "text", "value": "没有找到匹配的 MCP 工具或服务。"}],
                }
                assert dense.requests == [
                    ("orchidectomy", {"changing\nOrchid operations", "stable\nOrchid inventory"}),
                ]
            else:
                record, = _tool_records(result)
                assert result["meta"] == {"status": "SUCCESS"}
                assert record["input_schema"] == replacement.input_schema
                called = await invoke(port, "call_mcp_tool", {
                    "target": record["invocation"]["target"], "arguments": {"value": 4},
                })
                assert called["meta"] == {"status": "SUCCESS"}
                assert calls == [("changing", {"value": 4})]
                assert dense.requests == []

    asyncio.run(scenario())


def test_cancelling_pending_dense_query_preserves_query_cancellation(tmp_path):
    async def scenario():
        dense = _BlockedDenseScores()
        async with _hybrid_search_catalog(tmp_path, dense) as (port, _, _, _):
            arguments = {"kind": "MCP", "query": "orchid"}
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(invoke(port, "query_capabilities", arguments))
                await asyncio.wait_for(dense.entered.wait(), 5)
                pending.cancel()
                result = await asyncio.wait_for(pending, 5)
            assert result == {
                "meta": {"status": "CANCELLED"},
                "content": [{"type": "text", "value": "能力查询已取消。"}],
            }

            dense.result = {}
            dense.release.set()
            fallback = await invoke(port, "query_capabilities", arguments)
            assert fallback["meta"] == {"status": "SUCCESS"}
            assert {record["target"]["remote_tool_name"] for record in _tool_records(fallback)} == {
                "changing", "stable",
            }

    asyncio.run(scenario())


@pytest.mark.postgres
def test_cold_native_query_warms_pg_for_dense_only_cell_query(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    connection = RetrievalConnection(
        "https://embedding.example/v1/embeddings", "mcp-evolution-test", "openai_embedding",
        api_key="fixture-mcp-embedding-key",
    )
    query = "orchidectomy"
    arguments = {"kind": "MCP", "query": query}
    candidate_text = "changing\nOrchid operations"
    distractor_text = "stable\nOrchid inventory"
    tail = [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 2)
    vectors = {
        candidate_text: [1.0, 0.0, *tail],
        distractor_text: [0.0, 1.0, *tail],
        query: [1.0, 0.0, *tail],
    }

    async def scenario(access):
        warming, release = asyncio.Event(), asyncio.Event()
        embedding_requests = []

        async def respond(request):
            inputs = json.loads(request.content)["input"]
            embedding_requests.append(inputs)
            if inputs != [query]:
                warming.set()
                await release.wait()
            return httpx2.Response(200, json={"data": [
                {"index": index, "embedding": vectors[value]}
                for index, value in enumerate(inputs)
            ]})

        _inject_transport(monkeypatch, respond)
        dense = McpToolEmbeddings(
            PostgresMcpToolEmbeddingCache(access.connection_provider),
            connection=lambda: connection, requests=asyncio.Semaphore(5),
        )
        try:
            async with _hybrid_search_catalog(tmp_path, dense) as (port, _, _, calls):
                cold = await asyncio.wait_for(invoke(port, "query_capabilities", arguments), 5)
                assert cold == {
                    "meta": {"status": "SUCCESS"},
                    "content": [{"type": "text", "value": "没有找到匹配的 MCP 工具或服务。"}],
                }
                await asyncio.wait_for(warming.wait(), 5)
                documents, = embedding_requests
                assert sorted(documents) == [candidate_text, distractor_text]
                release.set()
                await asyncio.wait_for(dense._worker, 5)

                surface = prepare_test_direct_tool_surface(
                    port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                directory = port.freeze_code_mode_tool_directory(
                    surface, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                cell = random_id("cell")
                hot = await invoke(port, "query_capabilities", arguments, cell=cell, directory=directory)
                assert hot["meta"] == {"status": "SUCCESS"}
                record, = _tool_records(hot)
                assert record["target"]["remote_tool_name"] == "changing"
                tool = _changing_tool()
                assert record["input_schema"] == tool.input_schema
                assert record["output_schema"] == tool.output_schema
                called = await invoke(port, "call_mcp_tool", {
                    "target": record["invocation"]["target"], "arguments": {"value": "confirmed"},
                }, cell=cell, directory=directory)
                assert called["meta"] == {"status": "SUCCESS"}
                assert calls == [("changing", {"value": "confirmed"})]
                assert embedding_requests == [documents, [query]]
        finally:
            release.set()
            await dense.aclose()

    with acquire_verified_postgres_access_sync(
        isolated_migrated_postgres_database.runtime_dsn, deadline_monotonic=monotonic() + 30,
    ) as access:
        asyncio.run(scenario(access))


def test_accepted_removal_retires_reference_even_without_a_query_before_readdition(tmp_path):
    async def exercise():
        async with _hybrid_search_catalog(tmp_path, None) as (port, _, refresh, calls):
            async def details(name):
                result = await invoke(port, "query_capabilities", {"kind": "MCP", "target": {
                    "kind": "MCP_TOOL", "server_id": "catalog", "remote_tool_name": name,
                }})
                return _tool_records(result)[0]["invocation"]["target"]

            original, stable = await details("changing"), await details("stable")
            await refresh()
            await refresh(_changing_tool())

            rejected = await authorize(port, original, {"value": "old"})
            assert rejected.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            assert calls == []
            assert await details("stable") == stable
            replacement = await details("changing")
            assert replacement != original
            called = await invoke(port, "call_mcp_tool", {
                "target": replacement, "arguments": {"value": "new"},
            })
            assert called["meta"]["status"] == "SUCCESS"
            assert calls == [("changing", {"value": "new"})]

    asyncio.run(exercise())
