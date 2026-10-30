"""Real Host/queue/runner execution for reusable Agent sessions."""
from __future__ import annotations

import asyncio
from time import monotonic
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _tool_call, _text

pytestmark = pytest.mark.postgres


class AgentModel(_DetachedDraftModel):
    def __init__(self):
        super().__init__()
        self.root_id = None
        self.agent_id = None
        self.root_turns = {}
        self.child_requests = []
        self.first_child_started = asyncio.Event()
        self.first_child_release = asyncio.Event()

    async def stream(self, request):
        self.requests.append(request)
        if request.session_id == self.root_id:
            index = self.root_turns.get(request.turn_id, 0)
            self.root_turns[request.turn_id] = index + 1
            if index == 0:
                parameters = {'instruction': 'CHILD_FIRST' if self.agent_id is None else 'CHILD_SECOND'}
                if self.agent_id is not None:
                    parameters['target'] = {'kind': 'AGENT', 'agent_id': self.agent_id}
                events = _tool_call('run_agent', random_id('call'), parameters)
            else:
                events = _text('ROOT_FINISHED')
        else:
            self.child_requests.append(request)
            if len(self.child_requests) == 1:
                self.first_child_started.set()
                await self.first_child_release.wait()
                events = _text('CHILD_FIRST_FINAL')
            else:
                events = _text('CHILD_SECOND_FINAL')
        for event in events:
            yield event


async def eventually(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize('unload_between', [False, True])
def test_same_agent_uses_real_common_engine_and_retains_prefix_across_work(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, unload_between,
):
    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        model = AgentModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        try:
            await asyncio.wait_for(root.run_turn(PromptContent.text('Delegate the first investigation'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 15)
            await asyncio.wait_for(model.first_child_started.wait(), 15)
            ids = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=monotonic()+10)
            assert len(ids) == 1
            model.agent_id = ids[0]
            child = core._live_agent_session(model.agent_id)
            first_task = child._active_task
            assert child.root_session_id == root.session_id
            assert child._runner is not root._runner
            assert child._tools._terminal is root._tools._terminal
            assert child._tools.terminal_monitor_coordinator is root._tools.terminal_monitor_coordinator
            assert child._tools.todo_owner is not root._tools.todo_owner
            assert root.active_root_turn_id() is None
            # Ordinary ROOT final does not cancel the independently admitted turn.
            model.first_child_release.set()
            async def first_completed():
                rows = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=model.agent_id,
                    limit=10, deadline_monotonic=monotonic()+10)
                return rows and rows[0]['status'] == 'completed'
            await eventually(first_completed)
            if unload_between:
                await asyncio.shield(first_task)
                async def settled_child():
                    return child.active_root_turn_id() is None
                await eventually(settled_child)
                with root.repository._writer_transaction(child._lease.guard, deadline_monotonic=monotonic()+10) as connection:
                    connection.execute('UPDATE pulsara_v3.sessions SET idle_since=%s WHERE id=%s',
                        (datetime.now(timezone.utc)-timedelta(hours=2), child.session_id))
                await core._agent_runtime._advance()
                if retirement := core._session_retirements.get(model.agent_id):
                    await asyncio.shield(retirement.settlement)
                assert child._closed, {
                    'runtime':child._has_runtime_work(), 'terminal':child.archive_has_terminal_work(),
                    'safe_point_idle':child._runner._safe_point.is_idle(),
                    'retirements':tuple(core._session_retirements),
                    'state':await root._io.run(root.repository.read_agent_runtime_state,
                        session_id=model.agent_id,deadline_monotonic=monotonic()+10),
                }
                assert core._live_agent_session(model.agent_id) is None
            await asyncio.wait_for(root.run_turn(PromptContent.text('Continue with the same investigator'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 15)
            async def second_completed():
                rows = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=model.agent_id,
                    limit=10, deadline_monotonic=monotonic()+10)
                return len(rows) == 2 and all(r['status'] == 'completed' for r in rows)
            await eventually(second_completed)
            first, second = (r.compiled_input for r in model.child_requests)
            assert 'pulsara-viz' not in first.system_prompt
            if not unload_between:
                assert first.system_prompt == second.system_prompt
                assert first.tools == second.tools
                assert second.messages[:len(first.messages)] == first.messages
            else:
                reloaded = core._live_agent_session(model.agent_id)
                assert reloaded is not child and reloaded.session_id == child.session_id
            rendered = '\n'.join(t for message in second.messages for t in text_part_values(message.content))
            assert rendered.index('CHILD_FIRST') < rendered.index('CHILD_FIRST_FINAL') < rendered.index('CHILD_SECOND')
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute("SELECT input_origin,inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (root.session_id,)).fetchall()
                assert any(r['input_origin'] == 'AGENT_MATERIAL' and b'CHILD_FIRST_FINAL' in bytes(r['inline_content'] or b'') for r in rows), ([(r['input_origin'], bytes(r['inline_content'] or b'')[:300]) for r in rows], root._agent_runtime._completion_notifications, connection.execute('SELECT id,input_origin,status,target_turn_id,source_work_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s', (root.session_id,)).fetchall())
            await core.close_session(root.host_session_id)
            assert child._closed
            assert not core._sessions
        finally:
            model.first_child_release.set()
            await core.shutdown()
    asyncio.run(scenario())


class StopDependencyModel(AgentModel):
    def __init__(self):
        super().__init__()
        self.first_work_id = None
        self.control_turn = False

    async def stream(self, request):
        if request.session_id != self.root_id or not self.control_turn:
            async for event in super().stream(request):
                yield event
            return
        self.requests.append(request)
        index = self.root_turns.get(request.turn_id, 0)
        self.root_turns[request.turn_id] = index+1
        if index == 0:
            events = _tool_call('run_agent', random_id('call'), {
                'target': {'kind':'AGENT','agent_id':self.agent_id},
                'instruction':'INDEPENDENT_NEXT'})
        elif index == 1:
            events = _tool_call('run_agent', random_id('call'), {
                'instruction':'DEPENDENT_MUST_NOT_START',
                'depends_on':[{'kind':'WORK','work_id':self.first_work_id}]})
        elif index == 2:
            events = _tool_call('stop_agent', random_id('call'), {
                'target':{'kind':'WORK','work_id':self.first_work_id}})
        else:
            events = _text('ROOT_CONTROL_FINISHED')
        for event in events:
            yield event


def test_exact_work_stop_cascades_dependencies_but_preserves_next_fifo_work(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path/'home'))
        model = StopDependencyModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        await root.set_subagent_capacity(1)
        try:
            await asyncio.wait_for(root.run_turn(PromptContent.text('Start blocked investigation'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS),15)
            await asyncio.wait_for(model.first_child_started.wait(),15)
            agents = await root._io.run(root.repository.list_agent_sessions,
                root_session_id=root.session_id,limit=10,deadline_monotonic=monotonic()+10)
            model.agent_id = agents[0]['agent_id']
            works = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id,agent_id=model.agent_id,limit=10,deadline_monotonic=monotonic()+10)
            model.first_work_id = works[0]['work_id']
            model.control_turn = True
            await asyncio.wait_for(root.run_turn(PromptContent.text('Queue an independent continuation and dependent reviewer, then stop only the first work'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS),15)
            async def all_ended():
                agents = await root._io.run(root.repository.list_agent_sessions,
                    root_session_id=root.session_id,limit=10,deadline_monotonic=monotonic()+10)
                all_works=[]
                for agent in agents:
                    all_works.extend(await root._io.run(root.repository.list_agent_works,
                        root_session_id=root.session_id,agent_id=agent['agent_id'],limit=10,deadline_monotonic=monotonic()+10))
                return len(all_works)==3 and all(w['status'] in {'completed','stopped'} for w in all_works)
            await eventually(all_ended)
            own = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id,agent_id=model.agent_id,limit=10,deadline_monotonic=monotonic()+10)
            assert [w['status'] for w in own] == ['stopped','completed']
            assert own[0]['terminal_reason']=='USER_STOPPED'
            agents = await root._io.run(root.repository.list_agent_sessions,
                root_session_id=root.session_id,limit=10,deadline_monotonic=monotonic()+10)
            reviewer = next(a for a in agents if a['agent_id'] != model.agent_id)
            reviewer_work = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id,agent_id=reviewer['agent_id'],limit=10,deadline_monotonic=monotonic()+10)
            assert reviewer_work[0]['status']=='stopped'
            assert reviewer_work[0]['turn_id'] is None
            assert len(model.child_requests)==2
        finally:
            model.first_child_release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_user_can_stop_old_cell_without_a_bypass_turn_and_reopen_waits_for_settlement(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.code_mode import CodeModeToolResult
    from tests.test_code_mode_runtime import _start
    from pulsara_agent.conversation_kernel.user_control import UserControlRequest, UserControlOperation, UserControlTarget, UserControlTargetKind

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path/'home'))
        model = AgentModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        started, release = asyncio.Event(), asyncio.Event()
        async def invoke(call):
            started.set()
            await release.wait()
            return CodeModeToolResult(value={'physical_result':'settled'})
        try:
            root._launch_permission_mode = PermissionMode.READ_ONLY
            cell_id = await _start(root._code_mode, 'await tools.read_file({});', invoke,
                turn_id='old-source-turn')
            await asyncio.wait_for(started.wait(),10)
            with pytest.raises(RuntimeError, match='not quiescent'):
                await root.prepare_safe_runtime_reopen()
            command_id = random_id('command')
            admission_deadline = root.control_admission_deadline_ms()
            control_request = UserControlRequest(UserControlOperation.STOP_CODE_MODE_CELL, command_id,
                root.session_id, root.host_session_id, UserControlTarget(UserControlTargetKind.CODE_MODE_CELL, cell_id), admission_deadline)
            pending = await root.request_stop_code_mode_cell(command_id=command_id,
                admission_deadline_ms=admission_deadline,
                expected_session_id=root.session_id,expected_host_session_id=root.host_session_id,
                cell_id=cell_id)
            assert pending.status=='PENDING'
            async def stopped():
                observed = await root.query_control_command(control_request)
                return observed.outcome is not None and observed.outcome.status=='SUCCEEDED'
            await eventually(stopped)
            result = (await root.query_control_command(control_request)).outcome
            assert result.user_control.cell.state=='terminated'
            assert result.user_control.cell.pending_calls==1
            assert root.code_mode_cells()[0]['pending_calls']==1
            with pytest.raises(RuntimeError, match='not quiescent'):
                await root.prepare_safe_runtime_reopen()
            # An identical user command observes its existing stop outcome.
            repeated = await root.request_stop_code_mode_cell(command_id=command_id,
                admission_deadline_ms=admission_deadline,
                expected_session_id=root.session_id,expected_host_session_id=root.host_session_id,
                cell_id=cell_id)
            assert repeated.user_control.cell.state=='terminated'
            release.set()
            async def settled():
                return root.code_mode_cells()[0]['pending_calls']==0
            await eventually(settled)
            page = await root._code_mode.observe(cell_id,0)
            assert any(output['kind']=='tool_result' for output in page.output)
            await root._code_mode.acknowledge(page)
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('loaded', [False, True])
def test_actual_admission_wins_stale_idle_retirement_for_loaded_and_unloaded_agent(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, loaded,
):
    import threading

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path/'home'))
        model = AgentModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        entered, release = threading.Event(), threading.Event()
        try:
            model.first_child_release.set()
            await root.run_turn(PromptContent.text('Create investigator'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(model.first_child_started.wait(),10)
            ids = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id,deadline_monotonic=monotonic()+10)
            model.agent_id=ids[0]
            child = core._live_agent_session(model.agent_id)
            async def child_idle():
                return child.active_root_turn_id() is None
            await eventually(child_idle)
            if not loaded:
                await core.close_session(child.host_session_id)
            original = root.repository.admit_agent_requests
            def blocked(*args, **kwargs):
                entered.set()
                assert release.wait(10), 'test did not release real request admission'
                return original(*args, **kwargs)
            monkeypatch.setattr(root.repository,'admit_agent_requests',blocked)
            submitting = asyncio.create_task(root.run_turn(PromptContent.text('Continue investigator'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
            assert await asyncio.to_thread(entered.wait,10)
            with root.repository._writer_transaction(root._lease.guard,deadline_monotonic=monotonic()+10) as connection:
                connection.execute('UPDATE pulsara_v3.sessions SET idle_since=%s WHERE id=%s',
                    (datetime.now(timezone.utc)-timedelta(days=4),model.agent_id))
            stale = await root._io.run(root.repository.read_agent_runtime_state,
                session_id=model.agent_id,deadline_monotonic=monotonic()+10)
            retirement = asyncio.create_task(core._retire_idle_agent(stale,delete=True))
            async def retirement_waiting():
                return model.agent_id in core._session_retirements
            await eventually(retirement_waiting)
            winning_retirement = core._session_retirements[model.agent_id]
            assert not winning_retirement.settlement.done()
            if loaded:
                assert not child._closed
            release.set()
            await asyncio.wait_for(submitting,15)
            await asyncio.wait_for(retirement,15)
            await asyncio.wait_for(asyncio.shield(winning_retirement.settlement),15)
            state = await root._io.run(root.repository.read_agent_runtime_state,
                session_id=model.agent_id,deadline_monotonic=monotonic()+10)
            assert state is not None
            assert state['idle_since'] is None or state['idle_since'] > stale['idle_since']
            if loaded:
                assert not child._closed
            async def finished():
                rows = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id,agent_id=model.agent_id,limit=10,
                    deadline_monotonic=monotonic()+10)
                return len(rows)==2 and all(row['status']=='completed' for row in rows)
            await eventually(finished)
        finally:
            release.set()
            model.first_child_release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_child_first_preparation_adopts_real_common_snapshot_for_next_work(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from tests.test_stage2_conversation_runner import _CompactionScriptedModel
    from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy

    summary = 'CHILD_CONTEXT_SUMMARY: the first investigation completed and the next work must continue it.'
    class Model(_CompactionScriptedModel):
        def __init__(self):
            super().__init__([], summary)
            self.root_id = None
            self.agent_id = None
            self.root_turns = {}
            self.child_requests = []
            self.first_child_started = asyncio.Event()
            self.first_child_release = asyncio.Event()

        def preflight_execution(self, request, *, append_candidate, install_authority):
            from tests.test_round4_plan_host import _PreparedTestExecution
            return _PreparedTestExecution(self, request, append_candidate, install_authority)

        async def stream(self, request):
            if request.session_id != self.root_id and not self.child_requests:
                self.requests.append(request)
                self.child_requests.append(request)
                self.first_child_started.set()
                await self.first_child_release.wait()
                for event in _text('CHILD_FIRST_FINAL '+('disposable long investigation detail '*2500)):
                    yield event
            else:
                async for event in AgentModel.stream(self, request):
                    yield event

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path/'home'))
        model = Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        try:
            await root.run_turn(PromptContent.text('Investigate using the child'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(model.first_child_started.wait(),10)
            ids = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id,deadline_monotonic=monotonic()+10)
            model.agent_id=ids[0]
            child=core._live_agent_session(model.agent_id)
            model.first_child_release.set()
            async def child_idle():
                return child.active_root_turn_id() is None
            await eventually(child_idle)
            child._compaction.policy=ResolvedCompactionPolicy(
                auto_trigger_ratio=0.10, post_compaction_target_ratio=0.08,
                minimum_reclaim_tokens=1,
            )
            compactions=[]
            planning_failures=[]
            fenced=child._runner.compaction._execute_compaction_fenced
            async def recorded_fenced(**kwargs):
                try:
                    return await fenced(**kwargs)
                except Exception as error:
                    planning_failures.append(error)
                    raise
            monkeypatch.setattr(child._runner.compaction, '_execute_compaction_fenced', recorded_fenced)
            execute=child._runner.compaction.execute_active
            async def recorded_compaction(**kwargs):
                result=await execute(**kwargs)
                compactions.append(result)
                return result
            monkeypatch.setattr(child._runner.compaction, 'execute_active', recorded_compaction)
            await root.run_turn(PromptContent.text('Continue the compressed investigator'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            async def next_completed():
                rows=await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id,agent_id=model.agent_id,limit=10,
                    deadline_monotonic=monotonic()+10)
                return len(rows)==2 and all(r['status']=='completed' for r in rows)
            await eventually(next_completed)
            assert compactions and compactions[0].outcome.disposition.value=='COMPACTED', (compactions, planning_failures)
            assert len(model.summary_transport.contexts)==1
            from pulsara_agent.primitives.context import canonical_json_bytes
            assert tuple(canonical_json_bytes({'name':tool.name,'description':tool.description,'parameters':tool.parameters})
                for tool in model.summary_transport.contexts[0].tools) == tuple(
                    tool.canonical_bytes for tool in model.child_requests[0].compiled_input.tools)
            assert model.summary_transport.contexts[0].system_prompt==model.child_requests[0].compiled_input.system_prompt
            rendered='\n'.join(t for m in model.child_requests[1].compiled_input.messages for t in text_part_values(m.content))
            assert summary in rendered
            assert 'CHILD_SECOND' in rendered
            assert 'disposable long investigation detail '*20 not in rendered
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row,deadline_monotonic=monotonic()+10) as connection:
                revisions=connection.execute('SELECT context_snapshot_id FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s',
                    (model.agent_id,model.child_requests[1].turn_id)).fetchall()
                assert any(r['context_snapshot_id']==compactions[0].outcome.snapshot_id for r in revisions)
        finally:
            model.first_child_release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_process_loss_takeover_does_not_replay_running_work_or_settled_tools_and_runs_queued_work(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    import json
    import subprocess
    import sys
    from pathlib import Path
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime
    import pulsara_agent.conversation_kernel.host as kernel_host

    class RecoveryModel(_DetachedDraftModel):
        def __init__(self):
            super().__init__()
            self.agent_id=None
        async def stream(self,request):
            self.requests.append(request)
            assert request.session_id==self.agent_id
            rendered='\n'.join(t for m in request.compiled_input.messages for t in text_part_values(m.content))
            assert 'CHILD_SECOND' in rendered
            for event in _text('QUEUED_WORK_COMPLETED_AFTER_PROCESS_LOSS'):
                yield event

    async def scenario():
        workspace=tmp_path/'workspace'
        workspace.mkdir()
        ready=tmp_path/'ready.json'
        log=open(tmp_path/'lost-process.log','w')
        process=subprocess.Popen([sys.executable,'-m','tests.support.agent_process_loss',str(workspace),
            stage2_migrated_postgres_database.runtime_dsn,str(ready)],stdout=log,stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[1])
        core=None
        try:
            async def prepared():
                if process.poll() is not None:
                    log.flush()
                    raise AssertionError((tmp_path/'lost-process.log').read_text())
                return ready.is_file()
            await eventually(prepared)
            identities=json.loads(ready.read_text())
            process.kill()
            await asyncio.to_thread(process.wait,10)
            assert process.returncode != 0
            model=RecoveryModel()
            model.agent_id=identities['agent_id']
            monkeypatch.setenv('PULSARA_HOME',str(tmp_path/'replacement-home'))
            monkeypatch.setattr(kernel_host,'DirectKernelModelPort',lambda **kwargs:model)
            monkeypatch.setattr(kernel_host.LocalMcpManagementService,'load_configs',lambda *args,**kwargs:())
            core=KernelHostCore.production(model_runtime=test_model_runtime(
                model_id='test-pro',wire_api='openai_chat_completions',
                postgres_dsn=stage2_migrated_postgres_database.runtime_dsn))
            root=await core.resume_session(identities['root_session_id'],
                workspace_input=HostWorkspaceInput(workspace_kind='project',workspace_root=workspace))
            async def recovered():
                work=await root._io.run(root.repository.query_agent_work,
                    root_session_id=root.session_id,work_id=identities['queued_work_id'],
                    deadline_monotonic=monotonic()+10)
                return work['status']=='completed'
            await eventually(recovered)
            stopped=await root._io.run(root.repository.query_agent_work,
                root_session_id=root.session_id,work_id=identities['first_work_id'],
                deadline_monotonic=monotonic()+10)
            assert stopped['status']=='stopped'
            assert stopped['terminal_reason']=='HOST_TAKEOVER'
            assert len(model.requests)==1
            assert (workspace/'effect-count.txt').read_text()=='x'
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row,deadline_monotonic=monotonic()+10) as connection:
                attempts=connection.execute("""SELECT count(*) FROM pulsara_v3.tool_invocations i
                    JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=i.session_id
                        AND b.assistant_entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id
                    JOIN pulsara_v3.tool_results r ON r.session_id=i.session_id AND r.invocation_id=i.id
                    WHERE i.session_id=%s AND b.tool_name='exec_process'""",
                    (identities['agent_id'],)).fetchone()['count']
                assert attempts==1
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait,10)
            log.close()
            if core is not None:
                await core.shutdown()
    asyncio.run(scenario())


def test_live_exec_keeps_bypass_snapshot_after_root_read_only_turn_and_prefix_stays_installed(
    tmp_path,monkeypatch,stage2_migrated_postgres_database,
):
    import json
    class Model(_DetachedDraftModel):
        def __init__(self):
            super().__init__()
            self.first_turn=None
            self.counts={}
        async def stream(self,request):
            self.requests.append(request)
            self.first_turn=self.first_turn or request.turn_id
            index=self.counts.get(request.turn_id,0)
            self.counts[request.turn_id]=index+1
            if request.turn_id==self.first_turn and index==0:
                code='await tools.exec_process({command:"while [ ! -f release-old-cell ]; do sleep 0.01; done",yield_time_ms:30000});'
                code+='console.log(await tools.write_file('+json.dumps({'target':{'kind':'PATH','path':'source-write.txt'},'content':'FROM_SOURCE_BYPASS'})+'));'
                code+='return "OLD_CELL_BYPASS_FROZEN";'
                events=_tool_call('exec_cell',random_id('call'),{'code':code,'yield_time_ms':0})
            else:
                events=_text('ROOT_TURN_FINISHED')
            for event in events:
                yield event
    async def scenario():
        monkeypatch.setenv('PULSARA_HOME',str(tmp_path/'home'))
        model=Model()
        core,root=await _open_test_session(tmp_path=tmp_path,model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,monkeypatch=monkeypatch)
        try:
            first=await root.run_turn(PromptContent.text('Start a bypass script that waits for the gate'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            async def terminal_submitted():
                cells=root.code_mode_cells()
                return bool(cells) and cells[0]['pending_calls']==1
            await eventually(terminal_submitted)
            cell_id=root.code_mode_cells()[0]['cell_id']
            assert not (tmp_path/'source-write.txt').exists()
            second=await root.run_turn(PromptContent.text('Read-only turn: finish while the earlier script waits'),
                command_id=random_id('command'),requested_permission_mode=PermissionMode.READ_ONLY)
            assert second.turn_id!=first.turn_id
            assert root.code_mode_cells()[0]['state']=='running'
            assert not (tmp_path/'source-write.txt').exists()
            (tmp_path/'release-old-cell').write_text('release after read-only turn')
            async def completed():
                return root.code_mode_cells()[0]['state']=='completed'
            await eventually(completed)
            assert (tmp_path/'source-write.txt').read_text()=='FROM_SOURCE_BYPASS'
            original=model.requests[0].compiled_input
            latest=model.requests[-1].compiled_input
            assert latest.system_prompt==original.system_prompt
            assert latest.tools==original.tools
            assert latest.messages[:len(original.messages)]==original.messages
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row,deadline_monotonic=monotonic()+10) as connection:
                modes=connection.execute('SELECT id,effective_permission_mode FROM pulsara_v3.turns WHERE session_id=%s',
                    (root.session_id,)).fetchall()
                assert {r['id']:r['effective_permission_mode'] for r in modes}=={first.turn_id:'bypass-permissions',second.turn_id:'read-only'}
                calls=connection.execute("""SELECT i.turn_id,r.result_state FROM pulsara_v3.tool_invocations i
                    JOIN pulsara_v3.tool_results r ON r.session_id=i.session_id AND r.invocation_id=i.id
                    WHERE i.session_id=%s AND i.source_kind='CODE_MODE_CALL' AND i.tool_name='write_file'""",
                    (root.session_id,)).fetchall()
                assert calls==[{'turn_id':first.turn_id,'result_state':'SUCCESS'}]
            page=await root._code_mode.observe(cell_id,0)
            assert any(output.get('text')=='OLD_CELL_BYPASS_FROZEN' for output in page.output)
            await root._code_mode.acknowledge(page)
        finally:
            (tmp_path/'release-old-cell').write_text('cleanup')
            await core.shutdown()
    asyncio.run(scenario())
