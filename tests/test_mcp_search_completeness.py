"""Search coverage follows scoped configured services and accepted discoveries."""
import asyncio
import sys

import pytest

from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import _parse_server, LocalConfiguredMcpRuntimeSource, McpLocalConfigSourceKind
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.support.mcp import finish_connection_attempts
from tests.test_mcp_first_request_readiness import controlled_mcp_client
from tests.test_mcp_meta_only_integration import invoke
from tests.test_mcp_schema_validation import _port
from tests.test_round6_mcp_production import _FakeMcpClient, _FakeMcpSession, types


def config(name, **values):
    source = values.pop('source', 'USER')
    return _parse_server(name, {'scope_policy':'ROOT_AND_SUBAGENTS',
        'catalog_refresh_interval_ms':'DISABLED',
        'effect_policy':{'default_effect':'READ_ONLY'},
        'transport':{'type':'stdio','command':sys.executable}, **values},
        runtime_source=LocalConfiguredMcpRuntimeSource(McpLocalConfigSourceKind(source)))


@pytest.mark.parametrize('query', ['unmatchedquasar', 'pending'])
@pytest.mark.parametrize(('enabled','scope_policy','caller','status'), [
    (True,'ROOT_AND_SUBAGENTS',ModelInputScopeKind.ROOT,'ERROR'),
    (False,'ROOT_AND_SUBAGENTS',ModelInputScopeKind.ROOT,'SUCCESS'),
    (True,'ROOT_ONLY',ModelInputScopeKind.SUBAGENT,'SUCCESS'),
    (True,'ROOT_AND_SUBAGENTS',ModelInputScopeKind.SUBAGENT,'ERROR'),
])
def test_unknown_directory_does_not_claim_complete_search(tmp_path,query,enabled,scope_policy,caller,status):
    async def run():
        owner=McpHostSupervisor(workspace_root=tmp_path,
            configs=(config('pending',enabled=enabled,scope_policy=scope_policy),),
            credential_boundary=ProcessCredentialBoundary())
        runtime=owner.install_pending_at_safe_point()
        try:
            result=await CapabilitySourceQuery(None,runtime=runtime,scope=caller,inspect_tool=None,
                mcp_configs=owner.configs).execute({'kind':'MCP','query':query})
            assert result['meta']['status']==status,result
            text='\n'.join(p['value'] for p in result['content'] if p['type']=='text')
            if status=='ERROR':
                assert 'pending' in text and '本次搜索不完整' in text
                assert '没有找到匹配的 MCP 工具或服务。' not in text
        finally:
            runtime.release();await owner.aclose()
    asyncio.run(run())


def test_partial_error_preserves_callable_ref_and_scope_filter(tmp_path):
    async def run():
        Blocked, entered, release=controlled_mcp_client('initialize')
        class Client(Blocked):
            async def open(self):
                if self.config.server_id=='z_pending':
                    return await super().open()
        owner=McpHostSupervisor(workspace_root=tmp_path,
            configs=(config('known'),config('z_pending',source='WORKSPACE')),
            credential_boundary=ProcessCredentialBoundary(),client_factory=Client)
        port=_port(tmp_path,owner)
        try:
            await owner.start();await entered.wait()
            assert not owner._tasks['z_pending'].done()
            port.prepare_tool_surface_safe_point()
            result=await invoke(port,'query_capabilities',{'kind':'MCP','query':'fake_echo'})
            assert result['meta']['status']=='ERROR',result
            record=next(p['value'] for p in result['content'] if p['type']=='object' and 'invocation' in p['value'])
            called=await invoke(port,'call_mcp_tool',{'target':record['invocation']['target'],'arguments':{'text':'known partial hit'}})
            assert called['meta']['status']=='SUCCESS',called
            assert called['content'][0]['value']=='fake-result'
            scoped=await invoke(port,'query_capabilities',{'kind':'MCP','scope':'USER','query':'fake_echo'})
            assert scoped['meta']['status']=='SUCCESS',scoped
            assert not owner._tasks['z_pending'].done()
            release.set();await finish_connection_attempts(owner)
            port.prepare_tool_surface_safe_point()
            complete=await invoke(port,'query_capabilities',{'kind':'MCP','query':'unmatchedquasar'})
            assert complete['meta']['status']=='SUCCESS',complete
            assert complete['content']==[{'type':'text','value':'没有找到匹配的 MCP 工具或服务。'}]
        finally:
            release.set();await port.aclose(timeout_seconds=10);await owner.aclose()
    asyncio.run(run())


def test_empty_discovered_directory_is_complete(tmp_path):
    async def run():
        class Session(_FakeMcpSession):
            async def list_tools(self,*,params=None):
                return types.ListToolsResult(tools=[])
        class Client(_FakeMcpClient):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);self.session=Session()
        owner=McpHostSupervisor(workspace_root=tmp_path,configs=(config('empty'),),
            credential_boundary=ProcessCredentialBoundary(),client_factory=Client)
        port=_port(tmp_path,owner)
        try:
            await owner.start();await finish_connection_attempts(owner);port.prepare_tool_surface_safe_point()
            result=await invoke(port,'query_capabilities',{'kind':'MCP','query':'unmatchedquasar'})
            assert result=={'meta':{'status':'SUCCESS'},'content':[{'type':'text','value':'没有找到匹配的 MCP 工具或服务。'}]}
        finally:
            await port.aclose(timeout_seconds=10);await owner.aclose()
    asyncio.run(run())


def test_retained_directory_survives_reconnect_failure_but_not_semantic_replacement(tmp_path):
    async def run():
        fail=False
        class Client(_FakeMcpClient):
            async def open(self):
                if fail: raise TimeoutError('controlled reconnect failure')
        owner=McpHostSupervisor(workspace_root=tmp_path,configs=(config('known'),),
            credential_boundary=ProcessCredentialBoundary(),client_factory=Client)
        port=_port(tmp_path,owner)
        try:
            await owner.start();await finish_connection_attempts(owner);port.prepare_tool_surface_safe_point()
            fail=True;owner.reconnect('known');await finish_connection_attempts(owner)
            live=owner.inspect_discovery_catalog(live=True)
            assert live.catalog_snapshot.servers[0].status.value=='FAILED_RETRYABLE'
            retained=await invoke(port,'query_capabilities',{'kind':'MCP','query':'fake_echo'})
            assert retained['meta']['status']=='SUCCESS',retained
            hit=next(p['value'] for p in retained['content'] if p['type']=='object')
            assert hit['invocation']=={'mode':'UNAVAILABLE'}
            owner.reload_configs((config('known',display_name='New semantics'),))
            result=await invoke(port,'query_capabilities',{'kind':'MCP','query':'fake_echo'})
            assert result['meta']['status']=='ERROR',result
            assert not any(p['type']=='object' and 'invocation' in p['value'] for p in result['content'])
        finally:
            await port.aclose(timeout_seconds=10);await owner.aclose()
    asyncio.run(run())


@pytest.mark.postgres
def test_js_partial_error_exposes_accepted_ref_that_calls_through_real_host(
    tmp_path,monkeypatch,stage2_migrated_postgres_database,
):
    import json
    import yaml
    from pulsara_agent.conversation_kernel import host as kernel_host
    from pulsara_agent.conversation_kernel.mcp import supervisor as mcp_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime,test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call,_text
    from tests.test_agent_code_mode_execution import _settled_formal_records

    home=tmp_path/'home';home.mkdir()
    monkeypatch.setenv('PULSARA_HOME',str(home))
    entry={'scope_policy':'ROOT_AND_SUBAGENTS','catalog_refresh_interval_ms':'DISABLED',
        'effect_policy':{'default_effect':'READ_ONLY'},'transport':{'type':'stdio','command':sys.executable}}
    (home/'mcp.yaml').write_text(yaml.safe_dump({'servers':{'known':entry,'z_pending':entry}}))
    code='''let partial;
try { await tools.query_capabilities({kind:"MCP",query:"fake_echo"}); }
catch (err) { partial=err.result; }
if (!partial || partial.meta.status!=="ERROR") throw Error("expected partial error");
const record=partial.content.find(p=>p.type==="object" && p.value.invocation).value;
const called=await tools.call_mcp_tool({target:record.invocation.target,arguments:{text:"from partial error"}});
console.log(JSON.stringify({partial,called}));'''
    async def stream(request):
        blocks=(_tool_call('exec_cell',random_id('call'),{'code':code}) if len(model.requests)==1
            else _text(random_id('block'),'ROOT_DONE'))
        for block in blocks: yield block
    model=CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host,'DirectKernelModelPort',lambda **_:model)
    runtime=test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def run():
        Blocked,entered,release=controlled_mcp_client('initialize')
        class Client(Blocked):
            async def open(self):
                if self.config.server_id=='z_pending': return await super().open()
        async def factory():return Client
        monkeypatch.setattr(mcp_module,'_prepare_sdk_client_factory',factory)
        core=KernelHostCore.production(model_runtime=runtime)
        try:
            session=await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=tmp_path))
            await session.update_model_call_binding(test_model_binding(runtime))
            await entered.wait()
            await session.run_turn(PromptContent.text('Run the query and use a known partial result.'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            records=await _settled_formal_records(session,expected=1)
            actual=json.loads(records[0]['text'])
            assert actual.get('partial',{}).get('meta',{}).get('status')=='ERROR',actual
            assert any('z_pending' in p['value'] and '本次搜索不完整' in p['value']
                for p in actual['partial']['content'] if p['type']=='text')
            assert actual['called']['meta']['status']=='SUCCESS'
            assert actual['called']['content'][0]['value']=='fake-result'
            assert not session._mcp_supervisor._tasks['z_pending'].done()
        finally:
            release.set();await core.shutdown()
    asyncio.run(run())


def test_same_named_source_cannot_borrow_another_scopes_accepted_directory(tmp_path):
    from tests.test_capability_management_preparation import preparation
    from tests.test_mcp_query_association import saved_connection
    async def run():
        service=preparation(tmp_path)
        saved_connection(service,scope='USER',enabled=True)
        selected=saved_connection(service,scope='WORKSPACE',enabled=True)
        owner=McpHostSupervisor(workspace_root=service.workspace_root,configs=(selected,),
            user_config_path=service.mcp.user_config_path,credential_boundary=ProcessCredentialBoundary(),
            client_factory=_FakeMcpClient)
        runtime=None
        try:
            await owner.start();await finish_connection_attempts(owner)
            runtime=owner.install_pending_at_safe_point()
            def inspect(executor):
                return {'target':{'kind':'MCP_TOOL','server_id':executor.semantic.server_id,
                    'remote_tool_name':executor.semantic.remote_tool_name},'invocation':{'mode':'UNAVAILABLE'}}
            for scope,expected in [('USER','ERROR'),('WORKSPACE','SUCCESS')]:
                result=await CapabilitySourceQuery(service,runtime=runtime,scope=ModelInputScopeKind.ROOT,
                    inspect_tool=inspect,mcp_configs=owner.configs).execute({'kind':'MCP','scope':scope,'query':'fake_echo'})
                assert result['meta']['status']==expected,result
                hits=[p for p in result['content'] if p['type']=='object' and 'invocation' in p['value']]
                assert bool(hits)==(scope=='WORKSPACE'),result
        finally:
            if runtime is not None:runtime.release()
            await owner.aclose();await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.postgres
def test_cancelled_source_query_is_saved_and_reported_as_cancelled(
    tmp_path,monkeypatch,stage2_migrated_postgres_database,
):
    import psycopg
    from pulsara_agent.conversation_kernel import host as kernel_host
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.llm.input import MessageRole, join_text_content
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime,test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call,_text
    async def cancelled_source(self,arguments):
        raise asyncio.CancelledError
    monkeypatch.setattr(CapabilitySourceQuery,'_mcp_servers',cancelled_source)
    observed=[]
    async def stream(request):
        if len(model.requests)==1:
            blocks=_tool_call('query_capabilities',random_id('call'),{'kind':'MCP','query':'search'})
        else:
            text=join_text_content(next(message for message in reversed(request.compiled_input.messages)
                if message.role is MessageRole.TOOL_RESULT).content)
            observed.append(text)
            blocks=_text(random_id('block'),'CANCELLED_RESULT_OBSERVED')
        for block in blocks:yield block
    model=CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host,'DirectKernelModelPort',lambda **_:model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService,'load_configs',lambda *a,**k:())
    monkeypatch.setenv('PULSARA_HOME',str(tmp_path/'home'))
    runtime=test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def run():
        core=KernelHostCore.production(model_runtime=runtime)
        try:
            session=await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=tmp_path))
            await session.update_model_call_binding(test_model_binding(runtime))
            result=await session.run_turn(PromptContent.text('Search capabilities.'))
            assert result.final_text=='CANCELLED_RESULT_OBSERVED'
            assert len(observed)==1 and 'CANCELLED' in observed[0] and '能力查询已取消' in observed[0]
            with psycopg.connect(stage2_migrated_postgres_database.runtime_dsn) as connection:
                rows=connection.execute('SELECT result_state FROM pulsara_v3.tool_results WHERE session_id=%s',
                    (session.session_id,)).fetchall()
                assert rows==[('CANCELLED',)]
        finally:await core.shutdown()
    asyncio.run(run())


def test_same_public_plugin_name_keeps_separate_coverage_and_parent(tmp_path):
    from pulsara_agent.identity import random_id
    from pulsara_agent.mcp_config import ManagedPackageMcpRuntimeSource
    from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id
    async def run():
        local=config('docs')
        plugin=_parse_server(framed_plugin_mcp_server_id('example','docs'),
            {'transport':{'type':'stdio','command':sys.executable}},
            runtime_source=ManagedPackageMcpRuntimeSource('user','example',random_id('package')))
        owner=McpHostSupervisor(workspace_root=tmp_path,pulsara_home=tmp_path/'home',configs=(local,plugin),
            credential_boundary=ProcessCredentialBoundary())
        runtime=owner.install_pending_at_safe_point()
        try:
            async def query(arguments):
                return await CapabilitySourceQuery(None,runtime=runtime,scope=ModelInputScopeKind.ROOT,
                    inspect_tool=None,mcp_configs=owner.configs).execute(arguments)
            result=await query({'kind':'MCP','query':'docs'})
            rows=[p['value'] for p in result['content'] if p['type']=='object']
            assert result['meta']['status']=='ERROR' and len(rows)==2,result
            assert {r['target']['server_id'] for r in rows}=={'docs'}
            assert len({r['target']['config_path'] for r in rows})==2
            text='\n'.join(p['value'] for p in result['content'] if p['type']=='text')
            assert 'docs（来自插件 example）' in text and 'docs、' in text,text
            selected=next(r['target'] for r in rows if 'mcp.json' in r['target']['config_path'])
            from pathlib import Path
            filtered=await query({'kind':'MCP','query':'docs',
                'parent':{'kind':'PLUGIN','plugin_path':str(Path(selected['config_path']).parent)}})
            assert filtered['meta']['status']=='ERROR',filtered
            filtered_rows=[p['value'] for p in filtered['content'] if p['type']=='object']
            assert [r['target'] for r in filtered_rows]==[selected]
        finally:
            runtime.release();await owner.aclose()
    asyncio.run(run())
