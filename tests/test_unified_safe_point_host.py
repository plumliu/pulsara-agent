"""Host-level FIFO delivery across a real, non-preempted tool batch."""
from __future__ import annotations

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_session_host import eventually
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


def test_mixed_inputs_wait_for_real_batch_then_arrive_in_fifo_order(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    class Model(_DetachedDraftModel):
        def __init__(self):
            super().__init__()
            self.root_id = None
            self.phase = 'create'
            self.steps = {}
            self.child_started = asyncio.Event()
            self.child_release = asyncio.Event()
            self.batch_started = asyncio.Event()
            self.batch_release = asyncio.Event()
            self.batch_requests = []

        async def stream(self, request):
            if request.session_id != self.root_id:
                self.child_started.set()
                await self.child_release.wait()
                events = _text('FIFO_AGENT_COMPLETION')
            else:
                step = self.steps.get(request.turn_id, 0)
                self.steps[request.turn_id] = step + 1
                if self.phase == 'create':
                    events = (_tool_call('run_agent', 'call:create-fifo-agent',
                                         {'instruction': 'Wait for the test gate.'})
                              if step == 0 else _text('AGENT_CREATED'))
                else:
                    self.batch_requests.append(request)
                    if step == 0:
                        self.batch_started.set()
                        await self.batch_release.wait()
                        events = [*_tool_call('write_file', 'call:fifo-first', {
                            'target': {'kind': 'PATH', 'path': str(tmp_path / 'first.txt')},
                            'content': 'first side effect',
                        }), *_tool_call('write_file', 'call:fifo-second', {
                            'target': {'kind': 'PATH', 'path': str(tmp_path / 'second.txt')},
                            'content': 'second side effect',
                        })]
                    else:
                        events = _text('FIFO_FINISHED')
            for event in events:
                yield event

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        model = Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        running = None

        def rows(sql, parameters):
            with root.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic() + 10,
            ) as connection:
                return connection.execute(sql, parameters).fetchall()

        try:
            await root.run_turn(PromptContent.text('Create the source Agent.'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(model.child_started.wait(), 15)
            agents = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=monotonic() + 10)
            assert len(agents) == 1
            child_id = agents[0]
            works = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id, agent_id=child_id, limit=10,
                deadline_monotonic=monotonic() + 10)
            assert len(works) == 1 and works[0]['status'] == 'running'
            work_id = works[0]['work_id']
            model.phase = 'batch'
            running = asyncio.create_task(root.run_turn(PromptContent.text('Execute the batch.'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
            await asyncio.wait_for(model.batch_started.wait(), 15)
            turn_id = root.active_root_turn_id()
            assert turn_id is not None
            steer = await root.steer_active_turn(command_id=random_id('command'),
                content=PromptContent.text('FIFO_HUMAN_STEER'), target_turn_id=turn_id)
            assert steer.status == 'PENDING'
            message = await core._send_agent_message(root.session_id, 'FIFO_AGENT_MESSAGE',
                SimpleNamespace(session_id=child_id, work_id=work_id))
            assert message['delivery'] == 'queued'
            model.child_release.set()

            async def child_completed():
                work = await root._io.run(root.repository.query_agent_work,
                    root_session_id=root.session_id, work_id=work_id,
                    deadline_monotonic=monotonic() + 10)
                return work if work['status'] == 'completed' else None

            await eventually(child_completed)
            completed_work = await child_completed()
            assert completed_work['final'] == 'FIFO_AGENT_COMPLETION'
            pending = rows("""SELECT id, input_origin, source_session_id, source_work_id
                FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND target_turn_id=%s
                AND status='PENDING' ORDER BY queue_sequence""", (root.session_id, turn_id))
            assert [r['input_origin'] for r in pending] == ['HUMAN_STEER', 'AGENT_MESSAGE']
            assert pending[1]['source_session_id'] == child_id
            assert pending[1]['source_work_id'] == work_id
            assert not rows("""SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s
                AND input_origin='AGENT_MATERIAL' AND source_work_id=%s""", (root.session_id, work_id))
            assert not running.done()
            model.batch_release.set()
            result = await asyncio.wait_for(running, 15)
            assert result.turn_id == turn_id
            assert (tmp_path / 'first.txt').read_text() == 'first side effect'
            assert (tmp_path / 'second.txt').read_text() == 'second side effect'
            tool_results = rows("""SELECT r.result_state, r.attempt_id, e.entry_sequence
                FROM pulsara_v3.tool_results r JOIN pulsara_v3.tool_invocations i
                  ON i.session_id=r.session_id AND i.id=r.invocation_id
                JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id
                WHERE i.session_id=%s AND i.turn_id=%s ORDER BY e.entry_sequence""",
                (root.session_id, turn_id))
            assert [r['result_state'] for r in tool_results] == ['SUCCESS', 'SUCCESS']
            assert all(r['attempt_id'] is not None for r in tool_results)
            consumed = rows("""SELECT q.id, q.status, e.entry_sequence FROM pulsara_v3.prompt_queue_items q
                JOIN pulsara_v3.transcript_entries e ON e.session_id=q.session_id AND e.id=q.consumed_entry_id
                WHERE q.session_id=%s AND q.target_turn_id=%s ORDER BY e.entry_sequence""",
                (root.session_id, turn_id))
            assert [r['id'] for r in consumed] == [r['id'] for r in pending]
            assert all(r['status'] == 'CONSUMED' for r in consumed)
            assert tool_results[-1]['entry_sequence'] < consumed[0]['entry_sequence']
            first = model.batch_requests[0].compiled_input
            after = model.batch_requests[-1].compiled_input
            assert after.system_prompt == first.system_prompt and after.tools == first.tools
            assert after.messages[:len(first.messages)] == first.messages
            parts = [text for m in after.messages for text in text_part_values(m.content)]
            markers = ['FIFO_HUMAN_STEER', 'FIFO_AGENT_MESSAGE']
            assert 'FIFO_AGENT_COMPLETION' not in parts
            assert all(parts.count(marker) == 1 for marker in markers)
            assert [parts.index(marker) for marker in markers] == sorted(parts.index(marker) for marker in markers)
        finally:
            model.child_release.set()
            model.batch_release.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())
