"""Native and Cell entry points select the same sealed execution owners."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from tests.support.round3 import prepare_test_direct_tool_surface
from pulsara_agent.identity import random_id

COLLABORATION = frozenset({'run_agent', 'query_agents', 'send_agent_message', 'wait_agent', 'stop_agent'})


def _port(tmp_path):
    port = DirectKernelToolPort(scheduled_tasks=object(), workspace_root=tmp_path,
        host_owner_id='entry-host', session_id='session:root', live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy())
    port.bind_subagent_port(SimpleNamespace(tool_names=COLLABORATION,
        validate_arguments=lambda **kw: None, bind_cell_observer=lambda owner: None))
    return port


def test_root_native_and_cell_directories_select_sealed_owners_independently(tmp_path):
    async def scenario():
        port = _port(tmp_path)
        try:
            surface = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            native = {spec.name for spec in surface.model_surface.tool_specs}
            assert native & COLLABORATION == {'query_agents', 'send_agent_message', 'stop_agent'}
            assert 'decide' not in native
            directory = port.freeze_code_mode_tool_directory(surface,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            code = {spec.name for spec in directory.tool_specs}
            assert COLLABORATION | {'decide'} <= code
            assert not code & {'exec_cell', 'query_cells', 'manage_cell'}
            assert tuple(x.name for x in surface.model_surface.tool_specs) == tuple(x.tool_name for x in surface.execution_bindings)
            assert tuple(x.name for x in directory.tool_specs) == tuple(x.tool_name for x in directory.execution_bindings)
            for name in ('query_agents', 'send_agent_message', 'stop_agent'):
                assert directory.binding(name) == surface.binding(name)
        finally:
            await port.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('scope', [ModelInputScopeKind.ROOT, ModelInputScopeKind.SUBAGENT])
@pytest.mark.parametrize('mode', list(PermissionMode))
def test_entry_matrix_by_role_and_permission(tmp_path, scope, mode):
    async def scenario():
        port = _port(tmp_path)
        try:
            surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=scope, effective_permission_mode=mode)
            names = {spec.name for spec in surface.model_surface.tool_specs}
            expected = ({'query_agents', 'send_agent_message', 'wait_agent'} if scope is ModelInputScopeKind.SUBAGENT
                else {'query_agents', 'send_agent_message', 'stop_agent'} if mode is PermissionMode.BYPASS_PERMISSIONS else set())
            assert names & COLLABORATION == expected
            assert 'decide' not in names
            if scope is not ModelInputScopeKind.ROOT or mode is not PermissionMode.BYPASS_PERMISSIONS:
                with pytest.raises(PermissionError):
                    port.freeze_code_mode_tool_directory(surface, effective_permission_mode=mode)
        finally:
            await port.aclose()
    asyncio.run(scenario())


def _permission(mode=PermissionMode.BYPASS_PERMISSIONS):
    from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
    return build_run_permission_snapshot(snapshot_id='permission:entry', requested_mode=mode, effective_mode=mode,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)


def _source(name, arguments):
    from pulsara_agent.conversation_kernel.tool_invocations import ToolInvocationSource, ToolInvocationSourceKind
    from pulsara_agent.identity import random_id
    from pulsara_agent.primitives.context import freeze_json
    return ToolInvocationSource(random_id('call'), ToolInvocationSourceKind.CODE_MODE_CALL, random_id('turn'),
        source_exec_attempt_id=random_id('attempt'), call_sequence=1, tool_name=name,
        original_arguments=freeze_json(arguments))






@pytest.mark.parametrize('name,arguments', [
    ('run_agent', {'members': [{'agent_id': random_id('session'), 'work_id': random_id('queue'), 'parameters': {'instruction': 'do this'}}]}),
    ('wait_agent', {'agents': [{'kind': 'AGENT', 'agent_id': random_id('session')}]}),
    ('wait_agent', {'works': [{'kind': 'WORK', 'work_id': random_id('queue')}]}),
    ('decide', {'condition': 'contains x', 'inputs': []}),
])
def test_code_entry_requires_both_host_source_and_cell_borrow(tmp_path, name, arguments):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind, PreparedResolvedToolInvocation
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation
    from pulsara_agent.identity import random_id

    async def scenario():
        port = _port(tmp_path)
        native = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        directory = port.freeze_code_mode_tool_directory(native, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        borrow = port.borrow_code_mode_surface(directory, cell_id=random_id('cell'))
        try:
            denied = port.prepare_resolved_invocation(tool_name=name, arguments=arguments, surface_borrow=borrow)
            assert denied.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            token = current_tool_invocation.set(_source(name, arguments))
            try:
                result = port.prepare_resolved_invocation(tool_name=name, arguments=arguments, surface_borrow=borrow)
                assert isinstance(result, PreparedResolvedToolInvocation)
            finally:
                current_tool_invocation.reset(token)
        finally:
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('runtime_state', ['unavailable', 'full'])
def test_native_controls_dispatch_directly_without_touching_code_runtime(tmp_path, runtime_state):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolInvocationContext, KernelToolResult
    from pulsara_agent.identity import random_id

    class InaccessibleRuntime:
        def __getattribute__(self, name):
            raise AssertionError(f'{runtime_state} Code Mode runtime must not be accessed: {name}')

    async def scenario():
        port = _port(tmp_path)
        calls = []
        async def invoke(**kwargs):
            calls.append((kwargs['tool_name'], kwargs['arguments']))
            return KernelToolResult('SUCCESS', b'completed by collaboration owner')
        port._subagent.invoke = invoke
        runtime = None
        release = asyncio.Event()
        if runtime_state == 'full':
            from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
            runtime = CodeModeHost()
            dispatched = 0
            full = asyncio.Event()
            async def hold(invocation):
                nonlocal dispatched
                dispatched += 1
                if dispatched == 4:
                    full.set()
                await release.wait()
                return CodeModeToolResult(value={})
            for _ in range(5):
                await runtime.start(source='await tools.read_file({});',
                    source_exec_attempt_id=random_id('attempt'), turn_id=random_id('turn'), snapshot=object(),
                    tool_catalog={'decide': {}, 'read_file': {}}, invoke=hold, decision_available=False)
            await asyncio.wait_for(full.wait(), 10)
            assert dispatched == 4
        # These controls must not queue behind the fifth Cell or start a failed runtime.
        port.bind_code_mode_port(runtime if runtime is not None else InaccessibleRuntime())
        surface = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        borrow = port.borrow_tool_surface(surface)
        try:
            target = {'kind': 'AGENT', 'agent_id': random_id('session')}
            for name, arguments in [('query_agents', {}), ('send_agent_message', {'target': target, 'message': 'adjust'}),
                                    ('stop_agent', {'target': target})]:
                permission = _permission()
                auth = await port.authorize(tool_name=name, arguments=arguments, tool_call_id='call', turn_id='turn',
                    assistant_entry_id='entry', permission_snapshot=permission, surface_borrow=borrow,
                    memory_context=SimpleNamespace())
                assert auth.kind.value == 'ALLOW', auth
                context = KernelToolInvocationContext(session_id='session:root', workspace_id='workspace', turn_id='turn',
                    assistant_entry_id='entry', tool_call_id='call', attempt_id='attempt', result_entry_id='result',
                    conversation_scope_kind='ROOT', host_owner_epoch=surface.access.owner_epoch,
                    authorization_reference=auth.reference, permission_snapshot_fingerprint=permission.snapshot_fingerprint,
                    effective_permission_mode=permission.effective_mode,
                    attempt_permission_snapshot_fingerprint=permission.snapshot_fingerprint, surface_borrow=borrow,
                    page_budget=ToolResultPageBudget('call'))
                result = await port.invoke(tool_name=name, arguments=arguments, tool_call_id='call', turn_id='turn',
                    assistant_entry_id='entry', attempt_id='attempt', invocation_context=context)
                assert result.state == 'SUCCESS'
            assert [name for name, _ in calls] == ['query_agents', 'send_agent_message', 'stop_agent']
            if runtime is not None:
                assert dispatched == 4
        finally:
            borrow.close()
            await port.aclose()
            release.set()
            if runtime is not None:
                await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', list(PermissionMode))
def test_decide_requires_bypass_even_with_authentic_cell_source(tmp_path, mode):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation
    from pulsara_agent.identity import random_id

    async def scenario():
        port = _port(tmp_path)
        native = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        directory = port.freeze_code_mode_tool_directory(native, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        borrow = port.borrow_code_mode_surface(directory, cell_id=random_id('cell'))
        arguments = {'condition': 'contains x', 'inputs': []}
        token = current_tool_invocation.set(_source('decide', arguments))
        try:
            auth = await port.authorize(tool_name='decide', arguments=arguments, tool_call_id='call', turn_id='turn',
                assistant_entry_id='entry', permission_snapshot=_permission(mode), surface_borrow=borrow,
                memory_context=SimpleNamespace())
            assert auth.kind is (KernelToolAuthorizationKind.ALLOW if mode is PermissionMode.BYPASS_PERMISSIONS
                                 else KernelToolAuthorizationKind.PERMISSION_DENIED)
        finally:
            current_tool_invocation.reset(token)
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())


def test_child_native_wait_keeps_shared_owner_without_cell_source(tmp_path):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind, PreparedResolvedToolInvocation
    from pulsara_agent.identity import random_id

    async def scenario():
        port = _port(tmp_path)
        surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        borrow = port.borrow_tool_surface(surface)
        arguments = {'agents': [{'kind': 'AGENT', 'agent_id': random_id('session')}]}
        try:
            prepared = port.prepare_resolved_invocation(tool_name='wait_agent', arguments=arguments, surface_borrow=borrow)
            assert isinstance(prepared, PreparedResolvedToolInvocation)
            auth = await port.authorize(tool_name='wait_agent', arguments=arguments, tool_call_id='call', turn_id='turn',
                assistant_entry_id='entry', permission_snapshot=_permission(), surface_borrow=borrow,
                memory_context=SimpleNamespace())
            assert auth.kind is KernelToolAuthorizationKind.ALLOW
            forbidden = {'works': [{'kind': 'WORK', 'work_id': random_id('queue')}]}
            denied = port.prepare_resolved_invocation(tool_name='wait_agent', arguments=forbidden, surface_borrow=borrow)
            assert denied.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            auth = await port.authorize(tool_name='wait_agent', arguments=forbidden, tool_call_id='call', turn_id='turn',
                assistant_entry_id='entry', permission_snapshot=_permission(), surface_borrow=borrow,
                memory_context=SimpleNamespace())
            assert auth.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            for name in ('decide', 'run_agent', 'stop_agent', 'exec_cell'):
                denied = port.prepare_resolved_invocation(tool_name=name, arguments={}, surface_borrow=borrow)
                assert denied.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
        finally:
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())


def test_wait_schema_is_projected_per_entry_and_old_targets_are_rejected(tmp_path):
    from jsonschema import Draft202012Validator
    from pulsara_agent.identity import random_id
    from pulsara_agent.primitives.context import thaw_json

    async def scenario():
        port = _port(tmp_path)
        try:
            root = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            directory = port.freeze_code_mode_tool_directory(root, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            child = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            js = next(spec for spec in directory.tool_specs if spec.name == 'wait_agent')
            native = next(spec for spec in child.model_surface.tool_specs if spec.name == 'wait_agent')
            agent = {'kind': 'AGENT', 'agent_id': random_id('session')}
            work = {'kind': 'WORK', 'work_id': random_id('queue')}
            for spec in (js, native):
                validator = Draft202012Validator(thaw_json(spec.parameters))
                assert validator.is_valid({'agents': [agent]})
                assert not validator.is_valid({'targets': [agent]})
                assert not validator.is_valid({'agents': [agent], 'works': [work]})
            assert Draft202012Validator(thaw_json(js.parameters)).is_valid({'works': [work]})
            assert not Draft202012Validator(thaw_json(native.parameters)).is_valid({'works': [work]})
            assert js.descriptor_fingerprint == directory.binding('wait_agent').descriptor_fingerprint
            assert native.descriptor_fingerprint == child.binding('wait_agent').descriptor_fingerprint
            assert js.descriptor_fingerprint != native.descriptor_fingerprint
        finally:
            await port.aclose()
    asyncio.run(scenario())


def test_run_schema_accepts_only_ordered_prepared_members():
    from jsonschema import Draft202012Validator
    from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
    from pulsara_agent.ports.tool_execution import thaw_tool_json_object
    from pulsara_agent.identity import random_id
    schema = thaw_tool_json_object(execution_catalog_entry('run_agent').descriptor.input_schema)
    validator = Draft202012Validator(schema)
    member = {'agent_id': random_id('session'), 'work_id': random_id('queue'),
              'parameters': {'instruction': 'inspect', 'work_title': 'Review'}}
    assert validator.is_valid({'members': [member]})
    assert not validator.is_valid({'instruction': 'inspect'})
    assert not validator.is_valid([{'instruction': 'inspect'}])
    assert not validator.is_valid({'members': []})
    assert not validator.is_valid({'members': [{**member, 'key': 'old'}]})
    assert not validator.is_valid({'members': [{**member, 'parameters': {'instruction': 'inspect', 'name': 'old'}}]})




@pytest.mark.parametrize('name', ['run_agent.batch', 'wait_agent.works'])
def test_removed_helpers_have_no_execution_binding_even_with_cell_authority(tmp_path, name):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation

    async def scenario():
        port = _port(tmp_path)
        try:
            native = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            directory = port.freeze_code_mode_tool_directory(native, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            borrow = port.borrow_code_mode_surface(directory, cell_id=random_id('cell'))
            try:
                token = current_tool_invocation.set(_source(name, {}))
                try:
                    rejection = port.prepare_resolved_invocation(tool_name=name, arguments={}, surface_borrow=borrow)
                    assert rejection.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                finally:
                    current_tool_invocation.reset(token)
            finally:
                borrow.close()
        finally:
            await port.aclose()
    asyncio.run(scenario())




def test_native_inventory_and_script_apis_have_distinct_role_owners():
    from pulsara_agent.capability.builtin_catalog import (
        builtin_tool_catalog, builtin_tool_catalog_entry, builtin_tool_descriptors,
        script_api_catalog_entry,
    )
    from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind, thaw_tool_json_object
    from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT, DEFAULT_SUBAGENT_SYSTEM_PROMPT

    assert not {'run_agent', 'decide'} & {d.name for d in builtin_tool_descriptors()}
    assert not {'run_agent', 'decide'} & {e.name for e in builtin_tool_catalog()}
    for name in ('run_agent', 'decide'):
        with pytest.raises(KeyError):
            builtin_tool_catalog_entry(name)
    native = builtin_tool_catalog_entry('wait_agent')
    script = script_api_catalog_entry('wait_agent')
    assert native.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.SUBAGENT,)
    assert script.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
    assert native.descriptor.is_model_callable and not script.descriptor.is_model_callable
    assert 'works' not in thaw_tool_json_object(native.descriptor.input_schema)['properties']
    assert not any(text in DEFAULT_SYSTEM_PROMPT for text in ('Subagents keep their native', 'native wait_agent'))
    assert not any(text in DEFAULT_SUBAGENT_SYSTEM_PROMPT for text in ('tools.run_agent', 'tools.decide', 'exec_cell'))
