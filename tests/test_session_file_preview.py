from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession
import pytest

from pulsara_agent.desktop_app.file_preview import (
    FilePreviewError,
    PAGE_BYTES,
    TEXT_BYTES,
    local_path,
    open_preview,
)
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.identity import derive_id
from tests.test_desktop_http_surface import _model_server_dependencies


def bridge_for(root):
    async def summary(session_id, **kwargs):
        return SimpleNamespace(workspace_root=str(root / session_id))

    sessions = SimpleNamespace(
        core=SimpleNamespace(read_resumable_session=summary),
        memory_domain_id="device",
        host_writer_unavailable=lambda _host: False,
    )
    bridge = RendererBridge(sessions=sessions, protocol_server=object())
    for name in ("one", "two"):
        (root / name).mkdir(exist_ok=True)
        bridge._connections[name] = SimpleNamespace(
            connection_id=name, session_id=name,
            host_session_id=derive_id("host", "file-preview-test", name),
            is_open=True, aclose=AsyncMock()
        )
    return bridge


def test_path_resolution_utf8_pagination_and_changed_file(tmp_path):
    root = tmp_path.resolve()
    source = ("中文🙂\n" * TEXT_BYTES).encode()
    path = root / "中文 ?#.txt"
    path.write_bytes(source)
    assert local_path("%E4%B8%AD%E6%96%87%20%3F%23.txt#L3", root) == path
    assert local_path(path.as_uri(), root) == path
    for bad in ("file://remote/a", "javascript:alert(1)", "%00", "%zz", "a\\b"):
        with pytest.raises(FilePreviewError):
            local_path(bad, root)
    item = open_preview("one", path)
    try:
        cursor, parts = 0, []
        while True:
            page = item.page(cursor, "text")
            assert len(json.dumps(page).encode()) <= PAGE_BYTES
            parts.append(page["text"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert "".join(parts).encode() == source
        path.write_text("changed")
        with pytest.raises(FilePreviewError, match="文件已发生变化"):
            item.page(0, "text")
    finally:
        item.close()


def test_csv_multiline_bom_page_retry_and_record_bound(tmp_path):
    path = tmp_path.resolve() / "records.csv"
    path.write_text(
        '\ufeffname,body\n甲,"第一行\n第二行"\n' + "x,y\n" * 150000, encoding="utf-8"
    )
    item = open_preview("one", path)
    try:
        first = item.page(0, "table")
        assert first["rows"][:2] == [["name", "body"], ["甲", "第一行\n第二行"]]
        assert item.page(0, "table") is first
        rows = len(first["rows"])
        cursor = first["next_cursor"]
        while cursor is not None:
            page = item.page(cursor, "table")
            assert (
                len(
                    json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()
                )
                <= PAGE_BYTES
            )
            rows += len(page["rows"])
            cursor = page["next_cursor"]
        assert rows == 150002
        assert item.page(0, "table")["rows"][:2] == first["rows"][:2]
    finally:
        item.close()
    for record in (
        "," * PAGE_BYTES + "\n",
        '"' + ("\n" * PAGE_BYTES) + '"\n',
        "x" * (PAGE_BYTES + 1),
    ):
        path.write_text(record)
        item = open_preview("one", path)
        try:
            with pytest.raises(FilePreviewError) as caught:
                item.page(0, "table")
            assert caught.value.code == "FILE_PREVIEW_UNSUPPORTED"
            assert os.pread(item.fd, 1, 0) == record[:1].encode()
        finally:
            item.close()


def test_descriptor_binding_resources_and_no_symlink_follow(tmp_path):
    root = tmp_path.resolve()
    (root / "index.html").write_text("<h1>original</h1>")
    (root / "data.json").write_text('{"safe":true}')
    (root / ".private.json").write_text("secret")
    (root / "linked.json").symlink_to(root / ".private.json")
    item = open_preview("one", root / "index.html")
    try:
        assert item.page(0, "text")["text"] == "<h1>original</h1>"
        (root / "index.html").rename(root / "old.html")
        (root / "index.html").write_text("replacement")
        with pytest.raises(FilePreviewError) as changed:
            item.resource("index.html", images=False)
        assert changed.value.code == "FILE_CHANGED"
        item.close()
        item = open_preview("one", root / "index.html")
        for name in (
            "../data.json",
            "linked.json",
            "x\\data.json",
            "data.py",
        ):
            with pytest.raises(FilePreviewError):
                item.resource(name, images=False)
        with pytest.raises(FilePreviewError):
            item.action_path("open")
        assert item.action_path("reveal") == root / "index.html"
        fd, mime = item.resource("data.json", images=False)
        try:
            assert mime == "application/json"
        finally:
            os.close(fd)
    finally:
        item.close()
    link = open_preview("one", root / "linked.json")
    try:
        assert link.fd is None and link.notice
    finally:
        link.close()


def test_http_preview_contract(tmp_path):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        (root / "index.html").write_text("test")
        for name in ("one", "two"):
            (root / name / "same.txt").write_text(name)
        (root / "one" / "index.html").write_text(
            '<h1>local</h1><script src="./main.js"></script>'
        )
        (root / "one" / "main.js").write_text("window.ok=true")
        server = LocalHttpServer(
            sessions=bridge.sessions,
            bridge=bridge,
            static_root=root,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                headers = {"Origin": server.origin, "Sec-Fetch-Site": "same-origin"}

                async def post(connection, suffix="", **body):
                    return await client.post(
                        server.origin
                        + f"/api/connections/{connection}/file-preview"
                        + suffix,
                        json=body,
                        headers=headers,
                    )

                rejected = await client.post(
                    server.origin + "/api/connections/one/file-preview",
                    json={"path": "same.txt"},
                )
                assert rejected.status == 403
                one = await (await post("one", path="same.txt")).json()
                two = await (await post("two", path="same.txt")).json()
                assert (
                    await (await client.get(server.origin + one["content_url"])).text()
                    == "one"
                )
                assert (
                    await (await client.get(server.origin + two["content_url"])).text()
                    == "two"
                )
                assert (
                    await post("two", "/page", read_token=one["read_token"])
                ).status == 409
                ranges = await asyncio.gather(
                    *(
                        client.get(
                            server.origin + one["content_url"],
                            headers={"Range": f"bytes={i}-{i}"},
                        )
                        for i in range(3)
                    )
                )
                assert [await r.text() for r in ranges] == ["o", "n", "e"]
                assert all(r.status == 206 for r in ranges)
                assert (
                    await client.get(
                        server.origin + one["content_url"],
                        headers={"Range": "bytes=100-200"},
                    )
                ).status == 416
                html = await (await post("one", path="index.html")).json()
                assert "/resources/index.html" in html["document_url"]
                assert (
                    await client.get(server.origin + one["content_url"])
                ).status == 200
                late = await client.delete(
                    server.origin + "/api/connections/one/file-preview",
                    json={"read_token": one["read_token"]},
                    headers=headers,
                )
                assert late.status == 200
                doc = await client.get(
                    server.origin + html["document_url"],
                    headers={"Sec-Fetch-Dest": "iframe"},
                )
                assert (
                    doc.status == 200
                    and "sandbox allow-scripts"
                    in doc.headers["Content-Security-Policy"]
                )
                assert doc.headers["Cache-Control"] == "no-store"
                assert "pulsara-preview-error" in await doc.text()
                direct = await client.get(
                    server.origin + html["document_url"],
                    headers={"Sec-Fetch-Dest": "document"},
                )
                assert direct.status == 403
                resource = await client.get(
                    server.origin
                    + html["document_url"].replace("index.html", "main.js"),
                    headers={"Origin": "null"},
                )
                assert resource.headers["Access-Control-Allow-Origin"] == "null"
                assert await resource.text() == "window.ok=true"
                raw = await client.get(
                    server.origin + html["content_url"] + "?download=1"
                )
                assert await raw.text() == (root / "one" / "index.html").read_text()
                assert "attachment" in raw.headers["Content-Disposition"]
                (root / "one" / "index.html").write_text("changed after opening")
                closed = await client.delete(
                    server.origin + "/api/connections/one/file-preview",
                    json={"read_token": html["read_token"]},
                    headers=headers,
                )
                assert closed.status == 200
                assert "one" not in bridge.file_previews.slots
                await bridge.disconnect("one")
                assert (
                    await client.get(server.origin + html["document_url"])
                ).status == 409
        finally:
            await bridge.aclose()
            await server.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(("filename", "kind", "mime"), [("clip.MP4", "video", "video/mp4"), ("clip.wav", "audio", "audio/wav"), ("clip.mp3", "audio", "audio/mpeg"), ("clip.m4a", "audio", "audio/mp4"), ("clip.flac", "audio", "audio/flac"), ("clip.opus", "audio", "audio/ogg")])
def test_media_content_and_html_media_use_bound_range_reads(tmp_path, filename, kind, mime):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        payload = bytes(range(256)) * 8192
        (root / "index.html").write_text("Pulsara")
        path = root / "one" / filename
        path.write_bytes(payload)
        (root / "one" / "index.html").write_text(f'<{kind} src="{filename}"></{kind}>')
        server = LocalHttpServer(
            sessions=bridge.sessions, bridge=bridge, static_root=root,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                headers = {"Origin": server.origin, "Sec-Fetch-Site": "same-origin"}

                async def open_file(name):
                    response = await client.post(
                        server.origin + "/api/connections/one/file-preview",
                        json={"path": name}, headers=headers,
                    )
                    assert response.status == 200
                    return await response.json()

                video = await open_file(filename)
                assert video["kind"] == kind
                assert video["modified_ns"] == str(path.stat().st_mtime_ns)
                assert video["document_url"] is None
                assert video["images_url"] is None
                url = server.origin + video["content_url"]
                response = await client.get(url, headers={"Range": "bytes=16-47"})
                assert response.status == 206
                assert response.headers["Content-Type"] == mime
                assert response.headers["Content-Range"] == f"bytes 16-47/{len(payload)}"
                assert "Content-Disposition" not in response.headers
                assert await response.read() == payload[16:48]
                tail = await client.get(url, headers={"Range": "bytes=-64"})
                assert tail.status == 206 and await tail.read() == payload[-64:]
                outside = await client.get(url, headers={"Range": f"bytes={len(payload)}-"})
                assert outside.status == 416
                head = await client.head(url)
                assert head.status == 200 and head.headers["Content-Length"] == str(len(payload))

                html = await open_file("index.html")
                document = await client.get(server.origin + html["document_url"], headers={"Sec-Fetch-Dest": "iframe"})
                assert f"media-src {server.origin}{html['resources_url']}" in document.headers["Content-Security-Policy"]
                embedded = await client.get(server.origin + html["document_url"].replace("index.html", filename))
                assert embedded.status == 200
                assert await embedded.read() == payload
                path.write_bytes(b"replacement")
                changed = await client.get(url, headers={"Range": "bytes=0-3"})
                assert changed.status == 409
        finally:
            await bridge.aclose()
            await server.aclose()

    asyncio.run(run())


def test_independent_open_and_disconnect_release_all_instances(tmp_path, monkeypatch):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        for name in ("slow.txt", "fast.txt"):
            (root / "one" / name).write_text(name)
        from pulsara_agent.desktop_app import renderer_bridge

        real_open = renderer_bridge.open_preview
        entered, release = threading.Event(), threading.Event()
        opened = []

        def slow(connection, path, reading_root=None, **kwargs):
            if path.name == "slow.txt":
                entered.set()
                release.wait(5)
            result = real_open(connection, path, reading_root, **kwargs)
            opened.append(result)
            return result

        monkeypatch.setattr(renderer_bridge, "open_preview", slow)
        first = asyncio.create_task(bridge.open_file_preview("one", "slow.txt", None))
        await asyncio.to_thread(entered.wait, 5)
        second = await bridge.open_file_preview("one", "fast.txt", None)
        release.set()
        first_result = await first
        assert set(bridge.file_previews.slots["one"]) == {
            first_result["read_token"],
            second["read_token"],
        }
        assert not next(i for i in opened if i.path.name == "slow.txt").closed
        entered.clear()
        release.clear()
        late = asyncio.create_task(bridge.open_file_preview("one", "slow.txt", None))
        await asyncio.to_thread(entered.wait, 5)
        await bridge.disconnect("one")
        release.set()
        with pytest.raises(FilePreviewError) as disconnected:
            await late
        assert disconnected.value.code == "PREVIEW_EXPIRED"
        assert all(item.closed for item in opened)
        assert not bridge.file_previews.slots

    asyncio.run(run())


def test_failed_csv_does_not_resume_after_the_bad_record(tmp_path):
    path = tmp_path.resolve() / "invalid.csv"
    for bad in ("x" * 140000, "," * PAGE_BYTES):
        path.write_text("before,one\n" + bad + ",oversized\nafter,two\n")
        item = open_preview("one", path)
        try:
            with pytest.raises(FilePreviewError) as initial:
                item.page(0, "table")
            assert item.page(0, "text")["text"].startswith("before,one")
            with pytest.raises(FilePreviewError) as retry:
                item.page(0, "table")
            assert retry.value.code == initial.value.code
        finally:
            item.close()


def test_directory_root_and_ancestor_symlink_notice(tmp_path):
    item = open_preview("one", local_path("/", tmp_path.resolve()))
    try:
        assert item.kind == "directory" and item.fd is None
    finally:
        item.close()
    root = tmp_path.resolve()
    (root / "real").mkdir()
    (root / "real" / "data.txt").write_text("not read")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    item = open_preview("one", root / "link" / "data.txt")
    try:
        assert item.notice and item.fd is None and item.root_fd is None
    finally:
        item.close()


def test_revoked_inline_stream_stops_but_explicit_download_finishes(
    tmp_path, monkeypatch
):
    from pulsara_agent.desktop_app import file_preview_http

    async def run(download):
        path = tmp_path.resolve() / "stream.txt"
        path.write_bytes(b"abcdef")
        item = open_preview("one", path)
        chunks = []
        transport_closed = []

        class Response:
            def __init__(self, **kwargs):
                pass

            async def prepare(self, request):
                pass

            async def write(self, data):
                chunks.append(data)
                item.close()

            async def write_eof(self):
                pass

        monkeypatch.setattr(file_preview_http.web, "StreamResponse", Response)
        monkeypatch.setattr(file_preview_http, "PAGE_BYTES", 1)
        request = SimpleNamespace(
            headers={},
            method="GET",
            transport=SimpleNamespace(close=lambda: transport_closed.append(True)),
        )
        fd = os.dup(item.fd)
        await file_preview_http.FilePreviewHttp(None, lambda: "").stream(
            request, fd, "text/plain", {}, item=None if download else item
        )
        assert b"".join(chunks) == (b"abcdef" if download else b"a")
        assert bool(transport_closed) is not download
        with pytest.raises(OSError):
            os.fstat(fd)

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_browsing_scope_survives_child_navigation_entry_deletion_and_reconnect(
    tmp_path,
):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        site = root / "one" / "field_release"
        (site / "learner" / "role_manuals").mkdir(parents=True)
        (site / "index.html").write_text('<a href="learner/index.html">Learn</a>')
        (site / "learner" / "index.html").write_text(
            '<a href="role_manuals/ACCESS-07.md">Manual</a>'
        )
        manual = site / "learner" / "role_manuals" / "ACCESS-07.md"
        manual.write_text("# Manual\n[Home](../../index.html)")
        entry = await bridge.open_file_preview("one", str(site / "index.html"), None)
        learner = await bridge.open_file_preview(
            "one", "learner/index.html", entry["read_token"]
        )
        document = await bridge.open_file_preview(
            "one", "role_manuals/ACCESS-07.md", learner["read_token"]
        )
        assert {
            entry["reading_root"],
            learner["reading_root"],
            document["reading_root"],
        } == {str(site)}
        assert document["kind"] == "markdown"
        assert learner["document_url"].endswith("/resources/learner/index.html")
        for candidate in ("../../../outside.txt", str(root / "one" / "other.txt")):
            with pytest.raises(FilePreviewError) as rejected:
                await bridge.open_file_preview("one", candidate, document["read_token"])
            assert rejected.value.code == "RESOURCE_OUTSIDE_ROOT"
        assert (
            (await bridge.require_file_preview("one", document["read_token"]))
            .page(0, "text")["text"]
            .startswith("# Manual")
        )
        bridge.file_previews.revoke("one", learner["read_token"])
        assert await bridge.require_file_preview("one", entry["read_token"])
        (site / "index.html").unlink()
        bridge.file_previews.revoke("one")
        restored = await bridge.open_file_preview("one", str(manual), None, str(site))
        assert restored["reading_root"] == str(site)
        assert (
            (await bridge.require_file_preview("one", restored["read_token"]))
            .page(0, "text")["text"]
            .startswith("# Manual")
        )
        await bridge.disconnect("one")
        assert not bridge.file_previews.slots

    asyncio.run(run())


def test_unlink_and_atomic_replacement_never_reuse_old_page_or_cursor(tmp_path):
    path = tmp_path.resolve() / "data.csv"
    path.write_text("old,value\n")
    item = open_preview("one", path)
    try:
        item.page(0, "table")
        replacement = path.with_suffix(".replacement")
        replacement.write_text("new,value\n")
        replacement.replace(path)
        with pytest.raises(FilePreviewError) as changed:
            item.page(0, "table")
        assert changed.value.code == "FILE_CHANGED"
        path.unlink()
        with pytest.raises(FilePreviewError) as missing:
            item.page(0, "text")
        assert missing.value.code == "FILE_NOT_FOUND"
    finally:
        item.close()


@pytest.mark.parametrize(("media_name", "expected_mime"), [("clip.mp4", "video/mp4"), ("speech.wav", "audio/wav")])
def test_html_snapshot_binds_project_media_without_reopening_layout(tmp_path, media_name, expected_mime):
    async def run():
        from pulsara_agent.desktop_app.file_preview_http import bind_html_snapshot
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        project = root / 'one'
        directory = project / '.pulsara' / 'viz'
        directory.mkdir(parents=True)
        path = directory / 'gallery.html'
        image = project / 'image.png'
        image.write_bytes(b'first image')
        video = project / media_name
        video.write_bytes(b'video bytes')
        (project / 'secret.json').write_text('{}')
        (project / 'linked.png').symlink_to(image)
        html = '<h1>frozen layout</h1><img src="../../image.png"><button data-src="../../clip.mp4">Open</button>'
        bound = await bind_html_snapshot(bridge, 'http://127.0.0.1:8080', 'one', str(path), html)
        item = await bridge.require_file_preview('one', bound['read_token'])
        try:
            assert item.reading_root == project and item.fd is None
            assert '<h1>frozen layout</h1>' in bound['html']
            assert f'<base href="{bound["resource_prefix"]}.pulsara/viz/gallery.html">' in bound['html']
            assert f'src="{bound["resource_prefix"]}image.png"' in bound['html']
            assert 'data-src="../../clip.mp4"' in bound['html']
            path.write_text('different layout')
            path.unlink()
            for body in [b'first image', b'new image']:
                image.write_bytes(body)
                fd, mime = item.resource('image.png', images=False)
                try:
                    assert mime == 'image/png' and os.read(fd, 50) == body
                finally:
                    os.close(fd)
            fd, actual_mime = item.resource(media_name, images=False)
            os.close(fd)
            assert actual_mime == expected_mime
            video.write_bytes(b'replaced video')
            with pytest.raises(FilePreviewError) as changed:
                item.resource(media_name, images=False)
            assert changed.value.code == 'FILE_CHANGED'
            for name in ['secret.json', 'gallery.html', '../image.png', 'linked.png']:
                with pytest.raises(FilePreviewError):
                    item.resource(name, images=False)
        finally:
            bridge.file_previews.revoke('one', bound['read_token'])
            assert item.closed and item.root_fd is None
            await bridge.aclose()
    asyncio.run(run())


def test_html_snapshot_http_media_ranges_and_real_thumbnail(tmp_path):
    async def run():
        from io import BytesIO
        from time import monotonic
        from PIL import Image
        from tests.test_html_local_resources import png
        from pulsara_agent.conversation_kernel.visualization_screenshot import VisualizationScreenshotOwner
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        (root / 'index.html').write_text('test')
        (root / 'one' / 'image.png').write_bytes(png())
        video = root / 'one' / 'clip.mp4'
        video.write_bytes(b'0123456789')
        server = LocalHttpServer(sessions=bridge.sessions, bridge=bridge, static_root=root,
            requested_port=0, is_ready=lambda:True, is_draining=lambda:False, **_model_server_dependencies())
        screenshots = VisualizationScreenshotOwner()
        await server.start()
        try:
            async with ClientSession() as client:
                endpoint = server.origin+'/api/connections/one/file-preview'
                headers = {'Origin':server.origin, 'Sec-Fetch-Site':'same-origin'}
                html = '<style>body{margin:0}main,img{width:20px;height:20px}</style><main pulsara-viz-root><img src="image.png"></main>'
                response = await client.post(endpoint+'/snapshot',headers=headers,json={'path':'gallery.html','html':html})
                assert response.status == 200
                snapshot = await response.json()
                screenshot = await screenshots.render(snapshot['html'].encode(), deadline_monotonic=monotonic()+30, resource_prefix=snapshot['resource_prefix'])
                image = Image.open(BytesIO(screenshot)).convert('RGB')
                assert image.getpixel((10,10)) == (255,165,0)
                url = snapshot['resource_prefix']+'clip.mp4'
                ranged = await client.get(url,headers={'Range':'bytes=2-5'})
                assert ranged.status == 206 and await ranged.read() == b'2345'
                head = await client.head(url)
                assert head.status == 200 and head.headers['Content-Length'] == '10'
                video.write_bytes(b'replaced')
                assert (await client.get(url,headers={'Range':'bytes=0-1'})).status == 409
                closed = await client.delete(endpoint,headers=headers,json={'read_token':snapshot['read_token']})
                assert closed.status == 200
                expired = await client.get(snapshot['resource_prefix']+'image.png')
                assert expired.status == 409
                assert (await expired.json())['error']['code'] == 'PREVIEW_EXPIRED'
        finally:
            await screenshots.aclose()
            await bridge.aclose()
            await server.aclose()
    asyncio.run(run())


def test_media_activation_touches_only_validated_sidebar_source(tmp_path):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        touch = AsyncMock()
        bridge.sessions.core.touch_asr_cache_source = touch
        path = root / 'one' / 'speech.wav'
        path.write_bytes(b'RIFF audio source')
        (root / 'index.html').write_text('Pulsara')
        server = LocalHttpServer(sessions=bridge.sessions, bridge=bridge, static_root=root,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            **_model_server_dependencies())
        await server.start()
        try:
            async with ClientSession() as client:
                endpoint = server.origin + '/api/connections/one/file-preview'
                headers = {'Origin': server.origin, 'Sec-Fetch-Site': 'same-origin'}
                response = await client.post(endpoint, headers=headers, json={'path': 'speech.wav'})
                preview = await response.json()
                assert response.status == 200
                touch.assert_not_called()
                await client.get(server.origin + preview['content_url'], headers={'Range': 'bytes=0-3'})
                touch.assert_not_called()
                activated = await client.post(endpoint + '/action', headers=headers,
                    json={'read_token': preview['read_token'], 'action': 'activate'})
                assert activated.status == 200
                touch.assert_awaited_once_with(str(path), path.stat().st_mtime_ns, path.stat().st_size)
                path.write_bytes(b'replaced')
                changed = await client.post(endpoint + '/action', headers=headers,
                    json={'read_token': preview['read_token'], 'action': 'activate'})
                assert changed.status == 409
                touch.assert_awaited_once()
        finally:
            await bridge.aclose()
            await server.aclose()
    asyncio.run(run())
