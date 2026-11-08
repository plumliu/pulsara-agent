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

COLLABORATION = frozenset({'run_agent', 'query_agents', 'send_agent_message', 'wait_message', 'wait_agent', 'stop_agent'})


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
            assert (COLLABORATION - {'wait_message'}) | {'decide'} <= code
            assert 'wait_message' not in code
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
        from pulsara_agent.code_mode import CodeModeHost
        port.bind_code_mode_port(CodeModeHost())
        try:
            surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=scope, effective_permission_mode=mode)
            names = {spec.name for spec in surface.model_surface.tool_specs}
            expected = ({'query_agents', 'send_agent_message', 'wait_message'} if scope is ModelInputScopeKind.SUBAGENT
                else {'query_agents', 'send_agent_message', 'stop_agent'})
            assert names & COLLABORATION == expected
            assert 'decide' not in names
            assert bool({'exec_cell', 'query_cells', 'manage_cell'} <= names) == (scope is ModelInputScopeKind.ROOT)
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
    ('wait_agent', {'agents': [{'kind': 'AGENT', 'agent_id': random_id('session')}], 'until': 'work_end', 'settle': 'first', 'timeout_seconds': 0}),
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

    async def scenario():
        port = _port(tmp_path)
        surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        borrow = port.borrow_tool_surface(surface)
        try:
            prepared = port.prepare_resolved_invocation(tool_name='wait_message', arguments={}, surface_borrow=borrow)
            assert isinstance(prepared, PreparedResolvedToolInvocation)
            auth = await port.authorize(tool_name='wait_message', arguments={}, tool_call_id='call', turn_id='turn',
                assistant_entry_id='entry', permission_snapshot=_permission(), surface_borrow=borrow,
                memory_context=SimpleNamespace())
            assert auth.kind is KernelToolAuthorizationKind.ALLOW
            for name in ('wait_agent', 'wait_work', 'decide', 'run_agent', 'stop_agent', 'exec_cell'):
                denied = port.prepare_resolved_invocation(tool_name=name, arguments={}, surface_borrow=borrow)
                assert denied.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
        finally:
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())


def test_wait_schema_is_projected_per_entry_and_old_targets_are_rejected(tmp_path):
    from jsonschema import Draft202012Validator
    from pulsara_agent.primitives.context import thaw_json

    async def scenario():
        port = _port(tmp_path)
        try:
            root = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            directory = port.freeze_code_mode_tool_directory(root, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            child = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            agent = {'kind': 'AGENT', 'agent_id': random_id('session')}
            work = {'kind': 'WORK', 'work_id': random_id('queue')}
            for name, owner, accepted, rejected in (
                ('wait_message', child, {}, [{'agents': [agent]}, {'works': [work]}, {'settle': 'all'}]),
                ('wait_agent', directory, {'agents': [agent], 'until': 'work_end'}, [{}, {'works': [work]}, {'agents': [agent], 'works': [work]}, {'agents': [{'kind':'AGENT','agent_id':'ROOT'}]}, {'agents': [agent], 'until':'next'}]),
            ):
                specs = owner.model_surface.tool_specs if name == 'wait_message' else owner.tool_specs
                spec = next(item for item in specs if item.name == name)
                validator = Draft202012Validator(thaw_json(spec.parameters))
                assert validator.is_valid(accepted)
                assert all(not validator.is_valid(args) for args in rejected + [{'targets': [agent]}])
                assert spec.descriptor_fingerprint == owner.binding(name).descriptor_fingerprint
            assert not {'wait_agent', 'wait_work'} & {spec.name for spec in child.model_surface.tool_specs}
            assert 'wait_message' not in {spec.name for spec in directory.tool_specs}
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




@pytest.mark.parametrize('name', ['run_agent.batch', 'wait_agent.works', 'wait_work'])
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

    assert not {'run_agent', 'decide', 'wait_agent', 'wait_work'} & {d.name for d in builtin_tool_descriptors()}
    assert not {'run_agent', 'decide', 'wait_agent', 'wait_work'} & {e.name for e in builtin_tool_catalog()}
    for name in ('run_agent', 'decide', 'wait_agent', 'wait_work'):
        with pytest.raises(KeyError):
            builtin_tool_catalog_entry(name)
    native = builtin_tool_catalog_entry('wait_message')
    script = script_api_catalog_entry('wait_agent')
    assert native.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.SUBAGENT,)
    assert script.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
    assert native.descriptor.is_model_callable and not script.descriptor.is_model_callable
    assert 'works' not in thaw_tool_json_object(native.descriptor.input_schema)['properties']
    assert thaw_tool_json_object(script.descriptor.input_schema)['properties']['until']['default'] == 'idle'
    assert not any(text in DEFAULT_SYSTEM_PROMPT for text in ('Subagents keep their native', 'native wait_agent'))
    assert not any(text in DEFAULT_SUBAGENT_SYSTEM_PROMPT for text in ('tools.run_agent', 'tools.decide', 'exec_cell'))


@pytest.mark.parametrize('scope', [ModelInputScopeKind.ROOT, ModelInputScopeKind.SUBAGENT])
@pytest.mark.parametrize('mode', [mode for mode in PermissionMode if mode is not PermissionMode.BYPASS_PERMISSIONS])
def test_visible_control_tools_return_bypass_hint_without_execution(tmp_path, scope, mode):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from pulsara_agent.identity import random_id

    async def scenario():
        port = _port(tmp_path)
        from pulsara_agent.code_mode import CodeModeHost
        port.bind_code_mode_port(CodeModeHost())
        target = {'kind': 'AGENT', 'agent_id': random_id('session')}
        calls = {'query_agents': {}, 'send_agent_message': {'target': target, 'message': 'hello'}}
        if scope is ModelInputScopeKind.ROOT:
            calls.update({'stop_agent': {'target': target}, 'exec_cell': {'code': 'return 1;'},
                'query_cells': {}, 'manage_cell': {'action': 'wait', 'target': {'kind': 'CELL', 'cell_id': random_id('cell')}}})
        else:
            calls['wait_message'] = {}
        try:
            surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=scope, effective_permission_mode=mode)
            assert set(calls) <= {spec.name for spec in surface.model_surface.tool_specs}
            borrow = port.borrow_tool_surface(surface)
            try:
                for name, arguments in calls.items():
                    auth = await port.authorize(tool_name=name, arguments=arguments,
                        tool_call_id=random_id('call'), turn_id=random_id('turn'), assistant_entry_id=random_id('entry'),
                        permission_snapshot=_permission(mode), surface_borrow=borrow, memory_context=SimpleNamespace())
                    assert auth.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
                    assert 'only in bypass-permissions mode' in auth.public_message
            finally:
                borrow.close()
            assert not port._code_mode.snapshot_cell_index()
        finally:
            await port.aclose()
    asyncio.run(scenario())
