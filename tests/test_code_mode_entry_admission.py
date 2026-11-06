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


def _legacy_surface(port, native, directory, names):
    """Model an already-installed epoch without rewriting its retained schema."""
    from pulsara_agent.conversation_kernel.tool_surface import PreparedKernelToolSurface
    from pulsara_agent.model_input.contracts import FrozenModelToolSurface, model_tool_surface_fingerprint
    specs = tuple(sorted((*native.model_surface.tool_specs,
        *(spec for spec in directory.tool_specs if spec.name in names)), key=lambda spec: spec.name))
    bindings = tuple(directory.binding(spec.name) if spec.name in names else native.binding(spec.name) for spec in specs)
    model = FrozenModelToolSurface(ModelInputScopeKind.ROOT, specs,
        model_tool_surface_fingerprint(ModelInputScopeKind.ROOT, specs))
    retained = PreparedKernelToolSurface(model, bindings, native.access)
    port._prepared_surfaces[(native.access.surface_generation, ModelInputScopeKind.ROOT)] = retained
    return retained


@pytest.mark.parametrize('name', ['run_agent', 'wait_agent', 'decide'])
def test_retained_native_schema_rejected_even_with_self_reported_or_code_source(tmp_path, name):
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation

    async def scenario():
        port = _port(tmp_path)
        native = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        directory = port.freeze_code_mode_tool_directory(native, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        retained = _legacy_surface(port, native, directory, {name})
        before = retained.model_surface
        borrow = port.borrow_tool_surface(retained)
        try:
            arguments = {'source': 'CODE_MODE_CALL', 'condition': 'x', 'inputs': []}
            for source in (None, _source(name, arguments)):
                token = current_tool_invocation.set(source)
                try:
                    result = port.prepare_resolved_invocation(tool_name=name, arguments=arguments, surface_borrow=borrow)
                    assert result.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                    assert 'exec_cell' in result.authorization.public_message
                    auth = await port.authorize(tool_name=name, arguments=arguments, tool_call_id='call', turn_id='turn',
                        assistant_entry_id='entry', permission_snapshot=_permission(), surface_borrow=borrow,
                        memory_context=SimpleNamespace())
                    assert auth.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                    assert 'exec_cell' in auth.public_message
                finally:
                    current_tool_invocation.reset(token)
            assert retained.model_surface is before
        finally:
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('name,arguments', [
    ('run_agent', {'instruction': 'do this'}), ('wait_agent', {'targets': []}),
    ('run_agent.batch', {'items': []}), ('wait_agent.works', {'targets': []}),
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
        borrow = port.borrow_code_mode_surface(directory, cell_id=random_id('cell'), tool_name=name)
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
        arguments = {'targets': [{'kind': 'AGENT', 'agent_id': random_id('session')}]}
        try:
            prepared = port.prepare_resolved_invocation(tool_name='wait_agent', arguments=arguments, surface_borrow=borrow)
            assert isinstance(prepared, PreparedResolvedToolInvocation)
            auth = await port.authorize(tool_name='wait_agent', arguments=arguments, tool_call_id='call', turn_id='turn',
                assistant_entry_id='entry', permission_snapshot=_permission(), surface_borrow=borrow,
                memory_context=SimpleNamespace())
            assert auth.kind is KernelToolAuthorizationKind.ALLOW
            for name in ('decide', 'run_agent', 'stop_agent', 'exec_cell'):
                denied = port.prepare_resolved_invocation(tool_name=name, arguments={}, surface_borrow=borrow)
                assert denied.authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
        finally:
            borrow.close()
            await port.aclose()
    asyncio.run(scenario())
