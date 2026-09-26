from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, FormData
import pytest

from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.file_import import LocalImport
from pulsara_agent.exclusive_publish import PlatformExclusiveDirectoryPublisher
from tests.test_local_web_http_surface import _model_server_dependencies


@asynccontextmanager
async def surface(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("PULSARA_HOME", str(root / "home"))
    static = root / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("test")
    bridge = LocalBrowserBridge(sessions=SimpleNamespace(), protocol_server=object())
    owner = SimpleNamespace(
        session_id="session", role="controller", generation=7, is_open=True
    )
    bridge._connections["owner"] = owner
    bridge._controller_by_session["session"] = "owner"
    bridge._connections["observer"] = SimpleNamespace(
        session_id="session", role="observer", generation=8, is_open=True
    )
    server = LocalHttpServer(
        sessions=SimpleNamespace(),
        bridge=bridge,
        static_root=static,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **_model_server_dependencies(),
    )
    await server.start()
    async with ClientSession(
        headers={"X-Pulsara-Connection-Generation": "7"}
    ) as client:
        try:
            yield server, client, bridge, root / "home" / "imports"
        finally:
            await server.aclose()


def file_headers(name):
    return {
        "Content-Type": "application/octet-stream",
        "X-Pulsara-Filename": json.dumps(name),
    }


def folder(parts):
    form = FormData(quote_fields=False)
    for name, body in parts:
        form.add_field("path", json.dumps(name))
        form.add_field(
            "file", body, filename="data", content_type="application/octet-stream"
        )
    return form


def test_file_and_directory_streaming_above_json_limit_and_readonly(
    tmp_path, monkeypatch
):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            for name, data in [
                ("report.pdf", b"%PDF-local"),
                ("test.docx", b"PK\0"),
                ("sheet.xlsx", b"PK\1"),
                ("empty.unknown", b""),
                ("large.txt", b"a" * ((8 << 20) + 31)),
                ('../safe "测试".md', b"text"),
            ]:
                response = await client.post(
                    server.origin + "/api/connections/owner/import-file",
                    data=data,
                    headers=file_headers(name),
                )
                assert response.status == 200, await response.text()
                value = await response.json()
                path = Path(value["path"])
                assert path.is_relative_to(imports) and path.read_bytes() == data
                assert path.stat().st_mode & 0o222 == 0
                assert value["bytes"] == len(data)
            files = [
                ("目录/a/same.pdf", b"pdf"),
                ("目录/b/same.pdf", b"other"),
                ("目录/picture.png", b"png"),
                ("目录/empty", b""),
                ("目录/large", b"x" * ((8 << 20) + 11)),
            ]
            response = await client.post(
                server.origin + "/api/connections/owner/import-directory",
                data=folder(files),
            )
            assert response.status == 200, await response.text()
            value = await response.json()
            path = Path(value["path"])
            assert value["file_count"] == len(files)
            for name, data in files:
                assert path.joinpath(*name.split("/")[1:]).read_bytes() == data
            for directory in [path.parent, path, path / "a", path / "b"]:
                assert directory.stat().st_mode & 0o777 == 0o500
            # Run under the normal local user, not root: no chmod is required
            # for reads, and ordinary writes/unlinks cannot mutate the copy.
            with pytest.raises(PermissionError):
                (path / "injected.txt").write_text("must fail")
            with pytest.raises(PermissionError):
                (path / "a" / "same.pdf").unlink()
            assert not list(imports.glob(".import-*"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "paths",
    [
        ["root/../escape"],
        ["/absolute/file"],
        ["root/a", "other/b"],
        ["root/a", "root/a"],
        ["root/a", "root/a/file"],
        ["root/a/file", "root/a"],
        ["root//a"],
        ["root/a\\b"],
        ["root/\x00a"],
        ["root/e\u0301", "root/é"],
    ],
)
def test_invalid_folder_never_publishes_partial_tree(tmp_path, monkeypatch, paths):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            response = await client.post(
                server.origin + "/api/connections/owner/import-directory",
                data=folder([(path, b"contents") for path in paths]),
            )
            assert response.status == 400, await response.text()
            assert not list(imports.iterdir())

    asyncio.run(run())


def test_controller_generation_origin_and_takeover_are_checked(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, bridge, imports):
            for owner, headers in [
                ("observer", {}),
                ("missing", {}),
                ("owner", {"X-Pulsara-Connection-Generation": "6"}),
                ("owner", {"Origin": "https://elsewhere.invalid"}),
                ("owner", {"Sec-Fetch-Site": "cross-site"}),
                ("owner", {"Host": "evil.invalid"}),
            ]:
                response = await client.post(
                    server.origin + f"/api/connections/{owner}/import-file",
                    data=b"no",
                    headers={**file_headers("a.pdf"), **headers},
                )
                assert response.status in {403, 404, 409, 421}
            started = asyncio.Event()
            proceed = asyncio.Event()

            async def upload():
                yield b"first"
                started.set()
                await proceed.wait()
                yield b"last"

            task = asyncio.create_task(
                client.post(
                    server.origin + "/api/connections/owner/import-file",
                    data=upload(),
                    headers=file_headers("a.pdf"),
                )
            )
            await started.wait()
            bridge._controller_by_session["session"] = "new-owner"
            proceed.set()
            response = await task
            assert response.status == 409
            assert not imports.exists() or not list(imports.iterdir())

    asyncio.run(run())


def test_failed_disk_and_rebinding_clean_unpublished_files(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            original = LocalImport.publish

            def full(self, name):
                raise OSError(errno.ENOSPC, "No space left")

            monkeypatch.setattr(LocalImport, "publish", full)
            response = await client.post(
                server.origin + "/api/connections/owner/import-file",
                data=b"data",
                headers=file_headers("a.pdf"),
            )
            assert response.status == 507
            assert not list(imports.iterdir())

            def rebound(self, name):
                leaf = self.root / self.stage / name
                leaf.unlink()
                leaf.symlink_to(tmp_path / "outside")
                return original(self, name)

            monkeypatch.setattr(LocalImport, "publish", rebound)
            response = await client.post(
                server.origin + "/api/connections/owner/import-file",
                data=b"data",
                headers=file_headers("a.pdf"),
            )
            assert response.status == 400
            assert not list(imports.iterdir())

    asyncio.run(run())


def test_cancelled_upload_removes_unpublished_tree(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            hold = asyncio.Event()

            async def upload():
                yield b"pending body"
                await hold.wait()

            request = asyncio.create_task(
                client.post(
                    server.origin + "/api/connections/owner/import-file",
                    data=upload(),
                    headers=file_headers("cancel.pdf"),
                )
            )
            async with asyncio.timeout(5):
                while not imports.exists() or not list(imports.glob(".import-*")):
                    await asyncio.sleep(0.01)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                while list(imports.iterdir()):
                    await asyncio.sleep(0.01)

    asyncio.run(run())


def test_import_root_symlink_is_not_followed(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            outside = tmp_path / "outside"
            outside.mkdir()
            imports.parent.mkdir()
            imports.symlink_to(outside, target_is_directory=True)
            response = await client.post(
                server.origin + "/api/connections/owner/import-file",
                data=b"no",
                headers=file_headers("a.pdf"),
            )
            assert response.status == 400
            assert not list(outside.iterdir())

    asyncio.run(run())


def test_publish_checks_tree_after_exclusive_rename(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            original = PlatformExclusiveDirectoryPublisher.publish

            def inject(self, parent_fd, source, destination):
                original(self, parent_fd, source, destination)
                os.chmod(imports / destination, 0o700)
                (imports / destination / "injected").symlink_to(tmp_path)

            monkeypatch.setattr(PlatformExclusiveDirectoryPublisher, "publish", inject)
            response = await client.post(
                server.origin + "/api/connections/owner/import-directory",
                data=folder([("root/nested/a.pdf", b"bytes")]),
            )
            assert response.status == 400, await response.text()
            assert not list(imports.iterdir())
            assert tmp_path.exists()

    asyncio.run(run())


def test_published_copy_survives_host_close_and_new_host(tmp_path, monkeypatch):
    async def run():
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            response = await client.post(
                server.origin + "/api/connections/owner/import-file",
                data=b"persistent copy",
                headers=file_headers("report.pdf"),
            )
            first = Path((await response.json())["path"])
        assert first.read_bytes() == b"persistent copy"
        async with surface(tmp_path, monkeypatch) as (server, client, _, imports):
            response = await client.post(
                server.origin + "/api/connections/owner/import-file",
                data=b"new copy",
                headers=file_headers("report.pdf"),
            )
            second = Path((await response.json())["path"])
            assert second != first
            assert first.read_bytes() == b"persistent copy"
            assert second.read_bytes() == b"new copy"
            assert not list(imports.glob(".import-*"))

    asyncio.run(run())
