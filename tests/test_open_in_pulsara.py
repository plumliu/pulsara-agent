import asyncio
import json

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.llm.adapters.openai.function_tools import openai_chat_function_tool, openai_responses_function_tool
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource, build_run_permission_snapshot
from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_contracts import PreparedToolPreparationRejection
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool
from tests.support.round3 import direct_tool_invocation_context, prepare_test_direct_tool_surface


@pytest.mark.parametrize('arguments,rejected', [
    ({'targets': []}, True), ({'targets': 'file'}, True), ({}, True),
    ({'targets': [{'type': 'panel', 'name': 'tasks'}], 'session_id': 'other'}, True),
    ({'targets': [None, {'type': 'file', 'path': 'missing #?.md'}, {'type': 'browser', 'url': 'file:///tmp/a'}, {'type': 'terminal', 'process_id': 'p1'}, {'type': 'panel', 'name': 'tasks'}]}, False),
    ({'targets': [None, {'type': 'file', 'path': ''}]}, False),
    ({'targets': [{'type': 'panel', 'name': 'browser'}]}, False),
    ({'targets': [None], 'session_id': 'other'}, True),
])
def test_real_tool_gate_and_invocation(tmp_path, arguments, rejected):
    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id='session:test', tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
        borrow, context = direct_tool_invocation_context(port, **ids)
        try:
            prepared = port.prepare_resolved_invocation(tool_name='open_in_pulsara', arguments=arguments, surface_borrow=borrow)
            assert isinstance(prepared, PreparedToolPreparationRejection) is rejected
            permission = build_run_permission_snapshot(
                snapshot_id='permission:test', requested_mode=PermissionMode.BYPASS_PERMISSIONS,
                effective_mode=PermissionMode.BYPASS_PERMISSIONS,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            authorization = await port.authorize(
                tool_name=ids['tool_name'], arguments=arguments, tool_call_id=ids['tool_call_id'],
                turn_id=ids['turn_id'], assistant_entry_id=ids['assistant_entry_id'],
                permission_snapshot=permission, surface_borrow=borrow,
                memory_context=FrozenModelCallMemoryContext(),
            )
            assert authorization.kind is (KernelToolAuthorizationKind.INVALID_ARGUMENTS if rejected else KernelToolAuthorizationKind.ALLOW)
            if rejected:
                return
            result = await port.invoke(**{k: v for k, v in ids.items() if k != 'session_id'}, arguments=arguments, invocation_context=context)
            assert result.state == 'SUCCESS'
            value = json.loads(result.content)
            if arguments['targets'] == [{'type': 'panel', 'name': 'browser'}]:
                assert value == {'targets': [{'type': 'panel', 'name': 'browser'}]}
            elif len(arguments['targets']) == 2:
                assert value['targets'] == []
                assert [e['index'] for e in value['errors']] == [0, 1]
            else:
                assert [e['index'] for e in value['errors']] == [0, 2]
                assert value['targets'] == [
                    {'type': 'file', 'path': str(tmp_path / 'missing #?.md')},
                    {'type': 'terminal', 'process_id': 'p1', 'host_session_id': 'host:test'},
                    {'type': 'panel', 'name': 'tasks'},
                ]
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_root_surface_and_recovery(tmp_path):
    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        try:
            prepared = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK, scope_subagent_task_id='task:test')
            borrow = port.borrow_tool_surface(prepared)
            try:
                with pytest.raises((RuntimeError, KeyError)):
                    borrow.execution_binding('open_in_pulsara')
            finally:
                borrow.close()
            assert builtin_tool_catalog_entry('open_in_pulsara').recovery_contract.severity == 'read_only'
        finally:
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_normalization_does_not_read_or_resolve_links(tmp_path):
    tool = OpenInPulsaraTool(tmp_path, host_session_id='host:test')
    (tmp_path / 'link').symlink_to(tmp_path / 'absent')
    assert tool.normalize_target({'type': 'file', 'path': 'link/a.md'})['path'] == str(tmp_path / 'link/a.md')
    with pytest.raises(ValueError) as failure:
        tool.normalize_target({'type': 'browser', 'url': 'https://username:secret@example.test/'})
    assert 'secret' not in str(failure.value)
    assert tool.normalize_target({'type': 'browser', 'url': ' https://example.test/a#b '})['url'] == 'https://example.test/a#b'
    with pytest.raises(ValueError):
        tool.normalize_target({'type': 'panel', 'name': 'tasks', 'session_id': 'other'})


def test_clean_import_order():
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c", "from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool"], check=True)


def test_real_tool_opens_exact_task_without_model_supplied_session_or_host(tmp_path):
    async def run():
        task_id = 'task:6d7i5mg4ymfcckogzrqiser5km'
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id='session:test', tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
        borrow, context = direct_tool_invocation_context(port, **ids)
        arguments = {'targets': [
            {'type': 'task', 'task_id': task_id},
            {'type': 'task', 'task_id': task_id, 'session_id': 'other'},
            {'type': 'task', 'task_id': 'invented'},
            {'type': 'task'},
            {'type': 'unknown', 'name': 'tasks'},
            {'type': 'panel', 'name': 'tasks'},
        ]}
        try:
            assert not isinstance(port.prepare_resolved_invocation(tool_name='open_in_pulsara', arguments=arguments, surface_borrow=borrow), PreparedToolPreparationRejection)
            result = await port.invoke(**{k: v for k, v in ids.items() if k != 'session_id'}, arguments=arguments, invocation_context=context)
            value = json.loads(result.content)
            assert value['targets'] == [{'type': 'task', 'task_id': task_id}, {'type': 'panel', 'name': 'tasks'}]
            assert [error['index'] for error in value['errors']] == [1, 2, 3, 4]
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


@pytest.mark.parametrize('wire_builder', [openai_chat_function_tool, openai_responses_function_tool])
def test_wire_defines_all_target_types_and_fields(wire_builder):
    descriptor = builtin_tool_catalog_entry('open_in_pulsara').descriptor
    wire = wire_builder(ToolSpec(
        name=descriptor.name, description=descriptor.description,
        parameters=thaw_tool_json_object(descriptor.input_schema),
    ))
    definition = wire.get('function', wire)
    items = definition['parameters']['properties']['targets']['items']
    branches = {branch['properties']['type']['enum'][0]: branch for branch in items['anyOf']}
    fields = {'file': 'path', 'browser': 'url', 'terminal': 'process_id',
              'task': 'task_id', 'mcp': 'runtime_server_id', 'panel': 'name'}
    assert branches.keys() == fields.keys()
    validator = Draft202012Validator(definition['parameters'])
    for kind, field in fields.items():
        branch = branches[kind]
        assert set(branch['properties']) == set(branch['required']) == {'type', field}
        assert branch['additionalProperties'] is False
        assert branch['properties'][field]['type'] == 'string'
        target = {'type': kind, field: 'tasks' if kind == 'panel' else 'example'}
        assert validator.is_valid({'targets': [target]})
        assert not validator.is_valid({'targets': [{'type': kind}]})
        assert not validator.is_valid({'targets': [{**target, field: None}]})
        assert not validator.is_valid({'targets': [{**target, 'session_id': 'other'}]})
    assert branches['panel']['properties']['name']['enum'] == ['capabilities', 'tasks', 'background', 'browser']
