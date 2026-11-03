"""Killable one-shot screenshot owner; no browser thread can outlive its call."""

from __future__ import annotations

import asyncio
import os
import json
import signal
import sys
from time import monotonic


class VisualizationScreenshotError(RuntimeError):
    pass


class VisualizationScreenshotOwner:
    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._renders: set[asyncio.Task[bytes]] = set()
        self._closed = False

    def stop_admission(self) -> None:
        self._closed = True

    @property
    def local_children_stopped(self) -> bool:
        # A launch in flight is owned by its original render task, even before
        # asyncio has returned a Process to put in _processes.
        return self._closed and not self._renders and not self._processes

    async def render(self, html: bytes, *, deadline_monotonic: float, thumbnail: bool = False, svg: bool = False, resource_prefix: str | None = None) -> bytes:
        if self._closed:
            raise VisualizationScreenshotError("PREVIEW_OWNER_CLOSED")
        task = asyncio.create_task(self._render(
            html, deadline_monotonic=deadline_monotonic, thumbnail=thumbnail,
            svg=svg, resource_prefix=resource_prefix,
        ), name="visualization-screenshot")
        self._renders.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done() and not task.cancelling():
                task.cancel()
            await _join(task, propagate=False)
            raise
        finally:
            if task.done():
                self._renders.discard(task)

    async def _render(self, html: bytes, *, deadline_monotonic: float, thumbnail: bool, svg: bool, resource_prefix: str | None) -> bytes:
        if self._closed:
            raise VisualizationScreenshotError("PREVIEW_OWNER_CLOSED")
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED")
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pulsara_agent.conversation_kernel.visualization_screenshot_worker",
            *(["--thumbnail"] if thumbnail else []),
            *(["--svg"] if svg else []),
            *(["--local-media"] if resource_prefix is not None else []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        ))
        process = None
        communication = None
        ready = False
        started = False
        try:
            try:
                process = await asyncio.shield(launch)
            except asyncio.CancelledError:
                # Cancellation cannot abandon asyncio's actual launch owner.
                process = await _join(launch)
                raise
            self._processes.add(process)
            remaining = max(0.0, deadline_monotonic - monotonic())
            readiness = asyncio.create_task(process.stdout.readexactly(1))
            try:
                ready = await asyncio.wait_for(asyncio.shield(readiness), remaining) == b"\x00"
            except TimeoutError as exc:
                raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED") from exc
            except asyncio.IncompleteReadError as exc:
                raise VisualizationScreenshotError("PREVIEW_WORKER_START_FAILED") from exc
            finally:
                if not readiness.done():
                    readiness.cancel()
                await _join(readiness, propagate=False)
            if not ready:
                raise VisualizationScreenshotError("PREVIEW_WORKER_START_FAILED")
            if self._closed:
                raise VisualizationScreenshotError("PREVIEW_OWNER_CLOSED")
            communication = asyncio.create_task(process.communicate(
                html if resource_prefix is None else json.dumps({"html": html.decode("utf-8"), "resource_prefix": resource_prefix}, ensure_ascii=False).encode("utf-8")
            ))
            started = True
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communication), timeout=max(0.0, deadline_monotonic - monotonic())
                )
            except TimeoutError as exc:
                raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED") from exc
            if process.returncode != 0 or not stdout:
                raise VisualizationScreenshotError(
                    "PREVIEW_RENDER_FAILED: " + stderr.decode("utf-8", "replace")[:500]
                )
            return stdout
        finally:
            if process is not None:
                self._processes.add(process)
                if process.returncode is None:
                    try:
                        if started:
                            # Chromium uses its own detached group. Only the
                            # worker's supported SDK browser.close()/driver stop
                            # owns that group's closeOrKill deadline and join.
                            process.send_signal(signal.SIGTERM)
                        else:
                            # HTML has never been supplied, so the handshake
                            # contract proves no SDK/Chromium child can exist.
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if communication is not None:
                    await _join(communication, propagate=False)
                await _join(asyncio.create_task(process.wait()))
                if not started or process.returncode in (0, 1):
                    self._processes.discard(process)
                else:
                    # Keep the exact owner reachable. Killing the Python worker
                    # is never evidence that detached Chromium also stopped.
                    raise VisualizationScreenshotError("PREVIEW_CLOSE_UNCONFIRMED")

    async def aclose(self) -> None:
        self.stop_admission()
        tasks = tuple(self._renders)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        for task in tasks:
            await _join(task, propagate=False)
            self._renders.discard(task)
        if self._processes:
            raise VisualizationScreenshotError("PREVIEW_CLOSE_UNCONFIRMED")


async def _join(task, *, propagate: bool = True):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if propagate:
        return task.result()
    if not task.cancelled():
        task.exception()
