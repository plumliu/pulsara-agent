from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession
import pytest

from pulsara_agent.web_app.file_preview import (
    FilePreviewError,
    PAGE_BYTES,
    TEXT_BYTES,
    local_path,
    open_preview,
)
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from tests.test_local_web_http_surface import _model_server_dependencies


def bridge_for(root):
    async def summary(session_id, **kwargs):
        return SimpleNamespace(workspace_root=str(root / session_id))

    sessions = SimpleNamespace(
        core=SimpleNamespace(read_resumable_session=summary),
        workspace_input=SimpleNamespace(memory_domain_id="device"),
    )
    bridge = LocalBrowserBridge(sessions=sessions, protocol_server=object())
    for name in ("one", "two"):
        (root / name).mkdir(exist_ok=True)
        bridge._connections[name] = SimpleNamespace(
            connection_id=name, session_id=name, is_open=True, aclose=AsyncMock()
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
        fd, mime = item.resource("index.html", images=False)
        try:
            assert os.pread(fd, 100, 0) == b"<h1>original</h1>"
        finally:
            os.close(fd)
        for name in (
            "../data.json",
            ".private.json",
            "linked.json",
            "x\\data.json",
            "data.py",
        ):
            with pytest.raises(FilePreviewError):
                item.resource(name, images=False)
        with pytest.raises(FilePreviewError):
            item.action_path("open")
        with pytest.raises(FilePreviewError, match="文件已发生变化"):
            item.action_path("reveal")
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
                ).status == 409
                late = await client.delete(
                    server.origin + "/api/connections/one/file-preview",
                    json={"read_token": one["read_token"]},
                    headers=headers,
                )
                assert late.status == 409
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


def test_open_replacement_and_disconnect_prevent_late_install(tmp_path, monkeypatch):
    async def run():
        root = tmp_path.resolve()
        bridge = bridge_for(root)
        for name in ("slow.txt", "fast.txt"):
            (root / "one" / name).write_text(name)
        from pulsara_agent.web_app import browser_bridge

        real_open = browser_bridge.open_preview
        entered, release = threading.Event(), threading.Event()
        opened = []

        def slow(connection, path):
            if path.name == "slow.txt":
                entered.set()
                release.wait(5)
            result = real_open(connection, path)
            opened.append(result)
            return result

        monkeypatch.setattr(browser_bridge, "open_preview", slow)
        first = asyncio.create_task(bridge.open_file_preview("one", "slow.txt", None))
        await asyncio.to_thread(entered.wait, 5)
        second = await bridge.open_file_preview("one", "fast.txt", None)
        release.set()
        with pytest.raises(FilePreviewError):
            await first
        assert bridge.file_previews.slots["one"].token == second["read_token"]
        assert next(i for i in opened if i.path.name == "slow.txt").closed
        await bridge.disconnect("one")
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
    from pulsara_agent.web_app import file_preview_http

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
