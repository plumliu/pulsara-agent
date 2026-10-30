"""Desktop checks Cell lifetime after asynchronous, non-consuming reads."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.code_mode.contracts import CodeModeError
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id


@pytest.mark.parametrize('graph', [False, True])
def test_cell_retiring_during_read_never_delivers_cached_content(graph):
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

        async def read_code_cell_source_page(self, **_kwargs):
            self.live = False
            return {'source': {'status': 'AVAILABLE', 'text': 'frozen'}}

        async def read_code_cell_graph(self, **_kwargs):
            self.live = False
            return {'items': [], 'participation_coverage': 'complete'}

    async def scenario():
        controller = object.__new__(LocalSessionController)
        tools = Tools()
        host = SimpleNamespace(_tools=tools)
        async def root_host(*_args):
            return host
        controller._sidebar_root_host = root_host
        with pytest.raises(KeyError):
            await controller.sidebar_cell(random_id('session'), random_id('host'), random_id('cell'), {}, graph=graph)
    asyncio.run(scenario())


def test_cell_sidecar_failure_is_not_confused_with_retirement():
    class Tools:
        def capture_code_cell(self, _cell_id):
            raise CodeModeError('unavailable', 'sidecar is unavailable')

        def query_code_cells(self, **_kwargs):
            raise CodeModeError('unavailable', 'sidecar is unavailable')

    with pytest.raises(CodeModeError, match='sidecar'):
        LocalSessionController._sidebar_cell_capture(SimpleNamespace(_tools=Tools()), random_id('cell'))


def test_desktop_and_model_reads_have_distinct_retirement_boundaries():
    from pulsara_agent.code_mode import CodeModeHost
    from pulsara_agent.code_mode.targets import cell_target
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from tests.test_query_cells_contract import _start, _terminal

    async def scenario():
        host = CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def read_source(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
            await release.wait()
            return 'actual frozen source'
        port._code_mode_read_source = read_source
        controller = object.__new__(LocalSessionController)
        bound_host = SimpleNamespace(_tools=port)
        async def root_host(*_args):
            return bound_host
        controller._sidebar_root_host = root_host
        try:
            cell_id = await _start(host, "console.log('unread');")
            await _terminal(host, cell_id)
            output = await host.observe(cell_id, 0)
            context = SimpleNamespace(tool_call_id=random_id('call'), session_id=random_id('session'))
            model = asyncio.create_task(port._query_cells({'target': cell_target(cell_id)}, context))
            desktop = asyncio.create_task(controller.sidebar_cell(context.session_id, random_id('host'), cell_id, {}, graph=False))
            await asyncio.wait_for(started.wait(), 5)
            await host._cells[cell_id].vm_released.wait()
            await host.acknowledge(output)
            assert cell_id not in host._cells
            release.set()
            assert (await model)['source']['text'] == 'actual frozen source'
            with pytest.raises(KeyError):
                await desktop
        finally:
            release.set()
            await host.aclose()
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
