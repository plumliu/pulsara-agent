"""First MCP attempt observation and real Host/provider cold preparation."""

import asyncio
from datetime import datetime, timezone
import json
import sys

import pytest

from pulsara_agent.conversation_kernel.mcp import supervisor as mcp_module
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.identity import random_id
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round6_mcp_production import _FakeMcpClient, _FakeMcpSession, types


def controlled_mcp_client(phase):
    """A real supervisor attempt blocked at initialize or its second tools page."""
    entered, release = asyncio.Event(), asyncio.Event()

    class Session(_FakeMcpSession):
        async def list_tools(self, *, params=None):
            if phase == 'discovery':
                if params is None:
                    return types.ListToolsResult(tools=[], nextCursor='second-page')
                assert params.cursor == 'second-page'
                entered.set()
                await release.wait()
            return await super().list_tools(params=params)

    class Client(_FakeMcpClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.session = Session()

        async def open(self):
            if phase == 'initialize':
                entered.set()
                await release.wait()

    return Client, entered, release


def readiness_config(**fields):
    return _parse_server('fixture', {
        'catalog_refresh_interval_ms': 'DISABLED',
        'effect_policy': {'default_effect': 'READ_ONLY'},
        'scope_policy': 'ROOT_AND_SUBAGENTS',
        'transport': {'type': 'stdio', 'command': sys.executable},
        **fields,
    })


def test_config_removal_releases_observer_before_old_physical_cleanup(tmp_path):
    async def scenario():
        entered, cleanup, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Client(_FakeMcpClient):
            async def open(self):
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                cleanup.set()
                await release_cleanup.wait()
                await super().aclose()

        owner = mcp_module.McpHostSupervisor(
            workspace_root=tmp_path, configs=(readiness_config(),),
            credential_boundary=ProcessCredentialBoundary(), client_factory=Client,
        )
        try:
            await owner.start()
            await entered.wait()
            waiting = asyncio.create_task(owner.wait_initial_readiness())
            await asyncio.sleep(0)
            assert owner.readiness_progress()['waiting'] is True
            owner.reload_configs(())
            await asyncio.wait_for(waiting, 1)
            await asyncio.wait_for(cleanup.wait(), 1)
            assert owner._connection_tasks
            assert owner.readiness_progress() is None
        finally:
            release_cleanup.set()
            await owner.aclose()
        assert not owner._connection_tasks

    asyncio.run(scenario())


def test_two_configuration_changes_during_adoption_do_not_expand_captured_wait(tmp_path):
    async def scenario():
        entered, never_ready = asyncio.Event(), asyncio.Event()
        changes = asyncio.Event()
        first_adoption, release_first = asyncio.Event(), asyncio.Event()
        second_adoption, release_second = asyncio.Event(), asyncio.Event()
        adoptions = []

        class Client(_FakeMcpClient):
            async def open(self):
                entered.set()
                await never_ready.wait()

        async def adopt():
            adoptions.append(len(adoptions) + 1)
            if len(adoptions) == 1:
                owner.reload_configs(())
                first_adoption.set()
                await release_first.wait()
            else:
                owner.reload_configs((readiness_config(transport={
                    'type': 'stdio', 'command': sys.executable, 'args': ['replacement'],
                }),))
                second_adoption.set()
                await release_second.wait()
            return True

        owner = mcp_module.McpHostSupervisor(
            workspace_root=tmp_path, configs=(readiness_config(),),
            credential_boundary=ProcessCredentialBoundary(), client_factory=Client,
            configuration_change=changes, adopt_configuration=adopt,
        )
        waiter = None
        try:
            await owner.start()
            await entered.wait()
            changes.set()
            waiter = asyncio.create_task(owner.wait_initial_readiness())
            await asyncio.wait_for(first_adoption.wait(), 1)
            changes.set()
            release_first.set()
            await asyncio.wait_for(second_adoption.wait(), 1)
            assert not waiter.done()
            release_second.set()
            await asyncio.wait_for(waiter, 1)
            assert adoptions == [1, 2]
            assert not changes.is_set()
            assert not owner._tasks['fixture'].done()
            assert owner._config_by_id['fixture'].transport.args == ('replacement',)
        finally:
            release_first.set()
            release_second.set()
            never_ready.set()
            if waiter is not None:
                await waiter
            await owner.aclose()

    asyncio.run(scenario())


def test_configuration_owner_failure_escapes_execution_preparation(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def scenario():
        host = SimpleNamespace(
            _lock=asyncio.Lock(), _capability_refresh_requested_revision=1,
            _capability_refresh_applied_revision=0,
            reload_capabilities=AsyncMock(side_effect=ValueError('invalid current MCP configuration')),
        )
        with pytest.raises(ValueError, match='invalid current MCP configuration'):
            await KernelHostSession._adopt_capabilities_if_requested(host, propagate_failure=True)
        assert host._capability_refresh_applied_revision == 0

    asyncio.run(scenario())


def test_subagent_readiness_does_not_wait_for_root_only_connection(tmp_path):
    async def scenario():
        Client, entered, release = controlled_mcp_client('initialize')
        owner = mcp_module.McpHostSupervisor(
            workspace_root=tmp_path, configs=(readiness_config(scope_policy='ROOT_ONLY'),),
            credential_boundary=ProcessCredentialBoundary(), client_factory=Client,
        )
        try:
            await owner.start()
            await entered.wait()
            await asyncio.wait_for(owner.wait_initial_readiness(
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT), 1)
            assert not owner._tasks['fixture'].done()
            assert owner.readiness_progress() is None
        finally:
            release.set()
            await owner.aclose()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('phase', ['initialize', 'discovery'])
@pytest.mark.parametrize('finish', ['ready', 'stop', 'close', 'disable'])
def test_restored_pending_waits_before_final_provider_and_prefix(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, phase, finish,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore, KernelHostSession
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport, build_chat_completions_payload,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        runtime = test_model_runtime(wire_api='openai_chat_completions',
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        workspace = HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path)
        original_stream = OpenAIChatCompletionsTransport.stream
        requests = []
        provider_sent = asyncio.Event()
        advisory_sent = asyncio.Event()

        async def stream(adapter, *, call, context):
            if call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP:
                requests.append(build_chat_completions_payload(call=call, context=context))
                provider_sent.set()
            elif call.fact.purpose is ModelCallPurpose.SESSION_TITLE:
                assert context.tools == ()
                advisory_sent.set()
            adapter._mock_chunks = [
                {'choices': [{'index': 0, 'delta': {'content': 'COMPLETED'}}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            ]
            async for value in original_stream(adapter, call=call, context=context):
                yield value

        monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)
        # Seed one accepted pending input without consuming it; restoration
        # below uses the normal production constructor and activation path.
        with monkeypatch.context() as seed:
            seed.setattr(host_module.LocalMcpManagementService, 'load_configs', lambda *a, **kw: ())
            seed.setattr(KernelHostSession, 'activate_execution', lambda self: None)
            core = KernelHostCore.production(model_runtime=runtime)
            session = await core.open_session(workspace)
            session_id = session.session_id
            command_id = random_id('command')
            await session.update_model_call_binding(test_model_binding(runtime))
            accepted = await session.submit_prompt(command_id=command_id,
                content=PromptContent.text('Preserve this accepted queued input.'))
            assert accepted.status == 'PENDING'
            assert not requests
            await core.shutdown()

        Client, entered, release = controlled_mcp_client(phase)

        async def factory():
            return Client

        monkeypatch.setattr(mcp_module, '_prepare_sdk_client_factory', factory)
        configured = [(readiness_config(),)]
        monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs',
            lambda *a, **kw: configured[0])
        core = KernelHostCore.production(model_runtime=runtime)
        session = None
        try:
            session = await asyncio.wait_for(core.resume_session(session_id, workspace_input=workspace), 10)
            assert core._sessions[session.host_session_id] is session
            assert session._delivery_task is not None
            await asyncio.wait_for(entered.wait(), 5)
            async with asyncio.timeout(10):
                while session.mcp_readiness_progress() is None:
                    await asyncio.sleep(0)
            assert session.mcp_readiness_progress()['waiting'] is True
            await asyncio.wait_for(advisory_sent.wait(), 10)
            scope = next(iter(session._input_continuity._slots))
            assert session._input_continuity.current_view(scope) is None
            assert requests == []
            # The same read-only preview remains usable and cannot consume or
            # bypass the execution wait, or install a provider prefix.
            await asyncio.wait_for(session.read_context_usage(), 10)
            assert session._input_continuity.current_view(scope) is None
            assert requests == []
            attempt = session._mcp_supervisor._tasks['fixture']
            if finish == 'stop':
                await session.request_stop_turn(command_id=random_id('command'),
                    admission_deadline_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 30_000,
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=session.active_root_turn_id())
                assert not attempt.done()
            elif finish == 'close':
                await core.close_session(session.host_session_id)
                assert attempt.done()
            elif finish == 'disable':
                configured[0] = ()
                await session.request_capability_refresh()
            if finish != 'disable':
                release.set()
            if finish in {'ready', 'disable'}:
                await asyncio.wait_for(provider_sent.wait(), 10)
                async with asyncio.timeout(10):
                    while (await session.query_command(command_id)).status == 'PENDING':
                        await asyncio.sleep(0)
                assert len(requests) == 1
                names = {item['function']['name'] for item in requests[0]['tools']}
                assert ('mcp__fixture__fake_echo' in names) is (finish == 'ready')
                if finish == 'ready':
                    # A later physical reconnect is not another cold boundary.
                    release.clear()
                    entered.clear()
                    session._mcp_supervisor.reconnect('fixture')
                    await asyncio.wait_for(entered.wait(), 5)
                    replacement = session._mcp_supervisor._tasks['fixture']
                    assert not replacement.done()
                    await asyncio.wait_for(session.run_turn(PromptContent.text('Continue the same installed epoch.'),
                        command_id=random_id('command')), 10)
                    assert len(requests) == 2
                    assert requests[1]['tools'] == requests[0]['tools']
                    assert requests[1]['messages'][:len(requests[0]['messages'])] == requests[0]['messages']
                    assert not replacement.done()
            else:
                if not attempt.done():
                    await asyncio.wait_for(attempt, 5)
                assert requests == []
                assert session._input_continuity.current_view(scope) is None
        finally:
            release.set()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_failure_then_ready_on_tool_continuation_keeps_actual_prefix_and_uses_meta(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport, build_chat_completions_payload,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        retry, retry_started = asyncio.Event(), asyncio.Event()
        attempts, effects, requests = [], [], []
        session = None

        class Session(_FakeMcpSession):
            async def call_tool(self, name, arguments, **kwargs):
                effects.append((name, arguments))
                return await super().call_tool(name, arguments, **kwargs)

        class Client(_FakeMcpClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.session = Session()

            async def open(self):
                attempts.append(self)
                if len(attempts) == 1:
                    raise TimeoutError('controlled first connection failure')
                retry_started.set()

        async def retry_after(owner, server_id, _delay):
            await retry.wait()
            owner._start_connect(server_id)

        async def factory():
            return Client

        monkeypatch.setattr(mcp_module.McpHostSupervisor, '_retry_after', retry_after)
        monkeypatch.setattr(mcp_module, '_prepare_sdk_client_factory', factory)
        monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs',
            lambda *a, **kw: (readiness_config(),))
        original_stream = OpenAIChatCompletionsTransport.stream

        async def stream(adapter, *, call, context):
            name = arguments = None
            if call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP:
                payload = build_chat_completions_payload(call=call, context=context)
                requests.append(payload)
                if len(requests) == 1:
                    assert session._mcp_supervisor.catalog_snapshot().servers[0].status is McpServerState.FAILED_RETRYABLE
                    retry.set()
                    await retry_started.wait()
                    await session._mcp_supervisor._tasks['fixture']
                    name = 'query_processes'
                    arguments = {}
                elif len(requests) == 2:
                    name = 'query_capabilities'
                    arguments = {'target': {'kind': 'MCP_TOOL', 'server_id': 'fixture', 'tool_name': 'mcp__fixture__fake_echo'}}
                elif len(requests) == 3:
                    result = next(message for message in reversed(payload['messages']) if message['role'] == 'tool')
                    envelope = json.JSONDecoder().raw_decode(result['content'])[0]
                    detail = json.JSONDecoder().raw_decode(envelope['pulsara_tool_result']['body'])[0]
                    assert 'invocation' in detail, detail
                    assert detail['invocation']['mode'] == 'META'
                    name = 'use_new_mcp_tool'
                    arguments = {'target': detail['invocation']['target'], 'arguments': {'text': 'late MCP'}}
                else:
                    assert len(requests) == 4
                    assert 'fake-result' in json.dumps(payload['messages'], ensure_ascii=False)
            if name is None:
                chunks = [{'choices': [{'index': 0, 'delta': {'content': 'DONE'}}]},
                          {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
            else:
                chunks = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{
                    'index': 0, 'id': f'call_{len(requests)}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)},
                }]}}]}, {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}]
            adapter._mock_chunks = chunks
            async for value in original_stream(adapter, call=call, context=context):
                yield value

        monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)
        runtime = test_model_runtime(wire_api='openai_chat_completions',
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        core = KernelHostCore.production(model_runtime=runtime)
        try:
            session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text('Use the late MCP through its catalog.'), command_id=random_id('command'))
            assert result.final_text == 'DONE'
            assert len(requests) == 4
            assert effects == [('fake_echo', {'text': 'late MCP'})]
            assert not any(tool['function']['name'].startswith('mcp__') for tool in requests[0]['tools'])
            tools_bytes = json.dumps(requests[0]['tools'], sort_keys=True, separators=(',', ':')).encode()
            first_system = tuple(message for message in requests[0]['messages'] if message['role'] in {'system', 'developer'})
            system_bytes = json.dumps(first_system, sort_keys=True, separators=(',', ':')).encode()
            for before, after in zip(requests, requests[1:]):
                assert json.dumps(after['tools'], sort_keys=True, separators=(',', ':')).encode() == tools_bytes
                system = tuple(message for message in after['messages'] if message['role'] in {'system', 'developer'})
                assert json.dumps(system, sort_keys=True, separators=(',', ':')).encode() == system_bytes
                assert after['messages'][:len(before['messages'])] == before['messages']
                assert len(after['messages']) > len(before['messages'])
        finally:
            retry.set()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('phase', ['initialize', 'discovery'])
def test_new_child_canonical_work_uses_its_own_cold_readiness_scope(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, phase,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport, build_chat_completions_payload,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        ChildClient, entered, release = controlled_mcp_client(phase)
        preparations = []

        async def factory():
            preparations.append(True)
            return _FakeMcpClient if len(preparations) == 1 else ChildClient

        monkeypatch.setattr(mcp_module, '_prepare_sdk_client_factory', factory)
        monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs',
            lambda *a, **kw: (readiness_config(),))
        original_stream = OpenAIChatCompletionsTransport.stream
        root_requests, child_requests = [], []
        child_sent = asyncio.Event()

        async def stream(adapter, *, call, context):
            chunks = [{'choices': [{'index': 0, 'delta': {'content': 'DONE'}}]},
                      {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
            if call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP:
                payload = build_chat_completions_payload(call=call, context=context)
                names = {item['function']['name'] for item in payload['tools']}
                if 'exec_cell' in names:
                    root_requests.append(payload)
                    if len(root_requests) == 1:
                        chunks = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{
                            'index': 0, 'id': 'delegate_child', 'type': 'function',
                            'function': {'name': 'exec_cell', 'arguments': json.dumps({'code': 'await tools.run_agent({instruction: "CHILD_MCP_ONLY"});'})},
                        }]}}]}, {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}]
                else:
                    child_requests.append(payload)
                    child_sent.set()
            adapter._mock_chunks = chunks
            async for value in original_stream(adapter, call=call, context=context):
                yield value

        monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)
        runtime = test_model_runtime(wire_api='openai_chat_completions',
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        core = KernelHostCore.production(model_runtime=runtime)
        try:
            root = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
            await root.update_model_call_binding(test_model_binding(runtime))
            await root.run_turn(PromptContent.text('Delegate one independent task.'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(entered.wait(), 5)
            async with asyncio.timeout(10):
                while True:
                    child = next((item for item in core._sessions.values() if item.root_session_id == root.session_id), None)
                    if child is not None and child.mcp_readiness_progress() is not None:
                        break
                    await asyncio.sleep(0)
            assert child._delivery_task is not None
            scope = next(iter(child._input_continuity._slots))
            assert scope.scope_kind is ModelInputScopeKind.SUBAGENT
            assert child._input_continuity.current_view(scope) is None
            assert child_requests == []
            assert len(root_requests) == 2
            root_names = {item['function']['name'] for item in root_requests[0]['tools']}
            assert {'exec_cell', 'query_agents', 'send_agent_message', 'stop_agent'} <= root_names
            assert not {'run_agent', 'wait_agent'} & root_names
            release.set()
            await asyncio.wait_for(child_sent.wait(), 10)
            assert len(child_requests) == 1
            names = {item['function']['name'] for item in child_requests[0]['tools']}
            assert 'mcp__fixture__fake_echo' in names
            assert 'manage_schedule' not in names and 'run_agent' not in names
        finally:
            release.set()
            await core.shutdown()

    asyncio.run(scenario())
