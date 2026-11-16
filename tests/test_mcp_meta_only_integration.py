"""Real SDK RPCs through native and Cell borrowers share one session REF."""
from tests.support.mcp import finish_connection_attempts

import asyncio
import json

from jsonschema import Draft202012Validator

from pulsara_agent.capability.source_query import (
    call_mcp_tool_output_schema, query_output_schema, read_mcp_resource_output_schema,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind, ProcessLocalEffectSettlementDisposition
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
from tests.support.round3 import prepare_test_direct_tool_surface, _UnavailableTestDecisionPort
from tests.test_mcp_rpc_errors_and_pagination import _config
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from tests.support.model_config import scheduled_task_service
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolInvocationContext
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


async def invoke(port, name, args, *, cell=None, directory=None, scope=ModelInputScopeKind.ROOT):
    permission = build_run_permission_snapshot(snapshot_id=random_id('permission'),
        requested_mode=PermissionMode.BYPASS_PERMISSIONS, effective_mode=PermissionMode.BYPASS_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
    surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=scope, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
    borrow = port.borrow_tool_surface(surface) if cell is None else port.borrow_code_mode_surface(directory, cell_id=cell)
    context = KernelToolInvocationContext(session_id=port._session_id, workspace_id=random_id('workspace'),
        turn_id=random_id('turn'), assistant_entry_id=random_id('entry'), tool_call_id=random_id('call'),
        attempt_id=random_id('attempt'), result_entry_id=random_id('entry'), conversation_scope_kind=scope.value,
        host_owner_epoch=1, authorization_reference='test:authorized',
        permission_snapshot_fingerprint=permission.snapshot_fingerprint,
        attempt_permission_snapshot_fingerprint=permission.snapshot_fingerprint,
        effective_permission_mode=permission.effective_mode, surface_borrow=borrow, code_cell_id=cell)
    try:
        auth = await port.authorize(tool_name=name, arguments=args, tool_call_id=context.tool_call_id,
            turn_id=context.turn_id, assistant_entry_id=context.assistant_entry_id,
            permission_snapshot=permission, surface_borrow=borrow, memory_context=context.memory_context)
        assert auth.kind is KernelToolAuthorizationKind.ALLOW, auth
        result = await port.invoke(tool_name=name, arguments=args, tool_call_id=context.tool_call_id,
            attempt_id=context.attempt_id, turn_id=context.turn_id, assistant_entry_id=context.assistant_entry_id,
            invocation_context=context)
        if result.process_local_settlement is not None:
            await port.settle_process_local_effect(result.process_local_settlement, ProcessLocalEffectSettlementDisposition.COMMITTED)
        value = result.public_value()
        if name == 'query_capabilities':
            schema = query_output_schema()
        elif name == 'call_mcp_tool':
            schema = call_mcp_tool_output_schema()
        else:
            assert name == 'read_mcp_resource'
            schema = read_mcp_resource_output_schema()
        Draft202012Validator(schema).validate(value)
        return value
    finally:
        borrow.close()


def test_native_ref_calls_from_multiple_cells_and_resource_directory_does_not_read_bodies(tmp_path):
    async def scenario():
        config = _config(tmp_path, {'scope_policy':'ROOT_AND_SUBAGENTS'})
        supervisor = McpHostSupervisor(workspace_root=tmp_path, configs=(config,), credential_boundary=ProcessCredentialBoundary())
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path,
            host_owner_id=random_id('host'), session_id=random_id('session'), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_mcp_supervisor(supervisor)
        port.bind_interaction_port(object())
        port.bind_subagent_port(type('EmptySubagent', (), {'tool_names': ()})())
        port.bind_memory_port(type('EmptyMemory', (), {'tool_names': ()})())
        port.bind_decision_port(_UnavailableTestDecisionPort())
        port.bind_capability_reload_port(object())
        port.seal_builtin_composition()
        child_workspace = tmp_path / 'child'
        child_workspace.mkdir()
        child_config = _config(child_workspace, {'scope_policy':'ROOT_AND_SUBAGENTS'})
        child_supervisor = McpHostSupervisor(workspace_root=child_workspace,
            configs=(child_config,), credential_boundary=ProcessCredentialBoundary())
        child = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=child_workspace,
            host_owner_id=random_id('host'), session_id=random_id('session'), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        child.bind_mcp_supervisor(child_supervisor)
        try:
            await supervisor.start()
            await finish_connection_attempts(supervisor)
            port.prepare_tool_surface_safe_point()
            detail = await invoke(port, 'query_capabilities', {'target': {'kind':'MCP_TOOL','server_id':'probe','remote_tool_name':'first'},'kind':'MCP'})
            record = detail['content'][0]['value']
            assert record['input_schema'] == {'type':'object'}
            surface = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert all(not item.name.startswith('mcp__') for item in surface.model_surface.tool_specs)
            directory = port.freeze_code_mode_tool_directory(surface, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            for cell in (random_id('cell'), random_id('cell')):
                value = await invoke(port, 'call_mcp_tool', {'target':record['invocation']['target'],'arguments':{}}, cell=cell, directory=directory)
                assert value['meta']['status']=='SUCCESS'
                assert value['content'][0]['value']=='same connection success'
            listed = await invoke(port,'query_capabilities',{'kind':'MCP'})
            connection = listed['content'][0]['value']['target']
            await invoke(port,'query_capabilities',{'kind':'MCP','target':connection})
            await invoke(port,'query_capabilities',{'kind':'MCP','query':'first'})
            resources = await invoke(port,'read_mcp_resource',{'target':connection})
            first = resources['content'][0]['value']
            assert first['target']=={'kind':'MCP_RESOURCE','server_id':'probe','uri':'fixture://first'}
            before = json.loads((tmp_path/'requests.json').read_text())
            assert not any(item['method']=='resources/read' for item in before)
            read = await invoke(port,'read_mcp_resource',{'target':first['target']})
            assert read['meta']['status']=='SUCCESS', read
            assert read['content'][1]['value']=='same connection success', read
            after = json.loads((tmp_path/'requests.json').read_text())
            assert sum(item['method']=='tools/call' for item in after)==2
            assert sum(item['method']=='resources/read' for item in after)==1
            await child_supervisor.start()
            await finish_connection_attempts(child_supervisor)
            child.prepare_tool_surface_safe_point()
            child_detail = await invoke(child, 'query_capabilities',
                {'kind':'MCP','target':{'kind':'MCP_TOOL','server_id':'probe','remote_tool_name':'first'}},
                scope=ModelInputScopeKind.SUBAGENT)
            assert any(part['type']=='object' and 'invocation' in part['value'] for part in child_detail['content']), child_detail
            child_record = next(part['value'] for part in child_detail['content']
                if part['type']=='object' and 'invocation' in part['value'])
            assert child_record['invocation']['target'] != record['invocation']['target']
            child_result = await invoke(child, 'call_mcp_tool',
                {'target':child_record['invocation']['target'],'arguments':{}}, scope=ModelInputScopeKind.SUBAGENT)
            assert child_result['meta']['status']=='SUCCESS'
            assert child_result['content'][0]['value']=='same connection success'
            requests = json.loads((child_workspace/'requests.json').read_text())
            assert sum(item['method']=='tools/call' for item in requests)==1
            # Discovery is once per owning session; querying does not relist the peer.
            assert sum(item['method']=='tools/list' for item in requests)==1
        finally:
            supervisor.stop_admission()
            closing=asyncio.create_task(supervisor.aclose())
            child_supervisor.stop_admission()
            child_closing = asyncio.create_task(child_supervisor.aclose())
            await child.aclose(timeout_seconds=10)
            await child_closing
            await port.aclose(timeout_seconds=10)
            await closing
    asyncio.run(scenario())
