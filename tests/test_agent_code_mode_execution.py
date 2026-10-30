"""Shared execution facts, delivery and physical-owner behavior."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_invocations import ToolInvocationSource, ToolInvocationSourceKind
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.ports.tool_execution import ToolCall, ToolOutputSourceFormatHint
from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.builtins.filesystem import ReadFileTool, EditFileTool
from pulsara_agent.primitives.context import freeze_json


def _read(root, *, deferred=False):
    return ReadFileTool(root).execute(ToolCall('read', 'read_file',
        {'target': {'kind': 'PATH', 'path': 'source.txt'}}, defer_file_observation=deferred))


def _edit(root, revision, value):
    return EditFileTool(root).execute(ToolCall('edit', 'edit_file', {
        'target': {'kind': 'PATH', 'path': 'source.txt'}, 'base_revision': revision,
        'operations': [{'kind': 'replace_lines', 'start_line': 1, 'end_line': 1, 'lines': [value]}]}))


def test_public_value_uses_owner_format_without_json_guessing():
    text = b'{"value":9007199254740993}'
    assert KernelToolResult('SUCCESS', text).public_value() == text.decode()
    assert KernelToolResult('SUCCESS', b'{"value":3}', public_format=ToolOutputSourceFormatHint.JSON).public_value() == {'value': 3}


def test_code_source_does_not_require_or_copy_model_tool_call():
    source = ToolInvocationSource('call:source', ToolInvocationSourceKind.CODE_MODE_CALL,
        'turn:source', source_exec_attempt_id='attempt:exec', call_sequence=1,
        tool_name='read_file', original_arguments=freeze_json({'target': {'kind': 'PATH', 'path': 'a'}}))
    assert source.assistant_entry_id is None and source.tool_call_id is None
    with pytest.raises(ValueError):
        replace(source, assistant_entry_id='entry:fake')
    model = ToolInvocationSource.model('session:root', 'turn:source', 'entry:real', 'call:real')
    assert model.tool_name is None and model.original_arguments is None


def test_large_code_read_grants_shared_native_edit_only_after_actual_delivery(tmp_path):
    (tmp_path / 'source.txt').write_text('\n'.join('x' * 80 for _ in range(600)))
    result = _read(tmp_path, deferred=True)
    payload = json.loads(result.output)
    assert len(result.output.encode()) > 40_000
    assert result.status is ToolResultState.SUCCESS
    denied = _edit(tmp_path, payload['content_revision'], 'after-delivery')
    assert json.loads(denied.output)['error'] == 'READ_OBSERVATION_REQUIRED'
    result.delivery_succeeded()
    assert _edit(tmp_path, payload['content_revision'], 'after-delivery').status is ToolResultState.SUCCESS


def test_native_observation_is_shared_with_code_edit_and_old_read_cannot_replace_newer_observation(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('initial\nsecond\n')
    old = _read(tmp_path, deferred=True)
    native = json.loads(_read(tmp_path).output)
    assert _edit(tmp_path, native['content_revision'], 'native-read-code-edit').status is ToolResultState.SUCCESS
    fresh = json.loads(_read(tmp_path).output)
    old.delivery_succeeded()
    assert _edit(tmp_path, fresh['content_revision'], 'newer-observation').status is ToolResultState.SUCCESS


def test_source_file_is_frozen_once_through_file_owner_without_edit_observation(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('return 1;')
    tool = ReadFileTool(tmp_path)
    source = tool.read_code_mode_source('source.txt', maximum_bytes=1024)
    path.write_text('return 2;')
    assert source == 'return 1;'
    with pytest.raises(ValueError):
        tool.read_code_mode_source('source.txt', maximum_bytes=1)


def test_child_close_does_not_release_shared_terminal_owner(tmp_path):
    async def scenario():
        common = dict(workspace_root=tmp_path, live_bus=LiveAgentEventBus(),
                      authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        root = DirectKernelToolPort(**common, host_owner_id='root-host', session_id='session:root')
        child = DirectKernelToolPort(**common, host_owner_id='child-host', session_id='session:child', shared_terminal_owner=root)
        assert child._terminal is root._terminal
        assert child._terminal_monitor is root._terminal_monitor
        assert child._terminal_owner_id == root._terminal_owner_id
        await child.aclose()
        assert not root._terminal_physically_closed
        assert root._terminal.process_registry._closed is False
        await root.aclose()
    asyncio.run(scenario())


def _canonical_code_calls(database, *, code_result_body=b'private-code-physical-result', code_denial_body=b'private-code-denial'):
    from datetime import datetime, timezone
    from time import monotonic
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.llm.input import FrozenPromptContent
    from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock, build_prepared_tool_result_acceptance
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation
    from pulsara_agent.ports.artifact import ToolOutputArtifactDisposition, ToolResultDisplayKind
    from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from tests.test_stage2_conversation_kernel_postgres import _repository, _start_root_turn, _assistant_permission_fingerprint
    repository = _repository(database)
    deadline = monotonic() + 30
    session_id, workspace_id, turn_id = (random_id(k) for k in ('session', 'workspace', 'turn'))
    lease = repository.acquire_host_writer(intent='NEW', session_id=session_id,
        workspace_id=workspace_id, writer_owner_id=random_id('host'), lease_seconds=30, deadline_monotonic=deadline)
    _start_root_turn(repository, lease.guard, command_id=random_id('command'), turn_id=turn_id,
        entry_id=random_id('entry'), context_binding_revision_id=random_id('binding'),
        content=FrozenPromptContent.text('execute the program'), occurred_at=datetime.now(timezone.utc), deadline_monotonic=deadline)
    cut = repository.prepare_provider_input_cut(lease.guard, turn_id=turn_id, deadline_monotonic=deadline)
    exec_entry, exec_call, exec_attempt = (random_id(k) for k in ('entry','call','attempt'))
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=exec_entry,
        parent_content=InlineContent.from_bytes(b'program request'), blocks=(AssistantToolCallBlock(
            block_id=random_id('block'), tool_call_id=exec_call, tool_name='exec_cell', arguments=freeze_json({'code':'return 1;'})),),
        occurred_at=datetime.now(timezone.utc), actor_id='model:test', deadline_monotonic=deadline)
    permission = _assistant_permission_fingerprint(repository, lease.guard, exec_entry, deadline)
    repository.accept_tool_attempt(lease.guard, attempt_id=exec_attempt, assistant_entry_id=exec_entry,
        tool_call_id=exec_call, authorization_kind='policy', authorization_reference='test', actor_kind='runtime',
        actor_id='test', remote_idempotency_key=None, retry_of_attempt_id=None, permission_snapshot_fingerprint=permission,
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=deadline)
    source = InlineContent.from_bytes(b'return 1;', media_type='text/javascript')
    repository.freeze_code_mode_source(lease.guard, attempt_id=exec_attempt, content=source, deadline_monotonic=deadline)
    repository.freeze_code_mode_source(lease.guard, attempt_id=exec_attempt, content=source, deadline_monotonic=deadline)
    repository.interrupt_turn(lease.guard, turn_id=turn_id, reason='TEST_SOURCE_TURN_ENDED',
        occurred_at=datetime.now(timezone.utc), actor_id='test', deadline_monotonic=deadline)
    result_entries = []
    for sequence, decision in ((1, 'DENY'), (2, 'ALLOW')):
        call_id = random_id('call')
        provenance = ToolInvocationSource(call_id, ToolInvocationSourceKind.CODE_MODE_CALL, turn_id,
            source_exec_attempt_id=exec_attempt, call_sequence=sequence, tool_name='read_file',
            original_arguments=freeze_json({'target': {'kind':'PATH','path':'private-code-result'}}))
        token = current_tool_invocation.set(provenance)
        try:
            result_id, entry_id = random_id('result'), random_id('entry')
            attempt_id = random_id('attempt') if decision == 'ALLOW' else None
            repository.accept_tool_capability_decision(lease.guard, decision_id=random_id('decision'),
                assistant_entry_id=None, tool_call_id=call_id, decision=decision,
                authorization_reference='test', redacted_subject='tool:read_file', attempt_id=attempt_id,
                result_id=result_id if decision == 'DENY' else None,
                result_entry_id=entry_id if decision == 'DENY' else None,
                denial_content=InlineContent.from_bytes(code_denial_body) if decision == 'DENY' else None,
                denial_result_state='PERMISSION_DENIED' if decision == 'DENY' else None,
                occurred_at=datetime.now(timezone.utc), actor_id='test',
                permission_snapshot_fingerprint=permission, deadline_monotonic=deadline)
            if decision == 'ALLOW':
                candidate = build_prepared_tool_result_acceptance(guard=lease.guard, workspace_id=workspace_id,
                    result_id=result_id, result_entry_id=entry_id, turn_id=turn_id, assistant_entry_id=None,
                    tool_call_id=call_id, attempt_id=attempt_id, result_state='SUCCESS',
                    canonical_preview_content=InlineContent.from_bytes(code_result_body),
                    artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED, artifact_id=None,
                    artifact_blob_descriptor=None, source_coverage=ToolOutputSourceCoverage.COMPLETE,
                    display_kind=ToolResultDisplayKind.COMPLETE, source_coverage_reason=None,
                    artifact_unavailability_reason=None, observed_at=datetime.now(timezone.utc),
                    observation_duration_microseconds=10, observation_origin_kind=ToolObservationOrigin.BUILTIN,
                    trusted_tool_reported_duration_microseconds=None, actor_id='read_file')
                accepted = repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=deadline)
                assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate, deadline_monotonic=deadline) == accepted
            result_entries.append(entry_id)
        finally:
            current_tool_invocation.reset(token)
    return repository, lease, turn_id, exec_entry, exec_call, tuple(result_entries)


@pytest.mark.postgres
def test_code_call_rejections_and_late_results_share_actual_source_without_assistant_blocks(stage2_migrated_postgres_database):
    from time import monotonic
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    repository, lease, turn_id, exec_entry, exec_call, results = _canonical_code_calls(stage2_migrated_postgres_database)
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic()+30) as connection:
        assert connection.execute('SELECT count(*) FROM pulsara_v3.assistant_message_blocks WHERE session_id=%s', (lease.guard.session_id,)).fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_invocations WHERE session_id=%s', (lease.guard.session_id,)).fetchone()[0] == 3
        assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s', (lease.guard.session_id,)).fetchone()[0] == 2
        rows = connection.execute('SELECT tool_call_entry_id,attempt_id,result_entry_id FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY observed_at', (lease.guard.session_id,)).fetchall()
        assert len(rows) == 2 and all(row[0] is None for row in rows)
        assert rows[0][1] is None and rows[1][1] is not None
        assert tuple(row[2] for row in rows) == results


@pytest.mark.postgres
def test_real_host_code_calls_parallel_then_continue_after_source_turn_final(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host
    (tmp_path / 'source.txt').write_text('before\nsecond\n')
    bodies = []
    cell_id = None
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation
    post_diagnostics = []
    original_post = ToolBatchExecutor._dispatch_post_tool
    original_offer = ToolBatchExecutor._offer_operational_best_effort
    async def failed_after_accepted_edit(owner, **values):
        source = current_tool_invocation.get()
        if source is not None and source.kind is ToolInvocationSourceKind.CODE_MODE_CALL and values['prepared'].requested_tool_name == 'edit_file':
            raise RuntimeError('injected post Hook failure after accepted edit')
        return await original_post(owner, **values)
    def observe_diagnostic(owner, offer):
        if offer.public_payload.get('outcome') == 'POST_TOOL_HOOK_FAILED':
            post_diagnostics.append(offer.public_payload)
        original_offer(owner, offer)
    monkeypatch.setattr(ToolBatchExecutor, '_dispatch_post_tool', failed_after_accepted_edit)
    monkeypatch.setattr(ToolBatchExecutor, '_offer_operational_best_effort', observe_diagnostic)
    code = '''
const order = [];
await Promise.all([
  tools.exec_process({command:"sleep 0.3; printf slow", yield_time_ms:1000}).then(x => order.push("slow")),
  tools.read_file({target:{kind:"PATH",path:"source.txt"}}).then(x => order.push("fast"))
]);
const read = await tools.read_file({target:{kind:"PATH",path:"source.txt"}});
await tools.edit_file({target:{kind:"PATH",path:"source.txt"},base_revision:read.content_revision,
  operations:[{kind:"replace_lines",start_line:1,end_line:1,lines:["after-source-final"]}]});
try { await tools.read_file({unknown: true}); } catch (error) { console.log({rejected:error.code}); }
console.log({order});
return {finished:true};
'''
    async def stream(request):
        nonlocal cell_id
        index = len(model.requests)
        tool_messages = [m for m in request.compiled_input.messages if m.role is MessageRole.TOOL_RESULT]
        # Internal CODE calls never become unmatched provider tool messages.
        for message in tool_messages:
            body = str(decode_tool_result_observation(text_part_values(message.content)[0])['body'])
            assert 'after-source-final' not in body
        if index == 1:
            payloads = _tool_call('exec_cell', random_id('call'), {'code':code,'yield_time_ms':0})
        elif index == 2:
            body = str(decode_tool_result_observation(text_part_values(tool_messages[-1].content)[0])['body'])
            observation = json.loads(body)
            cell_id = observation['target']['cell_id']
            bodies.append(observation)
            payloads = _text('first-final','FIRST_SOURCE_FINAL')
        elif index == 3:
            payloads = _tool_call('manage_cell', random_id('call'), {'action':'wait','target':{'kind':'CELL','cell_id':cell_id},'yield_time_ms':1000})
        else:
            body = str(decode_tool_result_observation(text_part_values(tool_messages[-1].content)[0])['body'])
            observation = json.loads(body)
            bodies.append(observation)
            if observation['state'] in ('running','queued') or observation['has_more']:
                payloads = _tool_call('manage_cell', random_id('call'), {'action':'wait','target':{'kind':'CELL','cell_id':cell_id},'yield_time_ms':1000})
            else:
                payloads = _text('second-final','SECOND_FINAL')
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, 'DirectKernelModelPort', lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, 'load_configs', lambda *_a, **_kw: ())
    runtime = test_model_runtime(model_id='test-pro', wire_api='openai_chat_completions', postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            first = await session.run_turn(PromptContent.text('launch the background script'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert first.final_text == 'FIRST_SOURCE_FINAL'
            second = await session.run_turn(PromptContent.text('observe the script'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert second.final_text == 'SECOND_FINAL'
            assert bodies[-1]['state'] == 'completed'
            assert (tmp_path / 'source.txt').read_text().startswith('after-source-final\n')
            texts = [record.get('text','') for body in bodies for record in body['output']]
            assert json.dumps({'order':['fast','slow']},separators=(',',':')) in texts
            assert any('INVALID_ARGUMENTS' in text for text in texts)
            assert len(post_diagnostics) == 1 and post_diagnostics[0]['tool_name'] == 'edit_file'
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('mode', ['stop', 'archive', 'terminal_artifact', 'unicode'])
def test_real_host_stop_settles_effect_and_large_output_pages_keep_artifact(mode, tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from time import monotonic
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host
    bodies = []
    cell_id = None
    stopped = False
    code = ('await tools.exec_process({command:"printf started > launched; sleep 0.3; printf late-result",yield_time_ms:1000}); console.log("unreachable");'
            if mode == 'stop' else 'console.log("x".repeat(50000)); console.log("tail-record");')
    if mode == 'unicode':
        code = 'console.log("\\ud800"); return "\\ud800";'
    if mode == 'terminal_artifact':
        command = "head -c 50000 /dev/zero | tr '\\0' x"
        code = 'const result = await tools.exec_process(' + json.dumps({'command':command,'yield_time_ms':1000,'max_output_chars':512}) + '); console.log({artifact:result.output_artifact});'

    async def stream(request):
        nonlocal cell_id, stopped
        index = len(model.requests)
        if index == 1:
            payloads = _tool_call('exec_cell', random_id('call'), {'code':code,'yield_time_ms':0 if mode == 'stop' else 1000})
        else:
            message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
            body = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])['body']))
            bodies.append(body)
            cell_id = body['target']['cell_id']
            if mode == 'stop' and not stopped:
                deadline = monotonic() + 5
                while not (tmp_path / 'launched').exists():
                    if monotonic() > deadline:
                        raise AssertionError('physical terminal effect did not begin')
                    await asyncio.sleep(0.01)
                stopped = True
                payloads = _tool_call('manage_cell', random_id('call'), {'action':'stop','target':{'kind':'CELL','cell_id':cell_id}})
            elif body.get('has_more') or body.get('pending_calls') or body['state'] in ('running','queued'):
                payloads = _tool_call('manage_cell', random_id('call'), {'action':'wait','target':{'kind':'CELL','cell_id':cell_id},'yield_time_ms':1000})
            else:
                payloads = _text('final','OBSERVED')
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, 'DirectKernelModelPort', lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, 'load_configs', lambda *_a, **_kw: ())
    runtime = test_model_runtime(model_id='test-pro', wire_api='openai_chat_completions', postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text('run the script'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'OBSERVED'
            records = [record for body in bodies for record in body.get('output', ())]
            if mode == 'stop':
                assert bodies[-1]['state'] == 'terminated'
                late = [record for record in records if record['kind'] == 'tool_result']
                assert len(late) == 1 and late[0]['state'] == 'SUCCESS'
                assert late[0]['sequence'] == 1 and late[0]['result_entry_id']
                assert 'late-result' in late[0]['result']
                assert all(record.get('text') != 'unreachable' for record in records)
                assert (tmp_path / 'launched').read_text() == 'started'
            elif mode == 'unicode':
                assert records == [{'kind':'log','text':'\ud800'},{'kind':'return','text':'\ud800'}]
            elif mode == 'terminal_artifact':
                record = next(record for record in records if record.get('kind') == 'log')
                artifact = json.loads(record['text'])['artifact']
                assert artifact['disposition'] == 'AVAILABLE'
                assert artifact['source_coverage'] == 'COMPLETE'
                port = session._tools._tools['read_artifact'].artifact_read_port
                content = await asyncio.to_thread(port.read_body, artifact['target']['artifact_id'])
                assert content.content == b'x' * 50000
            else:
                archived = next(record['output_artifact'] for record in records if 'output_artifact' in record)
                port = session._tools._tools['read_artifact'].artifact_read_port
                content = await asyncio.to_thread(port.read_body, archived['target']['artifact_id'])
                assert json.loads(content.content) == {'kind':'log','text':'x'*50000}
                assert records[-1] == {'kind':'log','text':'tail-record'}
                assert any(body.get('has_more') for body in bodies)
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('rewrite', ['valid', 'identity', 'profile', 'profile_null'])
def test_batch_pre_hooks_use_each_run_member_and_one_real_invocation(rewrite, tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor, CodeModeToolRequest, CodeModeCallFacts, _batch_reservations
    from pulsara_agent.conversation_kernel.tool_contracts import PreparedResolvedToolInvocation
    from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import prepare_batch_refs
    from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
    from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
    from pulsara_agent.hooks.contracts import HookEventType, GateDecision
    from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
    from pulsara_agent.ports.session_content import SessionContentRange
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from tests.test_round9_2_hook_subsystem import _definition, _view, _scope, _ScriptedExecutor
    from time import monotonic
    async def scenario():
        scope = _scope()
        specs = {'first':{'instruction':'first original'},'second':{'instruction':'second original'}}
        peers = prepare_batch_refs(specs)
        arguments = {'members':[{'key':key,'agent_id':peers[key]['agent']['agent_id'],
            'work_id':peers[key]['work']['work_id'],'parameters':spec} for key,spec in specs.items()]}
        replacement = {'instruction':'rewritten by hook'}
        if rewrite == 'identity':
            replacement['target'] = {'kind':'AGENT','agent_id':random_id('session')}
        elif rewrite in ('profile', 'profile_null'):
            replacement['profile'] = 'research_worker' if rewrite == 'profile' else None
        definition = _definition(tmp_path, HookEventType.PRE_TOOL_USE_EVENT, 'rewrite')
        response = json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','updatedInput':replacement}}).encode()
        executor = _ScriptedExecutor({'rewrite':(0,response,b'')})
        dispatcher = KernelHookDispatcher(initial_view=_view(tmp_path,(definition,)),workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),executor=executor)
        owner = object.__new__(ToolBatchExecutor)
        owner._hooks, owner._hook_context = dispatcher, None
        owner._tools = SimpleNamespace(snapshot_workspace_root=lambda:tmp_path)
        owner._deadlines = KernelExecutionDeadlineFactory()
        permission = build_run_permission_snapshot(snapshot_id=random_id('permission'),
            requested_mode=PermissionMode.BYPASS_PERMISSIONS,effective_mode=PermissionMode.BYPASS_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
        turn_id, call_id = random_id('turn'), random_id('call')
        source = ToolInvocationSource(call_id,ToolInvocationSourceKind.CODE_MODE_CALL,turn_id,
            source_exec_attempt_id=random_id('attempt'),call_sequence=1,tool_name='run_agent.batch',original_arguments=freeze_json(arguments))
        source_token = current_tool_invocation.set(source)
        reservation_token = _batch_reservations.set([])
        try:
            result, causal = await owner._dispatch_pre_tool(view=dispatcher.capture_view(),scope=scope,
                prepared=PreparedResolvedToolInvocation('run_agent.batch','run_agent.batch',freeze_json(arguments)),
                turn_id=turn_id,assistant_entry_id=None,tool_call_id=call_id,
                request=CodeModeToolRequest(scope.session_id,'test',None,FrozenModelCallMemoryContext()),
                canonical_facts=CodeModeCallFacts(permission,SessionContentRange(0)))
            assert len(executor.calls) == 2
            assert [request.public_stdin['tool_name'] for request in executor.calls] == ['run_agent','run_agent']
            assert [request.public_stdin['tool_input']['instruction'] for request in executor.calls] == ['first original','second original']
            assert all(request.public_stdin['tool_use_id'] == call_id for request in executor.calls)
            assert all(request.public_stdin['input_origin'] == 'CODE_MODE_CALL' for request in executor.calls)
            assert causal.invocation_id == call_id
            assert result.decision is (GateDecision.PROCEED if rewrite == 'valid' else GateDecision.BLOCK)
            if rewrite == 'valid':
                assert all(member['parameters'] == replacement for member in result.updated_input['members'])
        finally:
            current_tool_invocation.reset(source_token)
            _batch_reservations.reset(reservation_token)
            await dispatcher.aclose(deadline_monotonic=monotonic()+5)
    asyncio.run(scenario())


@pytest.mark.postgres
def test_two_cells_and_native_batch_share_leaf_without_serializing_physical_waits(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host
    (tmp_path/'source.txt').write_text('native-value\n')
    cells, observations = [], []
    native_seen = False
    remaining = ['B','A']
    async def stream(request):
        nonlocal native_seen
        index = len(model.requests)
        messages = [m for m in request.compiled_input.messages if m.role is MessageRole.TOOL_RESULT]
        if index == 1:
            payloads = ()
            for label, duration in (('A',0.4),('B',0.05)):
                code = 'await tools.exec_process(' + json.dumps({'command':f'sleep {duration}; printf {label} >> order.txt','yield_time_ms':1000}) + '); return ' + json.dumps(label) + ';'
                payloads += _tool_call('exec_cell', random_id('call'), {'code':code,'yield_time_ms':0})
        elif index == 2:
            assert len(messages) == 2
            for message in messages:
                observed = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])['body']))
                cells.append(observed['target']['cell_id'])
                observations.append(observed)
            payloads = _tool_call('read_file', random_id('call'), {'target':{'kind':'PATH','path':'source.txt'}})
        else:
            body = json.loads(str(decode_tool_result_observation(text_part_values(messages[-1].content)[0])['body']))
            if index == 3:
                assert body['content'].endswith('native-value')
                native_seen = True
            else:
                observations.append(body)
                if body['state'] == 'completed' and not body['has_more']:
                    remaining.pop(0)
            if remaining:
                cell = cells[1] if remaining[0] == 'B' else cells[0]
                payloads = _tool_call('manage_cell', random_id('call'), {'action':'wait','target':{'kind':'CELL','cell_id':cell},'yield_time_ms':1000})
            else:
                payloads = _text('done','BOTH_CELLS_SETTLED')
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host,'DirectKernelModelPort',lambda **_:model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService,'load_configs',lambda *_a,**_kw:())
    runtime = test_model_runtime(model_id='test-pro',wire_api='openai_chat_completions',postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text('start two scripts'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'BOTH_CELLS_SETTLED'
            assert native_seen and len(set(cells)) == 2
            assert (tmp_path/'order.txt').read_text() == 'BA'
            returned = [record['text'] for page in observations for record in page['output'] if record['kind'] == 'return']
            assert sorted(returned) == ['A','B']
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('rewrite', ['valid', 'identity', 'profile', 'profile_null'])
def test_batch_hooks_reject_invalid_rewrites_and_keep_accepted_work_after_post_failure(rewrite, tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _tool_call, _text
    from time import monotonic
    import pulsara_agent.conversation_kernel.host as kernel_host
    root_session_id, cell_id = None, None
    root_calls = 0
    pages, member_posts, diagnostics = [], [], []
    original_post = ToolBatchExecutor._dispatch_post_tool
    original_pre = ToolBatchExecutor._dispatch_pre_tool
    original_offer = ToolBatchExecutor._offer_operational_best_effort
    async def fail_one_member(owner, **values):
        member = values.get('_member_key')
        if member is not None:
            member_posts.append(member)
        if member == 'first' and rewrite == 'valid':
            raise RuntimeError('injected member post failure after actual batch admission')
        return await original_post(owner, **values)
    def observe_diagnostic(owner, offer):
        if offer.public_payload.get('outcome') == 'POST_TOOL_HOOK_FAILED':
            diagnostics.append(offer.public_payload)
        original_offer(owner, offer)
    async def change_pre_identity(owner, **values):
        outcome, causal = await original_pre(owner, **values)
        if rewrite != 'valid' and values.get('_member_key') == 'first':
            updated = {'instruction':'changed by hook'}
            if rewrite == 'identity':
                updated['target'] = {'kind':'AGENT','agent_id':random_id('session')}
            else:
                updated['profile'] = 'research_worker' if rewrite == 'profile' else None
            return replace(outcome, updated_input=updated), causal
        return outcome, causal
    monkeypatch.setattr(ToolBatchExecutor,'_dispatch_pre_tool',change_pre_identity)
    monkeypatch.setattr(ToolBatchExecutor,'_dispatch_post_tool',fail_one_member)
    monkeypatch.setattr(ToolBatchExecutor,'_offer_operational_best_effort',observe_diagnostic)
    async def stream(request):
        nonlocal root_calls, cell_id
        if request.session_id != root_session_id:
            payloads = _text(random_id('block'),'CHILD_COMPLETED')
        else:
            root_calls += 1
            if root_calls == 1:
                code = 'try { const admitted=await tools.run_agent.batch({first:{instruction:"first work"},second:{instruction:"second work"}});console.log(admitted); } catch(error) { console.log({rejected:error.code}); }'
                payloads = _tool_call('exec_cell',random_id('call'),{'code':code,'yield_time_ms':1000})
            else:
                message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
                page = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])['body']))
                pages.append(page)
                cell_id = page['target']['cell_id']
                if page['state'] in ('running','queued') or page.get('pending_calls') or page['has_more']:
                    payloads = _tool_call('manage_cell',random_id('call'),{'action':'wait','target':{'kind':'CELL','cell_id':cell_id},'yield_time_ms':1000})
                else:
                    payloads = _text('root-final','BATCH_ACCEPTED')
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host,'DirectKernelModelPort',lambda **_:model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService,'load_configs',lambda *_a,**_kw:())
    runtime = test_model_runtime(model_id='test-pro',wire_api='openai_chat_completions',postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        nonlocal root_session_id
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=tmp_path))
        root_session_id = session.session_id
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text('admit a batch'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'BATCH_ACCEPTED'
            assert pages[-1]['state'] == 'completed'
            batch = json.loads(next(record['text'] for page in pages for record in page['output'] if record['kind']=='log'))
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute("SELECT r.result_state,r.attempt_id FROM pulsara_v3.tool_results r JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id WHERE i.session_id=%s AND i.source_kind='CODE_MODE_CALL' AND i.tool_name='run_agent.batch'",(root_session_id,)).fetchall()
                assert len(rows) == 1
                if rewrite != 'valid':
                    assert batch == {'rejected':'PERMISSION_DENIED'}
                    assert rows[0] == ('PERMISSION_DENIED',None)
                    assert connection.execute('SELECT count(*) FROM pulsara_v3.sessions WHERE root_session_id=%s',(root_session_id,)).fetchone()[0] == 0
                    assert not diagnostics
                else:
                    assert set(batch) == {'first','second'}
                    assert member_posts == ['first','second']
                    assert len(diagnostics) == 1 and diagnostics[0]['member_key'] == 'first'
                    work_ids = [item['work']['work_id'] for item in batch.values()]
                    assert connection.execute('SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE id=ANY(%s)',(work_ids,)).fetchone()[0] == 2
                    assert rows[0][0] == 'SUCCESS' and rows[0][1] is not None
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('permission_mode', ['read-only', 'ask-permissions', 'bypass-permissions'])
def test_cold_surface_filters_code_and_root_agent_tools_without_rewriting_old_prefix(permission_mode, tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.identity import random_id
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
    from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from tests.support.round3 import prepare_test_direct_tool_surface
    operations = {'exec_cell','query_cells','manage_cell','run_agent','query_agents','send_agent_message','wait_agent','stop_agent'}
    mode = PermissionMode(permission_mode)
    async def scenario():
        port = DirectKernelToolPort(workspace_root=tmp_path,host_owner_id='mode-host',session_id=random_id('session'),
            live_bus=LiveAgentEventBus(),authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        from pulsara_agent.code_mode import CodeModeHost
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset(operations-{'exec_cell','query_cells','manage_cell'}),
            validate_arguments=lambda **_:None, bind_cell_observer=lambda _:None))
        port.bind_code_mode_port(CodeModeHost())
        try:
            surface = prepare_test_direct_tool_surface(port,conversation_scope_kind=ModelInputScopeKind.ROOT,
                effective_permission_mode=mode)
            names = {item.name for item in surface.model_surface.tool_specs}
            assert names.intersection(operations) == (operations if mode is PermissionMode.BYPASS_PERMISSIONS else set())
            if mode is not PermissionMode.BYPASS_PERMISSIONS:
                # A retained epoch may still carry these exact schemas. The
                # new turn's execution permission rejects every operation.
                retained = prepare_test_direct_tool_surface(port,conversation_scope_kind=ModelInputScopeKind.ROOT,
                    effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                prefix = tuple(item.canonical_bytes for item in retained.model_surface.tool_specs)
                borrow = port.borrow_tool_surface(retained)
                permission = build_run_permission_snapshot(snapshot_id=random_id('permission'),requested_mode=mode,
                    effective_mode=mode,admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
                agent = {'kind':'AGENT','agent_id':random_id('session')}
                arguments = {'exec_cell':{'code':'return 1;'},'manage_cell':{'action':'wait','target':{'kind':'CELL','cell_id':'cell:'+'a'*26}},'query_cells':{},
                    'run_agent':{'instruction':'work'},'query_agents':{'target':agent},
                    'send_agent_message':{'target':agent,'message':'hello'},'wait_agent':{'targets':[agent]},'stop_agent':{'target':agent}}
                try:
                    for name in operations:
                        authorization = await port.authorize(tool_name=name,arguments=arguments[name],tool_call_id=random_id('call'),
                            turn_id=random_id('turn'),assistant_entry_id=random_id('entry'),permission_snapshot=permission,
                            surface_borrow=borrow,memory_context=FrozenModelCallMemoryContext())
                        assert authorization.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
                    assert tuple(item.canonical_bytes for item in retained.model_surface.tool_specs) == prefix
                finally:
                    borrow.close()
        finally:
            await port.aclose()
    asyncio.run(scenario())


def test_delayed_read_never_replaces_newer_observation_when_file_reverts(tmp_path):
    from pulsara_agent.tools.builtins.filesystem import _state_for_workspace
    path = tmp_path/'source.txt'
    path.write_text('old revision\n')
    delayed = _read(tmp_path,deferred=True)
    path.write_text('new revision\n')
    newer = json.loads(_read(tmp_path).output)
    path.write_text('old revision\n')
    delayed.delivery_succeeded()
    assert _state_for_workspace(tmp_path).observation(path.resolve()).content_revision == newer['content_revision']
