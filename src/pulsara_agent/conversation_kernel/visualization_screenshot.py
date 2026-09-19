"""Killable one-shot screenshot owner; no browser thread can outlive its call."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from time import monotonic


class VisualizationScreenshotError(RuntimeError):
    pass


class VisualizationScreenshotOwner:
    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False

    async def render(self, html: bytes, *, deadline_monotonic: float) -> bytes:
        if self._closed:
            raise VisualizationScreenshotError("PREVIEW_OWNER_CLOSED")
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pulsara_agent.conversation_kernel.visualization_screenshot_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._processes.add(process)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(html), timeout=remaining
                )
            except TimeoutError as exc:
                raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED") from exc
            if process.returncode != 0 or not stdout:
                raise VisualizationScreenshotError(
                    "PREVIEW_RENDER_FAILED: " + stderr.decode("utf-8", "replace")[:500]
                )
            return stdout
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            self._processes.discard(process)

    async def aclose(self) -> None:
        self._closed = True
        for process in tuple(self._processes):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await asyncio.gather(*(process.wait() for process in tuple(self._processes)))
