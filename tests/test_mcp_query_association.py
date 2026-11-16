"""Public connection paths join actual local and Plugin source owners."""

import asyncio
from dataclasses import fields

import pytest
import yaml

from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.mcp_config import LocalConfiguredMcpRuntimeSource, ManagedPackageMcpRuntimeSource, McpLocalConfigSourceKind, _parse_server, freeze_mcp_server_config
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_capability_management_preparation import preparation, install_plugin


def saved_connection(service, *, scope='USER', enabled=False, subagents=False):
    raw={'enabled':enabled,'scope_policy':'ROOT_AND_SUBAGENTS' if subagents else 'ROOT_ONLY',
        'transport':{'type':'streamable_http','endpoint':'https://example.org/mcp'}}
    root=service.workspace_root if scope=='WORKSPACE' else None
    path=service.mcp.path(LocalMcpTarget('firecrawl',root))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(yaml.safe_dump({'servers':{'firecrawl':raw}}))
    return _parse_server('firecrawl',raw,runtime_source=LocalConfiguredMcpRuntimeSource(McpLocalConfigSourceKind(scope)))


def supervisor_for(service,config):
    return McpHostSupervisor(workspace_root=service.workspace_root,pulsara_home=service.plugins._store().home, user_config_path=service.mcp.user_config_path,
        configs=(config,),credential_boundary=ProcessCredentialBoundary())


async def query(service,runtime,arguments,*,scope=ModelInputScopeKind.ROOT):
    return await CapabilitySourceQuery(service,runtime=runtime,scope=scope,inspect_tool=None,
        mcp_configs=tuple(freeze_mcp_server_config(**{
            field.name: getattr(server.configuration, field.name)
            for field in fields(server.configuration)
        }) for server in runtime.catalog_for_scope(scope).servers)).execute(arguments)


def records(result):
    return [item['value'] for item in result['content'] if item['type']=='object']


@pytest.mark.parametrize('scope',['USER','WORKSPACE'])
@pytest.mark.parametrize('state',[McpServerState.DISABLED,McpServerState.CONNECTING,McpServerState.DISCOVERING,McpServerState.FAILED_RETRYABLE])
def test_undiscovered_local_connection_is_one_model_visible_item(tmp_path,scope,state):
    async def run():
        service=preparation(tmp_path)
        config=saved_connection(service,scope=scope,enabled=state is not McpServerState.DISABLED)
        supervisor=supervisor_for(service,config)
        supervisor._set_catalog_state_locked(config.server_id,state)
        runtime=supervisor.install_pending_at_safe_point()
        try:
            page=await query(service,runtime,{'kind':'MCP'})
            assert page['meta']['status']=='SUCCESS',page
            row,=records(page)
            root=service.workspace_root if scope=='WORKSPACE' else None
            assert row['target']=={'kind':'MCP_CONNECTION','config_path':str(service.mcp.path(LocalMcpTarget('firecrawl',root))),'server_id':'firecrawl'}
            assert 'resource_count' not in row
            detail=await query(service,runtime,{'target':row['target'],'kind':'MCP'})
            assert detail['meta']['status']=='ERROR'
            assert '目录未知' in detail['content'][0]['value']
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


def test_same_name_in_different_scopes_is_not_collapsed(tmp_path):
    async def run():
        service=preparation(tmp_path)
        saved_connection(service,scope='USER')
        config=saved_connection(service,scope='WORKSPACE')
        supervisor=supervisor_for(service,config)
        runtime=supervisor.install_pending_at_safe_point()
        try:
            rows=records(await query(service,runtime,{'kind':'MCP'}))
            assert len(rows)==2
            assert {item['target']['config_path'] for item in rows}=={
                str(service.mcp.user_config_path),str(service.workspace_root/'.pulsara/mcp.yaml')}
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


def test_host_override_remains_internal_and_does_not_hide_same_named_local_source(tmp_path):
    async def run():
        service=preparation(tmp_path)
        saved_connection(service)
        config=_parse_server('firecrawl',{'enabled':False,'transport':{'type':'stdio','command':'unused'}},
            runtime_source=LocalConfiguredMcpRuntimeSource(McpLocalConfigSourceKind.HOST_OVERRIDE))
        supervisor=supervisor_for(service,config)
        runtime=supervisor.install_pending_at_safe_point()
        try:
            row,=records(await query(service,runtime,{'kind':'MCP'}))
            assert row['target']['config_path']==str(service.mcp.user_config_path)
            assert row['target']['server_id']=='firecrawl'
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('same_install',[True,False])
def test_plugin_connection_uses_current_package_source(tmp_path,same_install):
    async def run():
        service=preparation(tmp_path)
        installed=await install_plugin(service,tmp_path)
        source=ManagedPackageMcpRuntimeSource('user','example',installed.package_install_id if same_install else 'package:'+'a'*26)
        config=_parse_server(framed_plugin_mcp_server_id('example','docs'),{'enabled':False,
            'transport':{'type':'streamable_http','endpoint':'https://example.org/mcp'}},runtime_source=source)
        supervisor=supervisor_for(service,config)
        runtime=supervisor.install_pending_at_safe_point()
        try:
            row,=records(await query(service,runtime,{'kind':'MCP'}))
            assert installed.package_install_id in row['target']['config_path']
            assert row['target']['server_id']=='docs'
            assert (await query(service,runtime,{'kind':'MCP','target':row['target']}))['meta']['status']=='ERROR'
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('subagents',[False,True])
def test_child_query_uses_its_actual_visible_scope(tmp_path,subagents):
    async def run():
        service=preparation(tmp_path)
        config=saved_connection(service,subagents=subagents)
        supervisor=supervisor_for(service,config)
        runtime=supervisor.install_pending_at_safe_point()
        try:
            page=await query(None,runtime,{'kind':'MCP'},scope=ModelInputScopeKind.SUBAGENT)
            assert page['meta']['status']=='SUCCESS'
            assert len(records(page))==int(subagents)
        finally:
            runtime.release()
            await supervisor.aclose()
            await service.mcp.aclose()
    asyncio.run(run())
