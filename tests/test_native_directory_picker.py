from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.web_app import directory_picker as module
from pulsara_agent.web_app.directory_picker import NativeDirectoryPicker
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.workspace_identity import HostWorkspaceInput


@pytest.mark.parametrize("cancelled", [False, True])
def test_native_selection_returns_original_directory_or_cancel(tmp_path, monkeypatch, cancelled):
    selected = tmp_path / '空目录 "quoted" '
    selected.mkdir()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"\n" if cancelled else f"{selected}/\n".encode(), b"")),
        wait=AsyncMock(),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(NativeDirectoryPicker().choose(str(selected)))
    assert result == (None if cancelled else str(selected.resolve()))
    assert spawn.call_args.args[-1] == str(selected)
    assert str(selected) not in spawn.call_args.args[2]
    if not cancelled:
        controller = SimpleNamespace(workspace_input=HostWorkspaceInput(
            workspace_kind="project", workspace_root=tmp_path,
            memory_domain_id="test", trust_workspace_mcp_config=False,
        ))
        workspace = LocalSessionController._workspace_input_for_create(
            controller, workspace_kind="project", workspace_path=result,
        )
        assert Path(workspace.workspace_root) == selected.resolve()


def test_native_picker_failure_and_unsupported_platform(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(NotImplementedError):
        asyncio.run(NativeDirectoryPicker().choose(None))
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b"", b"failure")), wait=AsyncMock(),
    )))
    with pytest.raises(OSError):
        asyncio.run(NativeDirectoryPicker().choose(None))


def test_native_picker_serializes_panels_and_closes_pending_process(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")

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
