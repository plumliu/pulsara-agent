"""The screenshot owner retains actual launch and SDK cleanup ownership."""

import asyncio
import json
import os
from pathlib import Path
import sys
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel import visualization_screenshot as screenshot


@pytest.mark.parametrize("close_request", ["close", "cancel"])
def test_close_during_actual_spawn_keeps_the_original_launch_owned(monkeypatch, close_request):
    async def run():
        spawned = asyncio.get_running_loop().create_future()
        release = asyncio.Event()
        original = asyncio.create_subprocess_exec

        async def held_spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            spawned.set_result(process)
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", held_spawn)
        owner = screenshot.VisualizationScreenshotOwner()
        rendering = asyncio.create_task(owner.render(b"<main>Hello</main>", deadline_monotonic=monotonic() + 10))
        process = await spawned
        if close_request == "close":
            closing = asyncio.create_task(owner.aclose())
        else:
            rendering.cancel()
            closing = rendering
        await asyncio.sleep(0)  # Run the close admission, not a timing guess.
        assert not owner.local_children_stopped and not closing.done()
        release.set()
        if close_request == "close":
            await closing
        with pytest.raises(asyncio.CancelledError):
            await rendering
        await owner.aclose()
        assert owner.local_children_stopped and process.returncode is not None

    asyncio.run(run())


@pytest.mark.parametrize("worker", ["no-handshake", "early-eof"])
def test_startup_stays_inside_the_original_render_deadline_without_sdk_children(monkeypatch, worker):
    async def run():
        original = asyncio.create_subprocess_exec
        code = "import sys; sys.stdin.buffer.read()" if worker == "no-handshake" else "pass"

        async def spawn(_executable, *_args, **kwargs):
            return await original(sys.executable, "-c", code, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        owner = screenshot.VisualizationScreenshotOwner()
        with pytest.raises(screenshot.VisualizationScreenshotError, match="PREVIEW_DEADLINE_EXPIRED" if worker == "no-handshake" else "PREVIEW_WORKER_START_FAILED"):
            await owner.render(b"<main>Hello</main>", deadline_monotonic=monotonic() + 0.1)
        await owner.aclose()
        assert owner.local_children_stopped and not owner._processes

    asyncio.run(run())


async def browser_barrier(monkeypatch, path: Path, *, fail_close: bool = False):
    """CDP supplies the actual owned browser PIDs; no process discovery is used."""
    ready = asyncio.get_running_loop().create_future()

    async def accept(reader, writer):
        info = json.loads(await reader.readline())
        ready.set_result((info, writer))

    server = await asyncio.start_unix_server(accept, path=path)
    original = asyncio.create_subprocess_exec
    code = r'''
import json, os, runpy, socket, sys
from playwright.async_api import BrowserType, Browser
original_launch = BrowserType.launch
async def launch(self, *args, **kwargs):
    browser = await original_launch(self, *args, **kwargs)
    session = await browser.new_browser_cdp_session()
    info = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
    await session.detach()
    pids = [item["id"] for item in info]
    group = os.getpgid(next(item["id"] for item in info if item["type"] == "browser"))
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(sys.argv[1])
        channel.sendall((json.dumps({"pids":pids,"group":group}) + "\n").encode())
        channel.recv(1)
    return browser
BrowserType.launch = launch
if sys.argv[2] == "True":
    async def fail(*args, **kwargs): raise RuntimeError("SDK close failed")
    Browser.close = fail
runpy.run_module("pulsara_agent.conversation_kernel.visualization_screenshot_worker", run_name="__main__")
'''

    async def spawn(_executable, *_args, **kwargs):
        return await original(sys.executable, "-c", code, str(path), str(fail_close), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return server, ready


async def assert_browser_exited(info):
    deadline = monotonic() + 10  # Test observation only; no production lifetime cap.
    while True:
        try:
            os.killpg(info["group"], 0)
        except ProcessLookupError:
            return
        assert monotonic() < deadline, f"owned browser group remains: {info}"
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("outcome", ["normal", "cancel", "deadline", "sdk-close-failure"])
def test_real_chromium_cleanup_uses_the_original_sdk_owner(monkeypatch, tmp_path, outcome):
    async def run():
        # macOS Unix sockets have a short pathname limit.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ps-", dir="/tmp") as root:
            server, started = await browser_barrier(
                monkeypatch, Path(root) / "browser.sock", fail_close=outcome == "sdk-close-failure"
            )
            owner = screenshot.VisualizationScreenshotOwner()
            html = b"<main>Hello</main>"
            if outcome == "deadline":
                html = b"<script>while(true){}</script>"
            rendering = asyncio.create_task(owner.render(html, deadline_monotonic=monotonic() + (5 if outcome == "deadline" else 20)))
            try:
                info, writer = await asyncio.wait_for(started, 10)
                if outcome == "cancel":
                    rendering.cancel()
                writer.write(b"1")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                if outcome == "normal":
                    assert (await rendering).startswith(b"\x89PNG")
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await rendering
                else:
                    with pytest.raises(screenshot.VisualizationScreenshotError, match="PREVIEW_CLOSE_UNCONFIRMED" if outcome == "sdk-close-failure" else "PREVIEW_DEADLINE_EXPIRED"):
                        await rendering
                await assert_browser_exited(info)
                if outcome == "sdk-close-failure":
                    with pytest.raises(screenshot.VisualizationScreenshotError, match="PREVIEW_CLOSE_UNCONFIRMED"):
                        await owner.aclose()
                    assert not owner.local_children_stopped and owner._processes
                else:
                    await owner.aclose()
                    assert owner.local_children_stopped
            finally:
                server.close()
                await server.wait_closed()

    asyncio.run(run())
