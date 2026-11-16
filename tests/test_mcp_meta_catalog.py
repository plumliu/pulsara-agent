"""A recorded public MCP catalog stays queryable through one fixed meta API."""

import asyncio
import json
from pathlib import Path
import sys

from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.support.round3 import prepare_test_direct_tool_surface
from tests.test_mcp_schema_validation import _port
from tests.test_mcp_meta_only_integration import invoke
from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor
from mcp.server.lowlevel import Server
import mcp_types as types


def test_recorded_notion_directory_and_full_schema_use_actual_sdk_meta_call(tmp_path):
    async def scenario():
        fixture=Path(__file__).parent/'fixtures/notion_schema_replay_server.py'
        path=tmp_path/'mcp.yaml'
        path.write_text('servers:\n  notion:\n    catalog_refresh_interval_ms: DISABLED\n    effect_policy: {default_effect: READ_ONLY}\n    transport: '+json.dumps({'type':'stdio','command':sys.executable,'args':[str(fixture)]})+'\n')
        supervisor=McpHostSupervisor(workspace_root=tmp_path,pulsara_home=tmp_path/'home',user_config_path=path,
            configs=load_mcp_server_configs(user_config_path=path),credential_boundary=ProcessCredentialBoundary())
        port=_port(tmp_path,supervisor)
        try:
            await supervisor.start()
            await supervisor.wait_initial_readiness()
            port.prepare_tool_surface_safe_point()
            names={item.name for item in prepare_test_direct_tool_surface(port,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS).model_surface.tool_specs}
            assert {'query_capabilities','call_mcp_tool','read_mcp_resource'}<=names
            assert all(not item.startswith('mcp__') for item in names)
            services=await invoke(port,'query_capabilities',{'kind':'MCP'})
            record=services['content'][0]['value']
            assert record['target']=={'kind':'MCP_CONNECTION','config_path':str(path),'server_id':'notion'}
            directory=await invoke(port,'query_capabilities',{'kind':'MCP','target':record['target']})
            recorded=json.loads(fixture.with_name('notion_native_tools_20261020.json').read_text())['tools']
            assert len(directory['content'])==len(recorded)==65
            target=next(item['value']['target'] for item in directory['content']
                if item['value']['target']['remote_tool_name']=='notion_get_self')
            detail=await invoke(port,'query_capabilities',{'kind':'MCP','target':target})
            value=detail['content'][0]['value']
            original=next(item for item in recorded if item['name']=='notion_get_self')
            assert value['input_schema']==original['inputSchema']
            called=await invoke(port,'call_mcp_tool',{'target':value['invocation']['target'],'arguments':{}})
            assert called['meta']['status']=='SUCCESS'
            assert called['content']==[{'type':'text','value':'NOTION_SCHEMA_REPLAY_OK'}]
        finally:
            supervisor.stop_admission()
            closing=asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=10)
            await closing
    asyncio.run(scenario())


def test_tools_with_equal_native_aliases_keep_distinct_meta_targets_and_dispatch(tmp_path):
    async def scenario():
        called=[]
        async def listing(_ctx,_params):
            return types.ListToolsResult(tools=[types.Tool(name=name,input_schema={'type':'object'})
                for name in ('a-b','a_b')])
        async def call(_ctx,params):
            called.append(params.name)
            return types.CallToolResult(content=[types.TextContent(type='text',text=params.name)])
        async with _sdk_http(Server('aliases',on_list_tools=listing,on_call_tool=call)) as endpoint:
            supervisor=_supervisor(tmp_path,endpoint)
            port=_port(tmp_path,supervisor)
            try:
                await supervisor.start()
                await supervisor.wait_initial_readiness()
                port.prepare_tool_surface_safe_point()
                for name in ('a-b','a_b'):
                    detail=await invoke(port,'query_capabilities',{'kind':'MCP',
                        'target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':name}})
                    target=detail['content'][0]['value']['invocation']['target']
                    result=await invoke(port,'call_mcp_tool',{'target':target,'arguments':{}})
                    assert result['content']==[{'type':'text','value':name}]
                assert called==['a-b','a_b']
            finally:
                supervisor.stop_admission()
                closing=asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing
    asyncio.run(scenario())
