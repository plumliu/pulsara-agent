"""Sidebar reads use the actual Agent Host without owning its lifecycle."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from time import monotonic

import pytest

from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.protocol_client import ProtocolTransportClosed
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.tool_permission import preset_to_policy
from tests.test_agent_session_host import AgentModel, eventually
from tests.test_round4_plan_host import _open_test_session, _tool_call, _text

pytestmark = pytest.mark.postgres
pytest_plugins = ['tests.test_agent_requests_persistence']


def test_reader_attaches_existing_agent_without_resuming_and_recovers_cold(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    class ContextAgentModel(AgentModel):
        async def stream(self, request):
            if request.session_id != self.root_id:
                async for event in super().stream(request):
                    yield event
                return
            self.requests.append(request)
            index = self.root_turns.get(request.turn_id, 0)
            self.root_turns[request.turn_id] = index + 1
            if index:
                events = _text('ROOT_FINISHED')
            elif self.agent_id is None:
                events = _tool_call('run_agent', random_id('call'), {
                    'instruction': 'CHILD_FIRST', 'context': {'mode': 'last_n', 'turns': 1}})
            else:
                events = _tool_call('run_agent', random_id('call'), {
                    'instruction': 'CHILD_SECOND', 'target': {'kind': 'AGENT', 'agent_id': self.agent_id}})
            for event in events:
                yield event

    async def scenario():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        model = ContextAgentModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        controller = LocalSessionController(core=core, permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
            active_skill_names=frozenset())
        socket_dir = tempfile.TemporaryDirectory(prefix='ps-', dir='/tmp')
        server = TerminalKernelProtocolServer(socket_path=Path(socket_dir.name) / 'p.sock', session_provider=controller.session_by_host_id)
        await server.start()
        bridge = RendererBridge(sessions=controller, protocol_server=server)
        try:
            await root.run_turn(PromptContent.text('Create one child'), command_id=random_id('command'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(model.first_child_started.wait(), 15)
            ids = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=monotonic()+10)
            agent_id = ids[0]
            child = core._live_agent_session(agent_id)
            assert child is not None and child.host_session_id not in controller._by_host
            async def forbidden(*_args, **_kwargs):
                raise AssertionError('read must not resume an Agent')
            monkeypatch.setattr(controller, 'resume_session', forbidden)
            browser = random_id('transport')
            authorized = controller.authorize_agent_reader
            recheck_started = asyncio.Event()
            checks = 0
            async def pause_recheck(*args):
                nonlocal checks
                checks += 1
                if checks == 2:
                    recheck_started.set()
                    await asyncio.Event().wait()
                return await authorized(*args)
            monkeypatch.setattr(controller, 'authorize_agent_reader', pause_recheck)
            opening = asyncio.create_task(bridge.open_agent_reader(root.session_id, agent_id, browser_instance_id=browser))
            await asyncio.wait_for(recheck_started.wait(), 5)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
            async def detached():
                return not child.live_bus._observers
            await eventually(detached)
            assert not bridge._connections
            monkeypatch.setattr(controller, 'authorize_agent_reader', authorized)
            first = await bridge.open_agent_reader(root.session_id, agent_id, browser_instance_id=browser)
            assert first['view'] == 'live' and first['role'] == 'observer'
            assert first['host_session_id'] == child.host_session_id
            old = bridge._connections[first['connection_id']]
            second = await bridge.open_agent_reader(root.session_id, agent_id, browser_instance_id=browser)
            assert second['connection_id'] != first['connection_id'] and not old.is_open
            assert len(bridge._connections) == 1
            with pytest.raises(KeyError):
                await bridge.open_agent_reader(random_id('session'), agent_id, browser_instance_id=browser)
            model.first_child_release.set()
            async def idle():
                return child.active_root_turn_id() is None
            await eventually(idle)
            await core.close_session(child.host_session_id)
            with pytest.raises(ProtocolTransportClosed):
                await bridge.snapshot(second['connection_id'])
            state = await controller.sidebar_agent(root.session_id, agent_id)
            assert state['agent']['host_session_id'] is None
            before = await root._io.run(root.repository.read_agent_runtime_state,
                session_id=agent_id, deadline_monotonic=monotonic()+10)
            cold = await bridge.open_agent_reader(root.session_id, agent_id, browser_instance_id=browser)
            assert cold['view'] == 'history'
            assert core._live_agent_session(agent_id) is None
            page = await controller.queue_read(agent_id, {}, root_session_id=root.session_id)
            assert page == {'items': [], 'next_cursor': None}
            after = await root._io.run(root.repository.read_agent_runtime_state,
                session_id=agent_id, deadline_monotonic=monotonic()+10)
            assert before['idle_since'] == after['idle_since']
            assert before['writer_generation'] == after['writer_generation']
            model.agent_id = agent_id
            await root.run_turn(PromptContent.text('Continue the child'), command_id=random_id('command'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            async def reloaded():
                return core._live_agent_session(agent_id) is not None
            await eventually(reloaded)
            third = await bridge.open_agent_reader(root.session_id, agent_id, browser_instance_id=browser)
            assert third['view'] == 'live'
            assert third['host_session_id'] != first['host_session_id']
            async def second_completed():
                works = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=agent_id, limit=10, deadline_monotonic=monotonic()+10)
                return len(works) == 2 and all(work['status'] == 'completed' for work in works)
            await eventually(second_completed)
            reader = await controller.authorize_agent_reader(root.session_id, agent_id)
            with reader._connection(monotonic()+10) as connection:
                turns = connection.execute("""SELECT t.id,t.initial_entry_id FROM pulsara_v3.turns t
                    WHERE t.session_id=%s ORDER BY t.accepted_at,t.id""", (agent_id,)).fetchall()
                origins = connection.execute("""SELECT input_origin FROM pulsara_v3.transcript_entries
                    WHERE session_id=%s AND turn_id=%s AND entry_kind='USER_MESSAGE' ORDER BY entry_sequence""",
                    (agent_id, turns[0]['id'])).fetchall()
            assert [row['input_origin'] for row in origins] == ['AGENT_MATERIAL', 'AGENT_INSTRUCTION']
            facts = await asyncio.to_thread(reader.browser_history_context, session_id=agent_id,
                memory_domain_id=controller.memory_domain_id,
                entry_ids=[turns[0]['initial_entry_id'], turns[1]['initial_entry_id']],
                deadline_monotonic=monotonic()+10)
            groups = {group['owner_id']: group for group in facts['groups']}
            assert groups[turns[0]['id']]['next_owner_id'] == turns[1]['id']
            assert groups[turns[1]['id']]['previous_owner_id'] == turns[0]['id']
            assert all('root_owner_id' not in item for item in facts['entry_contexts'])
        finally:
            model.first_child_release.set()
            await bridge.aclose()
            await server.close()
            socket_dir.cleanup()
            await core.shutdown()
    asyncio.run(scenario())


def test_queue_pages_and_exact_consumption_keep_agent_input_identity(source):
    from tests.test_agent_requests_persistence import deadline, now, request, start

    repository, root, admit = source
    first = request()
    second = request(agent_id=first.agent_id, create=False)
    admit(first, second)
    reader = CanonicalProtocolReader(repository.connection_provider)
    with reader._connection(deadline()) as connection:
        domain = reader._session(connection, root.guard.session_id)['id']
        domain = connection.execute('SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s', (domain,)).fetchone()['memory_domain_id']
    common = dict(session_id=first.agent_id, root_session_id=root.guard.session_id,
                  memory_domain_id=domain, deadline_monotonic=deadline())
    page, cursor = reader.queue_page(after_sequence=0, limit=1, **common)
    assert len(page) == 1 and page[0].queue_item_id == first.work_id
    assert page[0].source_session_id == root.guard.session_id
    assert cursor == 1 and not page[0].command_id
    following, cursor = reader.queue_page(after_sequence=cursor, limit=1, **common)
    assert len(following) == 1 and following[0].queue_item_id == second.work_id and cursor is None
    start(repository, first)
    exact = reader.queue_item(queue_item_id=first.work_id, **common)
    assert exact.status == 'CONSUMED' and exact.consumed_entry_id
    following, cursor = reader.queue_page(after_sequence=1, limit=1, **common)
    assert len(following) == 1 and following[0].queue_item_id == second.work_id
    with pytest.raises(KeyError):
        reader.queue_item(queue_item_id=first.work_id, **(common | {'root_session_id': random_id('session')}))
    waiting = request(depends_on=(second.work_id,))
    admit(waiting)
    waiting_common = common | {'session_id': waiting.agent_id}
    pending = reader.queue_item(queue_item_id=waiting.work_id, **waiting_common)
    assert list(pending.waiting_on) == [second.work_id]
    repository.cancel_agent_requests(root.guard, agent_id=waiting.agent_id, work_id=waiting.work_id,
        occurred_at=now(), actor_id='test', deadline_monotonic=deadline())
    terminal = reader.queue_item(queue_item_id=waiting.work_id, **waiting_common)
    assert terminal.status == 'CANCELLED' and list(terminal.waiting_on) == []
