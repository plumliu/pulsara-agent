"""Subprocess fixture deliberately killed after canonical tool settlement."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import sys
import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_agent_session_host import AgentModel
from tests.test_round4_plan_host import _open_test_session, _tool_call


class ProcessModel(AgentModel):
    def __init__(self):
        super().__init__()
        self.first_turn = None
        self.calls = {}

    async def stream(self, request):
        if request.session_id == self.root_id:
            async for event in super().stream(request):
                yield event
            return
        self.requests.append(request)
        self.child_requests.append(request)
        self.first_turn = self.first_turn or request.turn_id
        assert request.turn_id == self.first_turn, 'queued continuation started before process-loss boundary'
        count = self.calls.get(request.turn_id, 0)
        self.calls[request.turn_id] = count+1
        if count == 0:
            for event in _tool_call('exec_process', random_id('call'), {'command':'printf x >> effect-count.txt'}):
                yield event
        else:
            self.first_child_started.set()
            await asyncio.Future()


async def run(workspace: Path, dsn: str, ready: Path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('PULSARA_HOME', str(workspace/'home'))
    model=ProcessModel()
    core, root = await _open_test_session(tmp_path=workspace,model=model,
        postgres_dsn=dsn,monkeypatch=monkeypatch)
    model.root_id=root.session_id
    await root.set_subagent_capacity(1)
    await root.run_turn(PromptContent.text('Perform the first effect'),command_id=random_id('command'),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
    await asyncio.wait_for(model.first_child_started.wait(),15)
    ids=await root._io.run(root.repository.list_root_agent_session_ids,root_session_id=root.session_id,
        deadline_monotonic=core._canonical_deadline())
    model.agent_id=ids[0]
    await root.run_turn(PromptContent.text('Queue the independent continuation'),command_id=random_id('command'),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
    works=await root._io.run(root.repository.list_agent_works,root_session_id=root.session_id,
        agent_id=model.agent_id,limit=10,deadline_monotonic=core._canonical_deadline())
    assert [work['status'] for work in works]==['running','queued']
    assert (workspace/'effect-count.txt').read_text()=='x'
    ready.write_text(json.dumps({'root_session_id':root.session_id,'agent_id':model.agent_id,
        'first_work_id':works[0]['work_id'],'queued_work_id':works[1]['work_id']}))
    # No finally/cleanup: the parent kills this process to exercise real loss of
    # VM, live Host objects and provider execution, rather than graceful close.
    await asyncio.Future()


if __name__=='__main__':
    asyncio.run(run(Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3])))
