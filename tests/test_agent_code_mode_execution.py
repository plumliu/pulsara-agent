"""Shared execution facts, delivery and physical-owner behavior."""
from __future__ import annotations
from tests.support.model_config import scheduled_task_service

import asyncio
import json
from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_invocations import ToolInvocationSource, ToolInvocationSourceKind
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.tools.builtins.filesystem import ReadFileTool
from pulsara_agent.primitives.context import freeze_json


def _native_fields(text):
    return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]


def test_public_value_uses_explicit_text_and_object_content():
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.ports.tool_execution import ToolBusinessValue
    text='{"value":9007199254740993}'
    result=KernelToolResult('SUCCESS',ToolBusinessValue(content=(ResultPart('text',text),)))
    assert result.public_value()['content']==[{'type':'text','value':text}]
    result=KernelToolResult('SUCCESS',ToolBusinessValue(content=(ResultPart('object',{'value':3}),)))
    assert result.public_value()['content']==[{'type':'object','value':{'value':3}}]


def test_code_source_does_not_require_or_copy_model_tool_call():
    from pulsara_agent.identity import random_id

    turn_id = random_id('turn')
    source = ToolInvocationSource(random_id('call'), ToolInvocationSourceKind.CODE_MODE_CALL,
        turn_id, source_exec_attempt_id=random_id('attempt'), call_sequence=1,
        tool_name='read_file', original_arguments=freeze_json({'target': {'kind': 'PATH', 'path': 'a'}}))
    assert source.assistant_entry_id is None and source.tool_call_id is None
    with pytest.raises(ValueError):
        replace(source, assistant_entry_id=random_id('entry'))
    model = ToolInvocationSource.model(random_id('session'), turn_id, random_id('entry'), 'provider-call')
    assert model.tool_name is None and model.original_arguments is None






def test_source_file_is_frozen_once_through_file_owner_without_edit_observation(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('return 1;')
    tool = ReadFileTool(tmp_path)
    source = tool.read_code_mode_source('source.txt')
    path.write_text('return 2;')
    assert source == 'return 1;'
    assert tool.read_code_mode_source('source.txt') == 'return 2;'


def test_child_close_does_not_release_shared_terminal_owner(tmp_path):
    async def scenario():
        common = dict(workspace_root=tmp_path, live_bus=LiveAgentEventBus(),
                      authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        root = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), **common, host_owner_id='root-host', session_id='session:root')
        child = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), **common, host_owner_id='child-host', session_id='session:child', shared_terminal_owner=root)
        assert child._terminal is root._terminal
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
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
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
        blocks=(AssistantToolCallBlock(
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
                    public_fields={}, content_parts=(ResultPart('text',code_result_body.decode()),),
                    observed_at=datetime.now(timezone.utc),
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
        assert connection.execute("SELECT count(*) FROM pulsara_v3.content_parts WHERE session_id=%s AND type='tool_call'", (lease.guard.session_id,)).fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_invocations WHERE session_id=%s', (lease.guard.session_id,)).fetchone()[0] == 3
        assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s', (lease.guard.session_id,)).fetchone()[0] == 2
        rows = connection.execute('SELECT tool_call_entry_id,attempt_id,result_entry_id FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY observed_at', (lease.guard.session_id,)).fetchall()
        assert len(rows) == 2 and all(row[0] is None for row in rows)
        assert rows[0][1] is None and rows[1][1] is not None
        assert tuple(row[2] for row in rows) == results


def _formal_cell_rows(session):
    from time import monotonic
    from psycopg.rows import dict_row
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.conversation_kernel._repository.queue_content import read_input_owner
    with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
        rows = connection.execute("SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s "
            "AND input_origin='CELL_OUTPUT' ORDER BY queue_sequence",(session.session_id,)).fetchall()
        values=[]
        for row in rows:
            content=read_input_owner(connection,row=row)
            assert (content.cell_id,content.source_turn_id,content.source_exec_attempt_id,content.record_sequence,content.storage_kind)==(row['source_cell_id'],row['source_turn_id'],row['source_tool_attempt_id'],row['source_record_sequence'],row['cell_output_kind'])
            assert all(p['type']=='text' for p in content.storage_parts)
            values.append((row,{'kind':content.storage_kind,'text':''.join(p['value'] for p in content.storage_parts)}))
        return values


async def _settled_formal_records(session, *, expected=None):
    from time import monotonic
    deadline=monotonic()+15
    while True:
        rows = await asyncio.to_thread(_formal_cell_rows,session)
        cells=session._code_mode.snapshot_cell_index()
        if not cells and (expected is None or len(rows)>=expected):
            return [record for _,record in rows]
        assert monotonic()<deadline, {'records':[record for _,record in rows],'cells':cells,'expected':expected}
        await asyncio.sleep(.005)


@pytest.mark.postgres
def test_real_host_code_calls_parallel_then_continue_after_source_turn_final(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
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
await tools.edit_file({target:{kind:"PATH",path:"source.txt"},old_text:"before",new_text:"after-source-final"});
try { await tools.read_flie({}); } catch (error) { console.log({tool_hint:error.message}); }
try { await tools.read_file({target:{kind:"PATH",path:"source.txt"},limti:1}); }
catch (error) { console.log({parameter_hint:error.message}); }
try { await tools.read_file({unknown: true}); } catch (error) { console.log({rejected:error.code}); }
console.log({order});
return {finished:true};
'''
    async def stream(request):
        nonlocal cell_id
        index = len(model.requests)
        tool_messages = [m for m in request.compiled_input.messages if m.role is MessageRole.TOOL_RESULT]
        for message in tool_messages:
            body = text_part_values(message.content)[0]
            assert 'after-source-final' not in body
        if index == 1:
            payloads = _tool_call('exec_cell', random_id('call'), {'code':code})
        else:
            if index == 2:
                observation = _native_fields(text_part_values(tool_messages[-1].content)[0])
                cell_id = observation['target']['cell_id']
                bodies.append(observation)
                assert observation['meta']['status']=='SUCCESS'
            payloads = _text(random_id('block'),'FIRST_SOURCE_FINAL' if index == 2 else 'FORMAL_INPUT_OBSERVED')
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
            records = await _settled_formal_records(session, expected=5)
            assert (tmp_path / 'source.txt').read_text().startswith('after-source-final\n')
            texts = [record.get('text','') for record in records]
            parsed=[json.loads(text) for text in texts]
            assert {'order':['fast','slow']} in parsed
            assert any('INVALID_ARGUMENTS' in text for text in texts)
            assert any('Did you mean \\"read_file\\"?' in text for text in texts)
            assert any("Did you mean" in str(value) and "limit" in str(value) for value in parsed), parsed
            assert records[-1]['kind']=='return' and parsed[-1]=={'finished':True}
            assert len(post_diagnostics) == 1 and post_diagnostics[0]['tool_name'] == 'edit_file'
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('mode', ['stop', 'large_record', 'terminal_body', 'unicode'])
def test_real_host_stop_and_frozen_large_outputs_use_canonical_owners(mode, tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from time import monotonic
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host
    code = ('await tools.exec_process({command:"printf started > launched; sleep 0.3; printf late-result",yield_time_ms:1000}); console.log("unreachable");'
        if mode == 'stop' else 'console.log("x".repeat(50000)); console.log("tail-record");')
    if mode == 'unicode':
        code = 'console.log("\\ud800"); return "\\ud800";'
    if mode == 'terminal_body':
        command = "head -c 50000 /dev/zero | tr '\\0' x"
        code = 'const result=await tools.exec_process(' + json.dumps({'command':command,'yield_time_ms':1000}) + ');console.log({length:result.content.map(p=>p.value).join("").length,exit_code:result.meta.exit_code});'
    accepted_cell = None
    async def stream(request):
        nonlocal accepted_cell
        index = len(model.requests)
        if index == 1:
            payloads = _tool_call('exec_cell',random_id('call'),{'code':code})
        elif index == 2:
            message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
            body = _native_fields(text_part_values(message.content)[0])
            assert body['meta']['status']=='SUCCESS'
            accepted_cell = body['target']
            if mode == 'stop':
                async with asyncio.timeout(5):
                    while not (tmp_path/'launched').exists():
                        await asyncio.sleep(.005)
                payloads = _tool_call('manage_cell',random_id('call'),{'action':'stop','target':accepted_cell})
            else:
                payloads = _text(random_id('block'),'OBSERVED')
        else:
            if mode == 'stop' and index == 3:
                message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
                body = _native_fields(text_part_values(message.content)[0])
                assert body['meta']['cell_status'] == 'terminated' and 'output' not in body
            payloads = _text(random_id('block'),'OBSERVED')
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
            result = await session.run_turn(PromptContent.text('run script'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'OBSERVED'
            records = await _settled_formal_records(session)
            if mode == 'stop':
                assert not records and (tmp_path/'launched').read_text() == 'started'
            elif mode == 'unicode':
                assert records == [{'kind':'log','text':'\ud800'},{'kind':'return','text':'\ud800'}]
            elif mode == 'terminal_body':
                assert len(records)==1 and records[0]['kind']=='log'
                assert json.loads(records[0]['text'])=={'length':50000,'exit_code':0}
                with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                        deadline_monotonic=monotonic()+10) as connection:
                    rows = connection.execute("SELECT r.result_entry_id FROM pulsara_v3.tool_results r "
                        "JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id "
                        "WHERE r.session_id=%s AND i.source_kind='CODE_MODE_CALL' AND i.tool_name='exec_process'",(session.session_id,)).fetchall()
                assert len(rows) == 1
                reader = PostgresToolOutputReadPort(session.repository.connection_provider,
                    session_id=session.session_id,workspace_id=session.workspace.workspace_key)
                actual=reader.read_result({'kind':'SESSION_ENTRY','entry_id':rows[0][0]}).value
                assert ''.join(part['value'] for part in actual['content'])=='x'*50000
                assert actual['meta']['exit_code']==0
            else:
                assert records == [{'kind':'log','text':'x'*50000},{'kind':'log','text':'tail-record'}]
                rows = await asyncio.to_thread(_formal_cell_rows,session)
                async with asyncio.timeout(5):
                    while rows[0][0]['consumed_entry_id'] is None:
                        await asyncio.sleep(.005)
                        rows = await asyncio.to_thread(_formal_cell_rows,session)
                reader = PostgresToolOutputReadPort(session.repository.connection_provider,
                    session_id=session.session_id,workspace_id=session.workspace.workspace_key)
                with pytest.raises(KeyError):
                    reader.read_result({'kind':'SESSION_ENTRY','entry_id':rows[0][0]['consumed_entry_id']})

        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('blocked', [False, True])
def test_batch_pre_hooks_use_each_run_member_and_one_real_invocation(blocked, tmp_path):
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
        specs = [{'instruction':'first original'}, {'instruction':'second original'}]
        peers = prepare_batch_refs([{} for _ in specs])
        arguments = {'members':[{'agent_id':peers['agents'][index]['agent_id'],
            'work_id':peers['works'][index]['work_id'],'parameters':spec} for index,spec in enumerate(specs)]}
        definition = _definition(tmp_path, HookEventType.PRE_TOOL_USE_EVENT, 'review')
        response = json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse',
            **({'permissionDecision':'deny', 'permissionDecisionReason':'member refused'} if blocked else {})}}).encode()
        executor = _ScriptedExecutor({'review':(0,response,b'')})
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
            source_exec_attempt_id=random_id('attempt'),call_sequence=1,tool_name='run_agent',original_arguments=freeze_json(arguments))
        source_token = current_tool_invocation.set(source)
        reservation_token = _batch_reservations.set([])
        try:
            result, causal = await owner._dispatch_pre_tool(view=dispatcher.capture_view(),scope=scope,
                prepared=PreparedResolvedToolInvocation('run_agent','run_agent',freeze_json(arguments)),
                turn_id=turn_id,assistant_entry_id=None,tool_call_id=call_id,
                request=CodeModeToolRequest(scope.session_id,'test',None,FrozenModelCallMemoryContext()),
                canonical_facts=CodeModeCallFacts(permission,SessionContentRange(0)))
            expected_instructions = ['first original'] if blocked else ['first original','second original']
            assert len(executor.calls) == len(expected_instructions)
            assert [request.public_stdin['tool_name'] for request in executor.calls] == ['run_agent'] * len(expected_instructions)
            assert [request.public_stdin['tool_input']['instruction'] for request in executor.calls] == expected_instructions
            assert all(request.public_stdin['tool_use_id'] == call_id for request in executor.calls)
            assert all(request.public_stdin['input_origin'] == 'CODE_MODE_CALL' for request in executor.calls)
            assert causal.invocation_id == call_id
            assert result.decision is (GateDecision.BLOCK if blocked else GateDecision.PROCEED)
            if blocked:
                assert result.reason == 'member refused'
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
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host
    (tmp_path/'source.txt').write_text('native-value\n')
    cells, observations = [], []
    native_seen = False
    async def stream(request):
        nonlocal native_seen
        index = len(model.requests)
        messages = [m for m in request.compiled_input.messages if m.role is MessageRole.TOOL_RESULT]
        if index == 1:
            payloads = ()
            for label,duration in (('A',0.4),('B',0.05)):
                code = 'await tools.exec_process(' + json.dumps({'command':f'sleep {duration}; printf {label} >> order.txt','yield_time_ms':1000}) + ');return ' + json.dumps(label) + ';'
                payloads += _tool_call('exec_cell',random_id('call'),{'code':code})
        elif index == 2:
            assert len(messages) == 2
            for message in messages:
                observed = _native_fields(text_part_values(message.content)[0])
                assert observed['meta']['status']=='SUCCESS'
                cells.append(observed['target']['cell_id'])
                observations.append(observed)
            payloads = _tool_call('read_file',random_id('call'),{'target':{'kind':'PATH','path':'source.txt'}})
        else:
            if index == 3:
                assert 'native-value' in text_part_values(messages[-1].content)[0]
                native_seen = True
            payloads = _text(random_id('block'),'BOTH_CELLS_SETTLED')
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
            records = await _settled_formal_records(session, expected=2)
            assert (tmp_path/'order.txt').read_text() == 'BA'
            returned = [record['text'] for record in records if record['kind'] == 'return']
            assert sorted(returned) == ['A','B']
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('scenario_kind', ['post_failure','later_block'])
def test_batch_hooks_block_atomically_and_keep_accepted_work_after_post_failure(scenario_kind, tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    from time import monotonic
    import pulsara_agent.conversation_kernel.host as kernel_host
    root_session_id, cell_id = None, None
    root_calls = 0
    pages, member_posts, diagnostics, member_pres = [], [], [], []
    original_post = ToolBatchExecutor._dispatch_post_tool
    original_pre = ToolBatchExecutor._dispatch_pre_tool
    original_offer = ToolBatchExecutor._offer_operational_best_effort
    async def fail_one_member(owner, **values):
        member = values.get('_member_index')
        if member is not None:
            member_posts.append(member)
        if member == 0 and scenario_kind == 'post_failure':
            raise RuntimeError('injected member post failure after actual batch admission')
        return await original_post(owner, **values)
    def observe_diagnostic(owner, offer):
        if offer.public_payload.get('outcome') == 'POST_TOOL_HOOK_FAILED':
            diagnostics.append(offer.public_payload)
        original_offer(owner, offer)
    async def block_second_member(owner, **values):
        outcome, causal = await original_pre(owner, **values)
        if values.get('_member_index') is not None:
            member_pres.append(values['_member_index'])
        if scenario_kind == 'later_block' and values.get('_member_index') == 1:
            from pulsara_agent.hooks.contracts import GateDecision
            return replace(outcome, decision=GateDecision.BLOCK, reason='second member refused'), causal
        return outcome, causal
    monkeypatch.setattr(ToolBatchExecutor,'_dispatch_pre_tool',block_second_member)
    monkeypatch.setattr(ToolBatchExecutor,'_dispatch_post_tool',fail_one_member)
    monkeypatch.setattr(ToolBatchExecutor,'_offer_operational_best_effort',observe_diagnostic)
    async def stream(request):
        nonlocal root_calls,cell_id
        if request.session_id != root_session_id:
            payloads = _text(random_id('block'),'CHILD_COMPLETED')
        else:
            root_calls += 1
            if root_calls == 1:
                code = 'try { const admitted=await tools.run_agent([{instruction:"first work",work_title:"first title"},{instruction:"second work",work_title:"second title"}]);const finished=await tools.wait_agent({agents:[...new Map(admitted.content.map(p=>p.value.agent).map(a=>[a.agent_id,a])).values()],settle:"all"});console.log({status:admitted.meta.status,records:admitted.content.map(p=>p.value)});console.log(finished); } catch(error) { console.log({rejected:error.code}); }'
                payloads = _tool_call('exec_cell',random_id('call'),{'code':code})
            else:
                if root_calls == 2:
                    message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
                    page = _native_fields(text_part_values(message.content)[0])
                    assert page['meta']['status']=='SUCCESS'
                    pages.append(page)
                    cell_id = page['target']['cell_id']
                payloads = _text(random_id('block'),'BATCH_ACCEPTED')
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
            records = await _settled_formal_records(session, expected=2 if scenario_kind == 'post_failure' else 1)
            batch = json.loads(next(record['text'] for record in records if record['kind']=='log'))
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute("SELECT r.result_state,r.attempt_id FROM pulsara_v3.tool_results r JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id WHERE i.session_id=%s AND i.source_kind='CODE_MODE_CALL' AND i.tool_name='run_agent'",(root_session_id,)).fetchall()
                assert len(rows) == 1
                if scenario_kind != 'post_failure':
                    expected = 'PERMISSION_DENIED'
                    assert batch == {'rejected':expected}
                    assert rows[0][0] == expected
                    assert rows[0][1] is None
                    assert member_pres == [0, 1]
                    assert connection.execute('SELECT count(*) FROM pulsara_v3.sessions WHERE root_session_id=%s',(root_session_id,)).fetchone()[0] == 0
                    assert not diagnostics
                else:
                    assert batch['status']=='SUCCESS'
                    assert len(batch['records'])==2
                    assert member_posts == [0,1]
                    assert len(diagnostics) == 1 and diagnostics[0]['member_index'] == 0
                    work_ids = [item['work']['work_id'] for item in batch['records']]
                    assert connection.execute('SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE id=ANY(%s)',(work_ids,)).fetchone()[0] == 2
                    titles = dict(connection.execute('SELECT id,work_title FROM pulsara_v3.prompt_queue_items WHERE id=ANY(%s)',(work_ids,)).fetchall())
                    assert [titles[work] for work in work_ids] == ['first title', 'second title']
                    original = connection.execute("SELECT original_arguments FROM pulsara_v3.tool_invocations WHERE session_id=%s AND tool_name='run_agent'",(root_session_id,)).fetchone()[0]
                    assert [item['parameters']['work_title'] for item in original['members']] == ['first title', 'second title']
                    assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE id=%s',(rows[0][1],)).fetchone()[0] == 1
                    assert rows[0][0] == 'SUCCESS' and rows[0][1] is not None
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('permission_mode', ['read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'])
def test_role_surface_keeps_control_tools_visible_and_rejects_restricted_execution(permission_mode, tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.identity import random_id
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
    from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from tests.support.round3 import prepare_test_direct_tool_surface
    operations = {'exec_cell','query_cells','manage_cell','run_agent','query_agents','send_agent_message','wait_agent','wait_message','stop_agent'}
    mode = PermissionMode(permission_mode)
    async def scenario():
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path,host_owner_id='mode-host',session_id=random_id('session'),
            live_bus=LiveAgentEventBus(),authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        from pulsara_agent.code_mode import CodeModeHost
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset(operations-{'exec_cell','query_cells','manage_cell'}),
            validate_arguments=lambda **_:None, bind_cell_observer=lambda _:None))
        port.bind_code_mode_port(CodeModeHost())
        try:
            surface = prepare_test_direct_tool_surface(port,conversation_scope_kind=ModelInputScopeKind.ROOT,
                effective_permission_mode=mode)
            names = {item.name for item in surface.model_surface.tool_specs}
            assert names.intersection(operations) == operations - {'run_agent', 'wait_agent', 'wait_message'}
            if mode is not PermissionMode.BYPASS_PERMISSIONS:
                prefix = tuple(item.canonical_bytes for item in surface.model_surface.tool_specs)
                borrow = port.borrow_tool_surface(surface)
                permission = build_run_permission_snapshot(snapshot_id=random_id('permission'),requested_mode=mode,
                    effective_mode=mode,admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
                agent = {'kind':'AGENT','agent_id':random_id('session')}
                arguments = {'exec_cell':{'code':'return 1;'},'manage_cell':{'action':'wait','target':{'kind':'CELL','cell_id':'cell:'+'a'*26}},'query_cells':{},
                    'run_agent':{'instruction':'work'},'query_agents':{'target':agent},
                    'send_agent_message':{'target':agent,'message':'hello'},'wait_agent':{'agents':[agent]},'wait_message':{},'stop_agent':{'target':agent}}
                try:
                    for name in operations:
                        authorization = await port.authorize(tool_name=name,arguments=arguments[name],tool_call_id=random_id('call'),
                            turn_id=random_id('turn'),assistant_entry_id=random_id('entry'),permission_snapshot=permission,
                            surface_borrow=borrow,memory_context=FrozenModelCallMemoryContext())
                        assert authorization.kind is (KernelToolAuthorizationKind.TOOL_UNAVAILABLE if name in {'run_agent', 'wait_agent', 'wait_message'} else KernelToolAuthorizationKind.PERMISSION_DENIED)
                    assert tuple(item.canonical_bytes for item in surface.model_surface.tool_specs) == prefix
                finally:
                    borrow.close()
        finally:
            await port.aclose()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('name,arguments', [('run_agent', {'instruction': 'must not create work'}),
    ('wait_agent', {'targets': []}), ('decide', {'condition': 'x', 'inputs': []})])
def test_new_root_schema_rejects_native_code_only_call_and_continues(name, arguments, tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    from time import monotonic
    import pulsara_agent.conversation_kernel.host as kernel_host
    observed = []

    async def stream(request):
        if len(model.requests) == 1:
            events = _tool_call(name, random_id('call'), arguments)
        else:
            message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
            observed.append(text_part_values(message.content)[0])
            events = _text(random_id('block'), 'CONTINUED_AFTER_UNAVAILABLE')
        for event in events:
            yield event
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, 'DirectKernelModelPort', lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, 'load_configs', lambda *_a, **_kw: ())
    runtime = test_model_runtime(model_id='test-pro', wire_api='openai_chat_completions',
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text('try the removed native entry'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'CONTINUED_AFTER_UNAVAILABLE'
            assert len(observed) == 1 and 'exec_cell' in observed[0]
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic()+10) as connection:
                assert connection.execute('SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s',
                    (session.session_id,)).fetchone()[0] == 0
                assert connection.execute('SELECT count(*) FROM pulsara_v3.sessions WHERE root_session_id=%s',
                    (session.session_id,)).fetchone()[0] == 0
                assert connection.execute('SELECT result_state FROM pulsara_v3.tool_results WHERE session_id=%s',
                    (session.session_id,)).fetchone()[0] == 'TOOL_UNAVAILABLE'
        finally:
            await core.shutdown()
    asyncio.run(scenario())
