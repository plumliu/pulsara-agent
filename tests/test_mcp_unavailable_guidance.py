"""Tool inspection preserves actual schemas rather than exposing source internals."""

import asyncio

from mcp.server.lowlevel import Server
import mcp_types as types

from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor
from tests.test_mcp_schema_validation import _port
from tests.test_mcp_meta_only_integration import invoke


def test_real_mcp_tool_query_preserves_package_named_schema_fields(tmp_path):
    async def scenario():
        schema={'type':'object','properties':{
            'package_install_id':{'type':'string','default':'ordinary'},
            'options':{'const':{'package_install_id':'literal-value'}}}}
        async def listing(_ctx,_params):
            return types.ListToolsResult(tools=[types.Tool(name='inspect',input_schema=schema)])
        async with _sdk_http(Server('schema-catalog',on_list_tools=listing)) as endpoint:
            supervisor=_supervisor(tmp_path,endpoint)
            port=_port(tmp_path,supervisor)
            try:
                await supervisor.start()
                await supervisor.wait_initial_readiness()
                port.prepare_tool_surface_safe_point()
                result=await invoke(port,'query_capabilities',{'kind':'MCP',
                    'target':{'kind':'MCP_TOOL','server_id':'catalog','remote_tool_name':'inspect'}})
                assert result['meta']['status']=='SUCCESS'
                assert result['content'][0]['value']['input_schema']==schema
            finally:
                supervisor.stop_admission()
                closing=asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing
    asyncio.run(scenario())
