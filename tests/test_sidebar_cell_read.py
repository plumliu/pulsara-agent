"""Canonical source reads outlive Cells; live graphs close only on retirement."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.code_mode.contracts import CodeModeError
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id


def test_cell_retiring_during_graph_read_closes_the_live_view():
    class Tools:
        def __init__(self):
            self.live = True
            self.attempt = random_id('attempt')

        def capture_code_cell(self, _cell_id):
            if not self.live:
                raise CodeModeError('unavailable', 'Cell is unavailable')
            return SimpleNamespace(source_exec_attempt_id=self.attempt)

        def query_code_cells(self, **_kwargs):
            return {'cells': []}

        async def read_code_cell_graph(self, **_kwargs):
            self.live = False
            return {'items': [], 'participation_coverage': 'complete'}

    async def scenario():
        controller = object.__new__(LocalSessionController)
        tools = Tools()
        host = SimpleNamespace(_tools=tools)
        controller._sidebar_root = AsyncMock()
        controller._sidebar_root_host = AsyncMock(return_value=host)
        result = await controller.sidebar_cell_graph(random_id('session'), random_id('host'), random_id('cell'), {})
        assert result == {'status': 'CLOSED', 'message': '实时工作流视图已关闭'}
    asyncio.run(scenario())


def test_cell_sidecar_failure_is_not_confused_with_retirement():
    class Tools:
        def capture_code_cell(self, _cell_id):
            raise CodeModeError('unavailable', 'sidecar is unavailable')

        def query_code_cells(self, **_kwargs):
            raise CodeModeError('unavailable', 'sidecar is unavailable')

    with pytest.raises(CodeModeError, match='sidecar'):
        LocalSessionController._sidebar_cell_capture(SimpleNamespace(_tools=Tools()), random_id('cell'))


def test_canonical_source_read_does_not_borrow_a_live_host_or_cell():
    async def scenario():
        root, attempt = random_id('session'), random_id('attempt')
        reads = []
        def source(**kwargs):
            reads.append(kwargs)
            return 'const actual = "Hook后冻结源码";'
        controller = object.__new__(LocalSessionController)
        controller._sidebar_root = AsyncMock()
        controller._sidebar_root_host = AsyncMock(side_effect=AssertionError('source read cannot use a live Host'))
        controller.core = SimpleNamespace(_ensure_resources=AsyncMock(return_value=SimpleNamespace(read_code_mode_source=source)),
                                         _canonical_deadline=lambda: 100, _sessions={})
        page = await controller.cell_source(root, attempt, {'offset_chars': 6, 'max_chars': 5})
        assert page == {'source_exec_attempt_id': attempt, 'live_cell': None, 'source': {'status': 'AVAILABLE',
            'text': 'actua', 'offset_chars': 6, 'returned_chars': 5,
            'total_chars': len('const actual = "Hook后冻结源码";'), 'has_more': True, 'next_offset_chars': 11}}
        assert reads == [{'session_id': root, 'attempt_id': attempt, 'deadline_monotonic': 100}]
        assert controller._sidebar_root.await_count == 2
        controller._sidebar_root_host.assert_not_awaited()
    asyncio.run(scenario())


def test_missing_or_other_root_source_attempt_is_not_readable():
    async def scenario():
        controller = object.__new__(LocalSessionController)
        controller._sidebar_root = AsyncMock()
        controller.core = SimpleNamespace(_ensure_resources=AsyncMock(return_value=SimpleNamespace(read_code_mode_source=lambda **_: None)),
                                         _canonical_deadline=lambda: 100, _sessions={})
        with pytest.raises(KeyError):
            await controller.cell_source(random_id('session'), random_id('attempt'), {})
        assert controller._sidebar_root.await_count == 2
    asyncio.run(scenario())


def test_source_read_rechecks_root_access_before_returning_content():
    async def scenario():
        controller = object.__new__(LocalSessionController)
        controller._sidebar_root = AsyncMock(side_effect=[None, KeyError('deleted ROOT')])
        controller.core = SimpleNamespace(_ensure_resources=AsyncMock(return_value=SimpleNamespace(read_code_mode_source=lambda **_: 'frozen')),
                                         _canonical_deadline=lambda: 100, _sessions={})
        with pytest.raises(KeyError, match='deleted ROOT'):
            await controller.cell_source(random_id('session'), random_id('attempt'), {})
    asyncio.run(scenario())


def test_source_live_graph_link_uses_only_exact_attempt_and_current_root():
    root, other_root, attempt, other_attempt = (random_id(kind) for kind in ('session', 'session', 'attempt', 'attempt'))
    cell, host_id = random_id('cell'), random_id('host')
    def host(session_id, source_attempt, *, closing=False):
        return SimpleNamespace(session_id=session_id, root_session_id=None,
            host_session_id=host_id, _closing=closing, _closed=False,
            _tools=SimpleNamespace(query_code_cells=lambda **_: {'cells': [{'target': {'cell_id': cell}}], 'next_cursor': None},
                capture_code_cell=lambda _: SimpleNamespace(source_exec_attempt_id=source_attempt)))
    controller = object.__new__(LocalSessionController)
    controller.core = SimpleNamespace(_sessions={'other': host(other_root, attempt),
        'retiring': host(root, attempt, closing=True), 'different': host(root, other_attempt)})
    assert controller._live_cell_for_source(root, attempt) is None
    controller.core._sessions['live'] = host(root, attempt)
    assert controller._live_cell_for_source(root, attempt) == {'host_session_id': host_id, 'cell_id': cell}


def test_graph_sidecar_failure_keeps_a_refresh_failure():
    async def scenario():
        controller = object.__new__(LocalSessionController)
        controller._sidebar_root = AsyncMock()
        controller._sidebar_root_host = AsyncMock(side_effect=CodeModeError('unavailable', 'sidecar refresh failed'))
        with pytest.raises(CodeModeError, match='refresh failed'):
            await controller.sidebar_cell_graph(random_id('session'), random_id('host'), random_id('cell'), {})
    asyncio.run(scenario())


def test_cancelled_http_open_closes_already_published_observer():
    from pulsara_agent.desktop_app.http_server import LocalHttpServer

    async def scenario():
        surface = object.__new__(LocalHttpServer)
        connection_id = random_id('transport')
        surface.bridge = SimpleNamespace(
            open_agent_reader=AsyncMock(return_value={'view': 'live', 'connection_id': connection_id}),
            disconnect=AsyncMock(),
        )
        surface._json_body = AsyncMock(return_value={'browser_instance_id': random_id('transport')})
        request = SimpleNamespace(transport=None,
            match_info={'session_id': random_id('session'), 'agent_id': random_id('session')})
        with pytest.raises(asyncio.CancelledError):
            await surface._sidebar_reader(request)
        surface.bridge.disconnect.assert_awaited_once_with(connection_id)
    asyncio.run(scenario())


def test_cancel_at_reader_gate_exit_releases_published_connection(monkeypatch):
    from pulsara_agent.desktop_app import renderer_bridge as bridge_module
    from pulsara_agent.desktop_app.renderer_bridge import RendererBridge

    async def scenario():
        root_id, agent_id = random_id('session'), random_id('session')
        host_id, connection_id = random_id('host'), random_id('transport')
        live = SimpleNamespace(root_session_id=root_id, host_session_id=host_id,
                               workspace=SimpleNamespace(workspace_root='/tmp'))
        sessions = SimpleNamespace(authorize_agent_reader=AsyncMock(),
            core=SimpleNamespace(_live_agent_session=lambda _agent: live))
        bridge = RendererBridge(sessions=sessions, protocol_server=object())
        entered, release = asyncio.Event(), asyncio.Event()

        class PausingLock:
            def __init__(self):
                self.paused = False

            async def __aenter__(self):
                if bridge._connections and not self.paused:
                    self.paused = True
                    entered.set()
                    await release.wait()
                return self

            async def __aexit__(self, *_args):
                pass

        connection = SimpleNamespace(connection_id=connection_id, session_id=agent_id,
                                     aclose=AsyncMock())
        bridge._lock = PausingLock()
        monkeypatch.setattr(bridge_module.BrowserRuntimeConnection, 'open', AsyncMock(return_value=connection))
        monkeypatch.setattr(bridge, '_connection_payload', lambda _: {'connection_id': connection_id})
        task = asyncio.create_task(bridge.open_agent_reader(root_id, agent_id,
                                                           browser_instance_id=random_id('transport')))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        connection.aclose.assert_awaited_once()
        assert not bridge._connections
        assert not bridge._agent_reader_targets
        assert not bridge._session_gate_users
    asyncio.run(scenario())


def test_scoped_history_http_checks_root_before_and_after_read():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulsara_agent.desktop_app.http_server import LocalHttpServer

    async def scenario():
        root, agent = random_id('session'), random_id('session')
        surface = object.__new__(LocalHttpServer)
        async def authorize(actual_root, actual_agent):
            if (actual_root, actual_agent) != (root, agent):
                raise KeyError(actual_agent)
        surface.sessions = SimpleNamespace(authorize_agent_reader=AsyncMock(side_effect=authorize),
            history_window=AsyncMock(return_value={'entries': []}))
        surface.bridge = SimpleNamespace(require_chat_controller=AsyncMock(side_effect=AssertionError('reader cannot require controller')))
        app = web.Application(middlewares=[surface._errors, surface._chat_write_admission])
        app.router.add_post('/api/sessions/{session_id}/agents/{agent_id}/history/window', surface._sidebar_history)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(f'/api/sessions/{root}/agents/{agent}/history/window', json={})
            assert response.status == 200 and await response.json() == {'entries': []}
            assert surface.sessions.authorize_agent_reader.await_count == 2
            response = await client.post(f'/api/sessions/{random_id("session")}/agents/{agent}/history/window', json={})
            assert response.status == 404
            surface.sessions.history_window.assert_awaited_once()
        surface.bridge.require_chat_controller.assert_not_awaited()
    asyncio.run(scenario())


def test_canonical_cell_source_http_does_not_require_an_active_controller():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulsara_agent.desktop_app.http_server import LocalHttpServer

    async def scenario():
        root, attempt = random_id('session'), random_id('attempt')
        surface = object.__new__(LocalHttpServer)
        payload = {'source_exec_attempt_id': attempt,
                   'source': {'status': 'AVAILABLE', 'text': 'frozen'}}
        surface.sessions = SimpleNamespace(cell_source=AsyncMock(return_value=payload))
        surface.bridge = SimpleNamespace(require_chat_controller=AsyncMock(
            side_effect=AssertionError('source reading cannot require a controller')))
        app = web.Application(middlewares=[surface._errors, surface._chat_write_admission])
        app.router.add_post('/api/sessions/{session_id}/cell-sources/{source_exec_attempt_id}', surface._cell_source)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(f'/api/sessions/{root}/cell-sources/{attempt}', json={'offset_chars': 0})
            assert response.status == 200 and await response.json() == payload
        surface.sessions.cell_source.assert_awaited_once_with(root, attempt, {'offset_chars': 0})
        surface.bridge.require_chat_controller.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize('blocked', ['second_attachment', 'snapshot', 'live_control'])
def test_cancelled_observer_open_closes_every_acquired_attachment(monkeypatch, blocked):
    from pulsara_agent.desktop_app.protocol_client import BrowserRuntimeConnection, TerminalProtocolClient

    async def scenario():
        entered = asyncio.Event()
        clients = []

        class Client:
            def __init__(self, role):
                self.role = role
                self.closed = False

            async def require(self, field, _message, _response):
                if (field == 'snapshot' and blocked == 'snapshot') or (field == 'live_control_snapshot' and blocked == 'live_control'):
                    entered.set()
                    await asyncio.Event().wait()
                return SimpleNamespace()

            async def aclose(self):
                self.closed = True

        async def connect(cls, **kwargs):
            if len(clients) == 1 and blocked == 'second_attachment':
                entered.set()
                await asyncio.Event().wait()
            client = Client(kwargs['role'])
            clients.append(client)
            return client, SimpleNamespace()

        monkeypatch.setattr(TerminalProtocolClient, 'connect', classmethod(connect))
        opening = asyncio.create_task(BrowserRuntimeConnection.open(server=SimpleNamespace(),
            session_id=random_id('session'), host_session_id=random_id('host'), generation=1, role='observer'))
        await asyncio.wait_for(entered.wait(), 5)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert clients and all(client.closed for client in clients)
    asyncio.run(scenario())
