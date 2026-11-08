"""Recovered canonical Agent work uses the common cold provider boundary."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

import psycopg
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel import host as host_module
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.mcp import supervisor as mcp_module
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport, build_chat_completions_payload,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime as make_runtime
from tests.test_agent_session_host import eventually
from tests.test_mcp_first_request_readiness import controlled_mcp_client, readiness_config
from tests.test_round6_mcp_production import _FakeMcpClient, _FakeMcpSession

pytestmark = pytest.mark.postgres


def queue_rows(dsn, agent_id):
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.read_only = True
        return connection.execute('''SELECT id,session_id,queue_sequence,input_origin,
            source_session_id,initial_context,work_inputs,inline_content,blob_id,status,
            consumed_entry_id FROM pulsara_v3.prompt_queue_items
            WHERE session_id=%s AND input_origin='AGENT_INSTRUCTION' ORDER BY queue_sequence''',
            (agent_id,)).fetchall()


def queue_identity(rows):
    return tuple({key: value for key, value in row.items()
                  if key not in {'status', 'consumed_entry_id'}} for row in rows)


@pytest.mark.parametrize('phase', ['initialize', 'discovery'])
@pytest.mark.parametrize(('finish', 'root_only_ready'), [
    ('ready', True), ('ready', False), ('close', False),
])
def test_process_loss_pending_agent_waits_before_final_provider_and_preserves_scope(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, phase, finish, root_only_ready,
):
    async def scenario():
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        ready = tmp_path / 'ready.json'
        database = stage2_migrated_postgres_database.runtime_dsn
        log = (tmp_path / 'lost-process.log').open('w')
        # Reuse the existing real-loss fixture: one settled filesystem effect,
        # one interrupted running Work and one separately admitted pending Work.
        process = subprocess.Popen([
            sys.executable, '-m', 'tests.support.agent_process_loss', str(workspace),
            database, str(ready),
        ], stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[1])
        core = None
        child = None
        release_registration = asyncio.Event()
        release_root_only = asyncio.Event()
        Client, _entered, release = controlled_mcp_client(phase)
        child_phase_entered = asyncio.Event()
        clients = []
        requests, prefix_installs = [], []
        before_registration = asyncio.Event()
        child_waiting = asyncio.Event()
        provider_sent = asyncio.Event()
        original_stream = OpenAIChatCompletionsTransport.stream
        original_registration = KernelHostCore._register_prepared_session
        original_wait = mcp_module.McpHostSupervisor.wait_initial_readiness
        # Discovery has the existing one-reservation physical bound. Put an
        # already-ready ROOT_ONLY peer first; put the blocked peer after the
        # shared peer so it cannot prevent that peer from entering discovery.
        root_only_id = 'a_rootonly' if root_only_ready else 'rootonly'
        if root_only_ready:
            release_root_only.set()

        class ScopedClient(Client):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clients.append(self)
                if self.config.server_id == root_only_id:
                    self.session = _FakeMcpSession()
                else:
                    original_list = self.session.list_tools
                    async def list_tools(*, params=None):
                        if (phase == 'discovery' and params is not None
                                and child is not None
                                and asyncio.current_task() is child._mcp_supervisor._tasks['fixture']):
                            assert params.cursor == 'second-page'
                            child_phase_entered.set()
                        return await original_list(params=params)
                    self.session.list_tools = list_tools

            async def open(self):
                if self.config.server_id == root_only_id:
                    await release_root_only.wait()
                    return await _FakeMcpClient.open(self)
                if (phase == 'initialize' and child is not None
                        and asyncio.current_task() is child._mcp_supervisor._tasks['fixture']):
                    child_phase_entered.set()
                return await super().open()

        async def factory():
            return ScopedClient

        async def prepared():
            if process.poll() is not None:
                log.flush()
                raise AssertionError((tmp_path / 'lost-process.log').read_text())
            return ready.is_file()

        async def register(owner, session, capability_baseline):
            nonlocal child
            assert session._delivery_task is None
            assert session._cell_output_task is None
            assert session.host_session_id not in owner._sessions
            dispatch = session._runner._provider_dispatch
            install_provider_open = dispatch.install_provider_open

            async def installed(**kwargs):
                result = await install_provider_open(**kwargs)
                prefix_installs.append((session.session_id, result.request))
                return result

            monkeypatch.setattr(dispatch, 'install_provider_open', installed)
            if session.session_id == identities['agent_id']:
                child = session
                before_registration.set()
                await release_registration.wait()
            return await original_registration(owner, session, capability_baseline)

        async def wait(owner, **kwargs):
            if child is not None and owner is child._mcp_supervisor:
                assert kwargs['conversation_scope_kind'] is ModelInputScopeKind.SUBAGENT
                child_waiting.set()
            return await original_wait(owner, **kwargs)

        async def stream(adapter, *, call, context):
            if call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP:
                assert child is not None
                assert core._sessions[child.host_session_id] is child
                assert child._session_scope_kind is ModelInputScopeKind.SUBAGENT
                payload = build_chat_completions_payload(call=call, context=context)
                assert 'CHILD_SECOND' in json.dumps(payload['messages'])
                requests.append(payload)
                provider_sent.set()
            adapter._mock_chunks = [
                {'choices': [{'index': 0, 'delta': {'content': 'RECOVERED_AGENT_WORK_COMPLETED'}}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            ]
            async for item in original_stream(adapter, call=call, context=context):
                yield item

        try:
            await eventually(prepared)
            identities = json.loads(ready.read_text())
            process.kill()
            await asyncio.to_thread(process.wait, 10)
            assert process.returncode != 0
            original_rows = queue_rows(database, identities['agent_id'])
            assert [row['id'] for row in original_rows] == [
                identities['first_work_id'], identities['queued_work_id']]
            assert [row['status'] for row in original_rows] == ['CONSUMED', 'PENDING']
            assert (workspace / 'effect-count.txt').read_text() == 'x'
            monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'replacement-home'))
            monkeypatch.setattr(mcp_module, '_prepare_sdk_client_factory', factory)
            shared = readiness_config()
            from pulsara_agent.mcp_config import _parse_server
            root_only = _parse_server(root_only_id, {
                'catalog_refresh_interval_ms': 'DISABLED', 'scope_policy': 'ROOT_ONLY',
                'effect_policy': {'default_effect': 'READ_ONLY'},
                'transport': {'type': 'stdio', 'command': sys.executable},
            })
            monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs',
                                lambda *args, **kwargs: (shared, root_only))
            monkeypatch.setattr(KernelHostCore, '_register_prepared_session', register)
            monkeypatch.setattr(mcp_module.McpHostSupervisor, 'wait_initial_readiness', wait)
            monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)
            core = KernelHostCore.production(model_runtime=make_runtime(
                model_id='test-pro', wire_api='openai_chat_completions', postgres_dsn=database))
            root = await core.resume_session(identities['root_session_id'],
                workspace_input=HostWorkspaceInput(workspace_kind='project', workspace_root=workspace))
            await asyncio.wait_for(before_registration.wait(), 10)
            assert child.host_session_id not in core._sessions
            assert child._delivery_task is None and child._cell_output_task is None
            assert queue_rows(database, identities['agent_id'])[1]['status'] == 'PENDING'
            assert requests == [] and prefix_installs == []
            release_registration.set()
            await asyncio.wait_for(child_waiting.wait(), 10)
            await asyncio.wait_for(child_phase_entered.wait(), 5)
            assert core._sessions[child.host_session_id] is child
            assert child._delivery_task is not None and child._cell_output_task is not None
            progress = child.mcp_readiness_progress()
            assert progress is not None and progress['waiting'] is True
            assert {item['server_id'] for item in progress['servers']} == {'fixture'}
            assert progress['servers'][0]['state'] == ('CONNECTING' if phase == 'initialize' else 'DISCOVERING')
            assert requests == [] and prefix_installs == []
            for scope in child._input_continuity._slots:
                assert child._input_continuity.current_view(scope) is None
            attempt = child._mcp_supervisor._tasks['fixture']
            assert not attempt.done()
            if root_only_ready:
                root_only_attempt = child._mcp_supervisor._tasks[root_only_id]
                await asyncio.wait_for(asyncio.shield(root_only_attempt), 5)
                assert root_only_attempt.result().state is McpServerState.READY
            else:
                assert not child._mcp_supervisor._tasks[root_only_id].done()
            first = await root._io.run(root.repository.query_agent_work,
                root_session_id=root.session_id, work_id=identities['first_work_id'],
                deadline_monotonic=monotonic() + 10)
            assert first['status'] == 'stopped' and first['terminal_reason'] == 'HOST_TAKEOVER'
            if finish == 'ready':
                release.set()
                await asyncio.wait_for(provider_sent.wait(), 10)
                async def completed():
                    work = await root._io.run(root.repository.query_agent_work,
                        root_session_id=root.session_id, work_id=identities['queued_work_id'],
                        deadline_monotonic=monotonic() + 10)
                    return work['status'] == 'completed'
                await eventually(completed)
                assert len(requests) == len(prefix_installs) == 1
                assert prefix_installs[0][0] == identities['agent_id']
                names = {tool['function']['name'] for tool in requests[0]['tools']}
                assert 'mcp__fixture__fake_echo' in names
                assert not any(name.startswith(f'mcp__{root_only_id}__') for name in names)
                assert not names.intersection({'query_schedules', 'manage_schedule', 'run_agent'})
                if not root_only_ready:
                    # Actual Agent execution completed while ROOT_ONLY IO is
                    # still blocked; it did not enlarge this scope's wait set.
                    assert not child._mcp_supervisor._tasks[root_only_id].done()
            else:
                await core.shutdown()
                assert attempt.done()
                release.set()
                release_root_only.set()
                assert requests == [] and prefix_installs == []
            final_rows = queue_rows(database, identities['agent_id'])
            assert queue_identity(final_rows) == queue_identity(original_rows)
            assert (workspace / 'effect-count.txt').read_text() == 'x'
            with psycopg.connect(database, row_factory=dict_row) as connection:
                settled = connection.execute('''SELECT count(*) AS count
                    FROM pulsara_v3.tool_invocations i
                    JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=i.session_id
                      AND b.assistant_entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id
                    JOIN pulsara_v3.tool_results r ON r.session_id=i.session_id AND r.invocation_id=i.id
                    WHERE i.session_id=%s AND b.tool_name='exec_process' ''',
                    (identities['agent_id'],)).fetchone()['count']
                assert settled == 1
        finally:
            release_registration.set()
            release.set()
            release_root_only.set()
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
            log.close()
            if core is not None:
                await core.shutdown()
                assert not core.has_database_resources
                assert not core._sessions
            if child is not None:
                assert not child._mcp_supervisor._connection_tasks
            assert all(client.closed for client in clients)

    asyncio.run(scenario())
