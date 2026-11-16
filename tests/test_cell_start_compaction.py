"""A queued Cell input survives first-call compaction and late steer."""

import asyncio
from dataclasses import replace

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_compaction_request_start_integration import _Model, _rows, _render
from tests.test_round4_plan_host import _open_test_session, _text, _tool_call
from tests.test_runtime_handoff_retirement_integration import handoff_database as handoff_database


pytestmark = pytest.mark.postgres
_OUTPUT = 'CELL_NEW_TURN_EXACT_INPUT'
_STEER = 'STEER_DURING_CELL_FIRST_SUMMARY'


class _CellStartModel(_Model):
    async def _stream(self, request):
        self.root_requests.append(request)
        index = len(self.root_requests)
        if index == 1:
            events = _tool_call('exec_cell', random_id('call'), {'code': (
                'await tools.exec_process({command:"while [ ! -f release-cell ]; do sleep 0.01; done",'
                'yield_time_ms:30000});'
                f'console.log("{_OUTPUT}");'
            )})
        elif index == 2:
            events = _text('PREDECESSOR_DETAILS ' + 'completed investigation evidence ' * 6000)
        elif index==3:
            events=_text('PREDECESSOR_ACKNOWLEDGED')
        else:
            events = _text('CELL_INPUT_HANDLED')
        for event in events:
            yield event


def test_cell_new_turn_first_summary_preserves_entry_and_steer(
    tmp_path, monkeypatch, handoff_database,
):
    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        model = _CellStartModel()
        core, root = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=handoff_database[0],
        )
        release = root.workspace.workspace_root / 'release-cell'
        compactions=[]
        quotes=[]
        from pulsara_agent.conversation_kernel.compaction import coordinator as coordinator_module
        trigger=coordinator_module.should_trigger_compaction
        def observe_trigger(**kwargs):
            view=kwargs['source_view']
            quotes.append({'estimate':view.provider_wire_quote.budget_input_tokens,'budget':view.normal_compile_binding.effective_input_budget_tokens,'ratio':kwargs['policy'].auto_trigger_ratio,'force':kwargs['force']})
            return trigger(**kwargs)
        monkeypatch.setattr(coordinator_module,'should_trigger_compaction',observe_trigger)
        execute=root._runner.compaction.execute_active
        async def observed(**kwargs):
            result=await execute(**kwargs)
            compactions.append(result.outcome)
            return result
        monkeypatch.setattr(root._runner.compaction,'execute_active',observed)
        try:
            first = await root.run_turn(
                PromptContent.text('Run the script; process only its explicit output.'),
                command_id=random_id('command'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            assert len(model.root_requests) == 2
            assert root.active_root_turn_id() is None
            # Automatic compaction may summarize only a consumed prefix.
            # Install the long accepted final in a real subsequent request.
            acknowledgement=await root.run_turn(PromptContent.text('Acknowledge the completed evidence before the Cell input.'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert len(model.root_requests)==3
            usage = await root.read_context_usage()
            ratio = usage['input_tokens'] / usage['input_budget_tokens']
            root._compaction.policy = replace(
                root._compaction.policy, automatic_enabled=True,
                # The usage preview includes observations omitted from the source
                # projection. Leave room for that measured difference; the
                # actual source quote below must independently cross the trigger.
                auto_trigger_ratio=ratio * .9,

            )
            release.touch()
            try:
                await asyncio.wait_for(model.summary.started.wait(),20)
            except TimeoutError as error:
                raise AssertionError({'compactions':compactions,'requests':len(model.root_requests),'active':root.active_root_turn_id(),'summary_calls':len(model.summary.calls),'usage':usage,'ratio':ratio,'quotes':quotes}) from error
            assert quotes and not quotes[-1]['force']
            assert quotes[-1]['estimate'] >= int(quotes[-1]['budget']*quotes[-1]['ratio'])
            active = root.active_root_turn_id()
            assert active is not None and active not in {first.turn_id,acknowledgement.turn_id}
            inputs = _rows(root,
                'SELECT q.delivery_mode,q.status,q.consumed_entry_id,e.turn_id,e.entry_kind,'
                'q.source_cell_id,q.source_record_sequence,q.cell_output_kind,t.initial_entry_id FROM pulsara_v3.prompt_queue_items q '
                'JOIN pulsara_v3.transcript_entries e ON e.session_id=q.session_id AND e.id=q.consumed_entry_id '
                'JOIN pulsara_v3.turns t ON t.session_id=e.session_id AND t.id=e.turn_id '
                "WHERE q.session_id=%s AND q.input_origin='CELL_OUTPUT'", (root.session_id,),
            )
            assert len(inputs) == 1
            item = inputs[0]
            assert item['delivery_mode'] == 'NEW_TURN' and item['status'] == 'CONSUMED'
            assert item['turn_id'] == active
            assert item['consumed_entry_id'] == item['initial_entry_id']
            assert item['entry_kind'] == 'TERMINAL_OBSERVATION'
            assert item['source_cell_id'] and item['source_record_sequence']==1 and item['cell_output_kind']=='log'
            from pulsara_agent.conversation_kernel.content_parts import read_content_parts
            from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
            from psycopg.rows import dict_row
            with root.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,row_factory=dict_row,deadline_monotonic=__import__('time').monotonic()+30) as connection:
                parts=read_content_parts(connection,entry_id=item['consumed_entry_id'])
                assert [part.value for part in parts]==[_OUTPUT]
            summary_text = _render(model.summary.contexts[0].messages)
            assert _OUTPUT not in summary_text
            accepted = await root.steer_active_turn(
                command_id=random_id('command'), target_turn_id=active,
                content=PromptContent.text(_STEER),
            )
            assert accepted.status == 'PENDING'
            model.summary.release.set()
            async with asyncio.timeout(20):
                while len(model.root_requests) < 4 or root.active_root_turn_id() is not None:
                    await asyncio.sleep(.01)
            assert len(model.summary.calls) == 1 and len(model.root_requests) == 4
            ordinary = model.root_requests[3]
            assert ordinary.turn_id == active
            rendered = _render(ordinary.compiled_input.messages)
            assert rendered.index(_OUTPUT) < rendered.index(_STEER)
            assert 'CELL_INPUT_HANDLED' not in summary_text
        finally:
            release.touch()
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())
