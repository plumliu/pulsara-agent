from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.web_app.visualization_preview import VisualizationPreviews
from pulsara_agent.conversation_kernel.visualization_screenshot import VisualizationScreenshotOwner


class Bridge:
    def __init__(self, content=b'<h1>Chart</h1>'):
        self.content = content
        self.calls = []
        self.denied = False

    async def read_content(self, connection, body):
        self.calls.append((connection, body))
        if self.denied:
            return {'error': {'code': 'OWNER_MISSING'}}
        offset = body['offset_bytes']
        part = self.content[offset:offset + min(7, body['limit_bytes'])]
        return {'content': {'digest': 'sha256:' + sha256(self.content).hexdigest(),
            'complete_size': len(self.content), 'offset_bytes': offset,
            'content': base64.b64encode(part).decode(), 'complete': offset + len(part) == len(self.content)}}


def target(bridge):
    return {'entry_id': 'entry:a', 'ordinal': 0, 'digest': 'sha256:' + sha256(bridge.content).hexdigest(), 'size': len(bridge.content)}


def test_thumbnail_uses_exact_owner_chunks_and_checks_owner_after_render():
    async def run():
        bridge = Bridge()
        owner = VisualizationPreviews(bridge)
        owner.screenshots.render = AsyncMock(return_value=b'PNG')
        assert await owner.read('connection', target(bridge)) == {'image': 'data:image/png;base64,UE5H'}
        assert owner.screenshots.render.call_args.args == (bridge.content,)
        assert owner.screenshots.render.call_args.kwargs['thumbnail'] is True
        assert bridge.calls[-1][1]['limit_bytes'] == 1
        assert all(call[1]['visualization_ordinal'] == 0 for call in bridge.calls)
        async def revoked(*args, **kwargs):
            bridge.denied = True
            return b'PNG'
        owner.screenshots.render = revoked
        with pytest.raises(ValueError, match='owner changed'):
            await owner.read('connection', target(bridge))
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mutation', [dict(ordinal=-1), dict(ordinal=True), dict(size=0), dict(digest='sha256:bad'), dict(size=999), dict(entry_id='')])
def test_invalid_reference_never_starts_browser(mutation):
    async def run():
        bridge = Bridge(); owner = VisualizationPreviews(bridge)
        owner.screenshots.render = AsyncMock()
        with pytest.raises(ValueError):
            await owner.read('connection', target(bridge) | mutation)
        owner.screenshots.render.assert_not_called()
        await owner.aclose()
    asyncio.run(run())


def test_thumbnail_browser_slot_serializes_and_does_not_reject_more_work():
    async def run():
        bridge = Bridge(); owner = VisualizationPreviews(bridge)
        active = 0; peak = 0; completed = 0
        async def screenshot(*args, **kwargs):
            nonlocal active, peak, completed
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.002)
            active -= 1; completed += 1
            return b'PNG'
        owner.screenshots.render = screenshot
        await asyncio.gather(*(owner.read('connection', target(bridge)) for _ in range(100)))
        assert peak == 1 and completed == 100
        await owner.aclose()
    asyncio.run(run())


def test_real_thumbnail_keeps_aspect_and_owner_cancellation_reaps_process():
    async def run():
        from io import BytesIO
        from PIL import Image
        owner = VisualizationScreenshotOwner()
        try:
            result = await owner.render(b'<main data-pulsara-visualization-root style="width:400px;height:100px;background:orange">Test</main>', deadline_monotonic=monotonic()+20, thumbnail=True)
            image = Image.open(BytesIO(result))
            assert image.size == (320, 80)
            pending = asyncio.create_task(owner.render(b'<script>while(true){}</script>', deadline_monotonic=monotonic()+20, thumbnail=True))
            while not owner._processes:
                await asyncio.sleep(.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not owner._processes
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_disconnected_queued_thumbnail_does_not_start_a_browser():
    async def run():
        bridge = Bridge(); owner = VisualizationPreviews(bridge)
        owner.screenshots.render = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await owner.read('connection', target(bridge), cancelled=lambda: True)
        assert not bridge.calls
        owner.screenshots.render.assert_not_called()
        await owner.aclose()
    asyncio.run(run())


def test_thumbnail_http_origin_boundary_and_canonical_response(tmp_path):
    from aiohttp import ClientSession
    from pulsara_agent.web_app.http_server import LocalHttpServer
    from tests.test_local_web_http_surface import _Sessions, _model_server_dependencies

    async def run():
        (tmp_path / 'index.html').write_text('Pulsara')
        bridge = Bridge()
        server = LocalHttpServer(sessions=_Sessions(), bridge=bridge, static_root=tmp_path,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            **_model_server_dependencies())
        renderer = AsyncMock(return_value=b'PNG')
        server._visualization_previews.screenshots.render = renderer
        await server.start()
        try:
            async with ClientSession() as client:
                url = server.origin + '/api/connections/test/visualization-thumbnail'
                async with client.post(url, json=target(bridge), headers={'Origin':'https://other.invalid'}) as response:
                    assert response.status == 403
                renderer.assert_not_awaited()
                async with client.post(url, json=target(bridge), headers={'Origin':server.origin}) as response:
                    assert response.status == 200
                    assert (await response.json())['image'] == 'data:image/png;base64,UE5H'
                bridge.denied = True
                async with client.post(url, json=target(bridge), headers={'Origin':server.origin}) as response:
                    assert response.status == 400
                assert renderer.await_count == 1
        finally:
            await server.aclose()
    asyncio.run(run())
