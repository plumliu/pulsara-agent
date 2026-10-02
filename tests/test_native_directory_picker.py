from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.web_app import directory_picker as module
from pulsara_agent.web_app.directory_picker import NativeDirectoryPicker
from pulsara_agent.web_app.native_desktop import NativeDesktopUnavailable
from pulsara_agent.web_app.session_controller import LocalSessionController


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_selection_returns_original_directory_or_cancel(tmp_path, monkeypatch, cancelled, platform):
    selected = tmp_path / '空目录 "quoted" $(touch nope)\n '
    selected.mkdir()
    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(module, "linux_desktop_command", lambda name: "/usr/bin/zenity")
    monkeypatch.setenv("ZENITY_OK", "7")
    process = SimpleNamespace(
        returncode=3 if platform == "linux" and cancelled else 0,
        communicate=AsyncMock(return_value=(b"\n" if cancelled else f"{selected}/\n".encode(), b"")),
        wait=AsyncMock(),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(NativeDirectoryPicker().choose(str(selected)))
    assert result == (None if cancelled else str(selected.resolve()))
    if platform == "darwin":
        assert spawn.call_args.args[-1] == str(selected)
        assert str(selected) not in spawn.call_args.args[2]
    else:
        assert spawn.call_args.args == (
            "/usr/bin/zenity", "--file-selection", "--directory",
            "--title=选择 Pulsara 会话的工作目录", f"--filename={selected}/",
        )
        assert spawn.call_args.kwargs["env"]["ZENITY_CANCEL"] == "3"
        assert spawn.call_args.kwargs["env"]["ZENITY_ESC"] == "3"
        assert spawn.call_args.kwargs["env"]["ZENITY_OK"] == "0"
        assert module.os.environ["ZENITY_OK"] == "7"
    if not cancelled:
        controller = SimpleNamespace(memory_domain_id="test", trust_workspace_mcp_config=False)
        workspace = LocalSessionController._workspace_input_for_create(
            controller, workspace_kind="project", workspace_path=result,
        )
        assert Path(workspace.workspace_root) == selected.resolve()


def test_native_picker_failure_and_unsupported_platform(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "win32")
    with pytest.raises(NotImplementedError):
        asyncio.run(NativeDirectoryPicker().choose(None))
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b"", b"failure")), wait=AsyncMock(),
    )))
    with pytest.raises(OSError):
        asyncio.run(NativeDirectoryPicker().choose(None))


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_picker_serializes_panels_and_closes_pending_process(monkeypatch, platform):
    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setattr(module, "linux_desktop_command", lambda name: "/usr/bin/zenity")

    async def exercise():
        exit_event = asyncio.Event()
        process = SimpleNamespace(returncode=None)

        async def communicate():
            await exit_event.wait()
            return b"", b""

        def kill():
            process.returncode = -9
            exit_event.set()

        process.communicate = communicate
        process.kill = kill
        process.wait = AsyncMock()
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
        picker = NativeDirectoryPicker()
        first = asyncio.create_task(picker.choose(None))
        second = asyncio.create_task(picker.choose(None))
        await asyncio.sleep(0)
        assert spawn.await_count == 1
        await picker.aclose()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(outcome, OSError) for outcome in outcomes)
        assert spawn.await_count == 1
        assert process.returncode == -9

    asyncio.run(exercise())


@pytest.mark.parametrize("status,output", [(1, b""), (255, b""), (-9, b""), (0, b""), (0, b"relative\n")])
def test_linux_startup_errors_and_invalid_output_are_not_user_cancellation(monkeypatch, status, output):
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "linux_desktop_command", lambda name: "/usr/bin/zenity")
    process = SimpleNamespace(
        returncode=status, communicate=AsyncMock(return_value=(output, b"GTK failed")), wait=AsyncMock(),
    )
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(OSError):
        asyncio.run(NativeDirectoryPicker().choose(None))
    process.wait.assert_awaited_once()


def test_linux_picker_reports_missing_desktop_component_before_spawning(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    def unavailable(name):
        raise NativeDesktopUnavailable("缺少桌面组件，请安装 zenity 后重试。")
    monkeypatch.setattr(module, "linux_desktop_command", unavailable)
    spawn = AsyncMock()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(NativeDesktopUnavailable, match="zenity"):
        asyncio.run(NativeDirectoryPicker().choose(None))
    spawn.assert_not_awaited()
