"""Installed child schemas remain frozen while obsolete execution is rejected."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptor
from pulsara_agent.conversation_kernel import tool_runtime
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.model_input.lowering import decode_tool_result_observation
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_session_host import _delegate, _delegation_progress, eventually
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


def test_installed_child_old_wait_rejects_without_attempt_and_retains_prefix(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    descriptor = builtin_tool_descriptor('wait_agent')
    schema = thaw_tool_json_object(descriptor.input_schema)
    schema['properties']['targets'] = schema['properties'].pop('agents')
    schema['required'] = ['targets']
    old_descriptor = replace(descriptor, input_schema=schema)

    def old_catalog(name, *, code_mode=False):
        return old_descriptor if name == 'wait_agent' else builtin_tool_descriptor(name, code_mode=code_mode)

    class Model(_DetachedDraftModel):
        def __init__(self):
            super().__init__()
            self.root_id = None
            self.target = None
            self.phase = 'install'
            self.counts = {}
            self.children = {}
            self.results = {}
            self.calls = {}

        async def stream(self, request):
            self.requests.append(request)
            if request.session_id == self.root_id:
                if progress := _delegation_progress(self, request):
                    for event in progress:
                        yield event
                    return
                index = self.counts.get(request.turn_id, 0)
                self.counts[request.turn_id] = index + 1
                arguments = {'instruction': self.phase}
                if self.target is not None:
                    arguments['target'] = {'kind': 'AGENT', 'agent_id': self.target}
                events = _delegate(self, random_id('call'), arguments) if index == 0 else _text('ROOT_FINISHED')
            else:
                self.children.setdefault(self.phase, []).append(request)
                index = self.counts.get(request.turn_id, 0)
                self.counts[request.turn_id] = index + 1
                if self.phase == 'install':
                    events = _text('INSTALLED_OLD_PREFIX')
                elif index == 0:
                    call = random_id('call')
                    self.calls[self.phase] = call
                    key = 'targets' if self.phase == 'retained' else 'agents'
                    events = _tool_call('wait_agent', call,
                        {key: [{'kind': 'AGENT', 'agent_id': 'ROOT'}], 'timeout_seconds': 0})
                else:
                    result = next(message for message in reversed(request.compiled_input.messages)
                                  if message.role == 'tool_result')
                    observed = decode_tool_result_observation(text_part_values(result.content)[0])
                    self.results.setdefault(self.phase, []).append(observed)
                    events = (_tool_call('query_agents', random_id('call'), {}) if index == 1
                              else _text('CHILD_CONTINUED'))
            for event in events:
                yield event

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        model = Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id

        async def run_phase():
            await asyncio.wait_for(root.run_turn(PromptContent.text(model.phase), command_id=random_id('command'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 15)
            async def completed():
                requests = model.children.get(model.phase, [])
                if not requests:
                    return False
                child_id = requests[0].session_id
                works = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=child_id, limit=10,
                    deadline_monotonic=monotonic()+10)
                child = core._live_agent_session(child_id)
                return works and all(work['status'] == 'completed' for work in works) and child._active_task is None
            await eventually(completed)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(tool_runtime, 'builtin_tool_descriptor', old_catalog)
                await run_phase()
            installed = model.children['install'][0].compiled_input
            old_wait = next(tool for tool in installed.tools if tool.name == 'wait_agent')
            assert 'targets' in thaw_json(old_wait.parameters)['properties']
            assert 'agents' not in thaw_json(old_wait.parameters)['properties']
            model.target = model.children['install'][0].session_id
            model.phase = 'retained'
            await run_phase()
            assert [result['result_state'] for result in model.results['retained']] == ['TOOL_UNAVAILABLE', 'SUCCESS']
            for request in model.children['retained']:
                assert request.compiled_input.system_prompt == installed.system_prompt
                assert tuple(tool.canonical_bytes for tool in request.compiled_input.tools) == tuple(
                    tool.canonical_bytes for tool in installed.tools)
                assert request.compiled_input.messages[:len(installed.messages)] == installed.messages
            assert len({request.turn_id for request in model.children['retained']}) == 1

            # A genuine new child cold epoch installs and executes the new agents contract.
            model.target = None
            model.phase = 'cold'
            await run_phase()
            assert [result['result_state'] for result in model.results['cold']] == ['SUCCESS', 'SUCCESS']
            current = model.children['cold'][0].compiled_input
            new_wait = next(tool for tool in current.tools if tool.name == 'wait_agent')
            assert 'agents' in thaw_json(new_wait.parameters)['properties']
            assert 'targets' not in thaw_json(new_wait.parameters)['properties']
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute('''SELECT i.tool_call_id, b.tool_name, r.result_state, a.id AS attempt_id
                    FROM pulsara_v3.tool_invocations i
                    JOIN pulsara_v3.assistant_message_blocks b
                      ON b.session_id=i.session_id AND b.tool_call_id=i.tool_call_id
                    JOIN pulsara_v3.tool_results r ON r.session_id=i.session_id AND r.invocation_id=i.id
                    LEFT JOIN pulsara_v3.tool_execution_attempts a ON a.session_id=i.session_id AND a.invocation_id=i.id
                    WHERE i.session_id=ANY(%s) ORDER BY b.tool_name''',
                    ([model.children['retained'][0].session_id, model.children['cold'][0].session_id],)).fetchall()
                by_call = {row['tool_call_id']: row for row in rows}
                old = by_call[model.calls['retained']]
                assert old['result_state'] == 'TOOL_UNAVAILABLE' and old['attempt_id'] is None
                new = by_call[model.calls['cold']]
                assert new['result_state'] == 'SUCCESS' and new['attempt_id'] is not None
                queries = [row for row in rows if row['tool_name'] == 'query_agents']
                assert len(queries) == 2
                assert all(row['result_state'] == 'SUCCESS' and row['attempt_id'] is not None for row in queries)
                assert len(rows) == 4
                assert connection.execute('SELECT count(*) AS n FROM pulsara_v3.sessions WHERE root_session_id=%s',
                    (root.session_id,)).fetchone()['n'] == 2
                assert connection.execute("SELECT count(*) AS n FROM pulsara_v3.prompt_queue_items WHERE source_session_id=%s AND input_origin='AGENT_INSTRUCTION'",
                    (root.session_id,)).fetchone()['n'] == 3
            assert not root.code_mode_cells()
            assert not root._code_mode.pending_output_cells()
        finally:
            await core.shutdown()

    asyncio.run(scenario())
