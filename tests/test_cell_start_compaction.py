"""A queued Cell input survives first-call compaction and late steer."""

import asyncio
from dataclasses import replace
import json

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
        try:
            first = await root.run_turn(
                PromptContent.text('Run the script; process only its explicit output.'),
                command_id=random_id('command'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            assert len(model.root_requests) == 2
            assert root.active_root_turn_id() is None
            usage = await root.read_context_usage()
            ratio = usage['input_tokens'] / usage['input_budget_tokens']
            root._compaction.policy = replace(
                root._compaction.policy, automatic_enabled=True,
                auto_trigger_ratio=ratio * .95,
                post_compaction_target_ratio=ratio * .85, minimum_reclaim_tokens=1,
            )
            release.touch()
            await asyncio.wait_for(model.summary.started.wait(), 20)
            active = root.active_root_turn_id()
            assert active is not None and active != first.turn_id
            inputs = _rows(root,
                'SELECT q.delivery_mode,q.status,q.consumed_entry_id,e.turn_id,e.entry_kind,'
                'e.inline_content,t.initial_entry_id FROM pulsara_v3.prompt_queue_items q '
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
            assert json.loads(bytes(item['inline_content']))['record']['text'] == _OUTPUT
            summary_text = _render(model.summary.contexts[0].messages)
            assert _OUTPUT not in summary_text
            accepted = await root.steer_active_turn(
                command_id=random_id('command'), target_turn_id=active,
                content=PromptContent.text(_STEER),
            )
            assert accepted.status == 'PENDING'
            model.summary.release.set()
            async with asyncio.timeout(20):
                while len(model.root_requests) < 3 or root.active_root_turn_id() is not None:
                    await asyncio.sleep(.01)
            assert len(model.summary.calls) == 1 and len(model.root_requests) == 3
            ordinary = model.root_requests[2]
            assert ordinary.turn_id == active
            rendered = _render(ordinary.compiled_input.messages)
            assert rendered.index(_OUTPUT) < rendered.index(_STEER)
            assert 'CELL_INPUT_HANDLED' not in summary_text
        finally:
            release.touch()
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())
