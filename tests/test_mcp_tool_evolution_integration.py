"""Actual SDK refreshes invalidate only changed tools and retain builtin prefix."""
from tests.support.mcp import finish_connection_attempts

import asyncio

from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
import mcp_types as types

from pulsara_agent.capability.contracts import InstalledCapabilityEpochPredecessor
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.permission import PermissionMode
from tests.support.round3 import authorize_direct_tool, prepare_test_direct_tool_surface
from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor
from tests.test_mcp_schema_validation import _port
from tests.test_mcp_meta_only_integration import invoke


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
