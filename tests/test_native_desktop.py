from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.web_app import native_desktop as module
from pulsara_agent.web_app import session_controller
from pulsara_agent.web_app.file_preview import FilePreviewError, open_preview
from pulsara_agent.web_app.file_preview_http import FilePreviewHttp


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("reveal_file", [False, True])
def test_open_and_reveal_dispatch_without_shell(tmp_path, monkeypatch, platform, reveal_file):
    path = tmp_path.resolve() / 'file $(touch nope) "中".txt'
    path.write_text("hello")
    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/xdg-open")
    process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(module.open_desktop_path(path, reveal_file=reveal_file))
    expected = (
        ("/usr/bin/open", *(["-R"] if reveal_file else []), str(path))
        if platform == "darwin" else
        ("/usr/bin/xdg-open", str(path.parent if reveal_file else path))
    )
    assert spawn.call_args.args == expected
    assert not (tmp_path / "nope").exists()


@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_linux_dependency_and_desktop_detection(monkeypatch, display):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(module.NativeDesktopUnavailable, match="桌面会话"):
        module.linux_desktop_command("zenity")
    monkeypatch.setenv(display, "desktop")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    for name, package in [("zenity", "zenity"), ("xdg-open", "xdg-utils")]:
        with pytest.raises(module.NativeDesktopUnavailable, match=package):
            module.linux_desktop_command(name)
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert module.linux_desktop_command("zenity") == "/usr/bin/zenity"


def test_opener_failure_and_cancellation_reap_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")

    async def exercise():
        failed = SimpleNamespace(returncode=4, wait=AsyncMock(return_value=4))
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=failed))
        with pytest.raises(module.NativeDesktopUnavailable, match="默认应用"):
            await module.open_desktop_path(tmp_path)
        exited = asyncio.Event()
        waiting = asyncio.Event()
        process = SimpleNamespace(returncode=None)
        async def wait():
            waiting.set()
            await exited.wait()
            return process.returncode
        def kill():
            process.returncode = -9
            exited.set()
        process.wait, process.kill = wait, kill
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        task = asyncio.create_task(module.open_desktop_path(tmp_path))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode == -9
    asyncio.run(exercise())


def test_capability_root_uses_shared_opener_and_effective_home(tmp_path, monkeypatch):
    home = tmp_path / "pulsara home"
    monkeypatch.setenv("PULSARA_HOME", str(home))
    opener = AsyncMock()
    monkeypatch.setattr(session_controller, "open_desktop_path", opener)
    result = asyncio.run(session_controller.LocalSessionController.open_capability_root(object(), "pulsara"))
    assert result == {"opened": True, "path": str(home)}
    assert home.is_dir()
    opener.assert_awaited_once_with(home)


def test_preview_action_keeps_validated_path_handoff_and_public_errors(tmp_path, monkeypatch):
    from pulsara_agent.web_app import file_preview_http
    path = tmp_path.resolve() / "note.txt"
    path.write_text("hello")
    item = open_preview("connection", path)
    http = FilePreviewHttp(None, lambda: "")
    monkeypatch.setattr(http, "body", AsyncMock(return_value={"action": "reveal"}))
    monkeypatch.setattr(http, "current", AsyncMock(return_value=item))
    opener = AsyncMock()
    monkeypatch.setattr(file_preview_http, "open_desktop_path", opener)
    async def exercise():
        request = object()
        assert (await http.action(request)).status == 200
        opener.assert_awaited_once_with(path, reveal_file=True)
        opener.reset_mock()
        opener.side_effect = module.NativeDesktopUnavailable("缺少桌面组件，请安装 xdg-utils 后重试。")
        with pytest.raises(FilePreviewError, match="xdg-utils") as caught:
            await http.action(request)
        assert caught.value.code == "FILE_UNREADABLE"
        opener.reset_mock()
        path.write_text("changed after preview")
        with pytest.raises(FilePreviewError, match="文件已发生变化"):
            await http.action(request)
        opener.assert_not_awaited()
    try:
        asyncio.run(exercise())
    finally:
        item.close()
