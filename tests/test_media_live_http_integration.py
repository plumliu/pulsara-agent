"""A real Host, PostgreSQL, protocol gateway and source-bound HTTP preview."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import av
from aiohttp import ClientSession
from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.tool_permission import default_permission_policy
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.test_desktop_http_surface import _model_server_dependencies
from tests.test_round4_plan_host import _DetachedDraftModel, _text, _tool_call

pytestmark = pytest.mark.postgres


def make_video(path: Path) -> None:
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=25)
        stream.width, stream.height, stream.pix_fmt = 64, 36, 'yuv420p'
        for index in range(25):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 36), (index * 9, 20, 30)))
            frame.pts = index
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def test_actual_read_live_snapshot_http_preview_reconnect_and_idle(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    import pulsara_agent.conversation_kernel.host as kernel_host

    async def exercise():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        media = tmp_path / 'clip.mp4'
        make_video(media)
        runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            input_modalities=('text', 'image'), wire_api='openai_chat_completions')

        class ReadingModel(_DetachedDraftModel):
            def __init__(self):
                super().__init__()
                self._preparer = DirectKernelModelPort(model_runtime=runtime)
                self.read_finished = asyncio.Event()
                self.finish = asyncio.Event()

            async def stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    events = _tool_call('read_video_frames', random_id('call'), {
                        'target': {'kind': 'PATH', 'path': str(media)},
                        'from': 0, 'to': 1, 'mode': 'detail'})
                else:
                    self.read_finished.set()
                    await self.finish.wait()
                    events = _text('Finished inspecting the delivered evidence.')
                for event in events:
                    yield event

        model = ReadingModel()
        monkeypatch.setattr(kernel_host, 'DirectKernelModelPort', lambda **_: model)
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, 'load_configs', lambda *_args, **_kwargs: ())
        core = KernelHostCore.production(model_runtime=runtime)
        sessions = LocalSessionController(core=core, permission_policy=default_permission_policy(), active_skill_names=frozenset())
        handle = await sessions.create_session(workspace_kind='project', workspace_path=str(tmp_path), model_call_binding=test_model_binding(runtime))
        session = handle.session
        running = None
        with TemporaryDirectory(prefix='pulsara-live-', dir='/tmp') as socket_directory:
            protocol = TerminalKernelProtocolServer(socket_path=Path(socket_directory) / 'kernel.sock', session_provider=sessions.session_by_host_id)
            await protocol.start()
            bridge = RendererBridge(sessions=sessions, protocol_server=protocol)
            dependencies = _model_server_dependencies()
            dependencies.update(settings=runtime.settings, model_runtime=runtime, catalog=runtime.catalog)
            (tmp_path / 'index.html').write_text('Live media integration')
            server = LocalHttpServer(sessions=sessions, bridge=bridge, static_root=tmp_path,
                requested_port=0, is_ready=lambda: True, is_draining=lambda: False, **dependencies)
            await server.start()
            try:
                async with ClientSession(headers={'Origin': server.origin, 'Sec-Fetch-Site': 'same-origin'}) as client:
                    browser = random_id('transport')

                    async def connect():
                        response = await client.post(f'{server.origin}/api/sessions/{session.session_id}/connections', json={'browser_instance_id': browser})
                        payload = await response.json()
                        assert response.status == 201, payload
                        return payload

                    first = await connect()
                    assert not first['live_control_snapshot']['snapshot'].get('media_ranges')
                    connection = first['connection_id']

                    async def post(suffix, **body):
                        response = await client.post(f'{server.origin}/api/connections/{connection}/{suffix}', json=body)
                        payload = await response.json()
                        assert response.status == 200, payload
                        return payload

                    running = asyncio.create_task(session.run_turn(PromptContent.text('Read the video and hold the current work.'),
                        command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
                    waiting = asyncio.create_task(model.read_finished.wait())
                    done, _ = await asyncio.wait({running, waiting}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
                    if running in done:
                        await running
                    assert waiting in done, 'Read did not reach the next model call'
                    snapshot = (await post('live-control-snapshot'))['live_control_snapshot']['snapshot']
                    ranges = snapshot['media_ranges']
                    assert len(ranges) == 1
                    assert ranges[0]['canonical_path'] == str(media.resolve())
                    assert ranges[0]['file_mtime_ns'] == str(media.stat().st_mtime_ns)
                    assert ranges[0]['to_seconds'] == 1
                    assert not snapshot.get('current_todos', [{}])[0].get('ordered_items')

                    observed = await post('observe', after_event_sequence=0,
                        live_owner_epoch=int(first['live_hello']['live_owner_epoch']), after_live_revision=int(first['live_hello'].get('live_revision', '0')),
                        live_control_owner_epoch=int(snapshot['owner_epoch']), after_live_control_revision=int(snapshot.get('live_revision', '0')),
                        maximum_events=128, maximum_bytes=2 << 20, wait_ms=0)
                    starts = [event for event in observed['observation'].get('live', []) if event['event_type'] == 'CURRENT_WORK_SNAPSHOT_UPDATED']
                    assert any(event['payload']['current_work_snapshot_updated'].get('media_read_attempt_id') == ranges[0]['attempt_id'] for event in starts)
                    preview = await post('file-preview', path=str(media))
                    assert preview['path'] == ranges[0]['canonical_path'] and preview['modified_ns'] == ranges[0]['file_mtime_ns']
                    source_url = server.origin + preview['content_url']
                    response = await client.get(source_url, headers={'Range': 'bytes=0-15'})
                    assert response.status == 206 and await response.read() == media.read_bytes()[:16]
                    await client.delete(f'{server.origin}/api/connections/{connection}')
                    assert (await client.get(source_url)).status == 409
                    second = await connect()
                    connection = second['connection_id']
                    assert second['live_control_snapshot']['snapshot']['media_ranges'] == ranges
                    assert second['live_control_snapshot']['snapshot']['host_session_id'] == session.host_session_id
                    model.finish.set()
                    await running
                    idle = (await post('live-control-snapshot'))['live_control_snapshot']['snapshot']
                    assert not idle.get('media_ranges')
                    third = await connect()
                    connection = third['connection_id']
                    assert not third['live_control_snapshot']['snapshot'].get('media_ranges')
            finally:
                model.finish.set()
                if running is not None:
                    await asyncio.gather(running, return_exceptions=True)
                await server.aclose()
                await bridge.aclose()
                await protocol.close()
                await sessions.aclose()
                await core.shutdown()

    asyncio.run(exercise())
