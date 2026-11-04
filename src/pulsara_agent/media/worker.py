"""Cancelable isolated decoding, owned by the existing tool lifecycle.

asyncio subprocesses own process transport and reap. Each admitted operation
inherits only its bound media descriptor; no persistent jobs or recovery exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import pickle
import json
import inspect
import threading
import os
import sys
from time import monotonic
from typing import TypeVar

import psutil

from pulsara_agent.media.resources import (
    available_worker_memory,
    install_worker_memory_limit,
    MEMORY_OBSERVATION_SECONDS,
)

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.media.source import MediaError, MediaSource

T = TypeVar("T")

# Standard native semaphore shares physical capacity across owners and loops.
_DECODE_SLOTS = threading.BoundedSemaphore(STAGE2_LIMITS.foreground_io_hard_concurrency)


class MediaWorker:
    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._active: set[asyncio.Task] = set()
        self._closing = False

    @property
    def physical_close_complete(self) -> bool:
        return self._closing and not self._processes and not self._active

    def stop_admission(self) -> None:
        self._closing = True

    async def run(
        self,
        operation: Callable[..., T],
        source: MediaSource,
        *args: object,
        deadline_monotonic: float,
        on_progress: Callable[[tuple[float, float]], None] | None = None,
        on_started: Callable[[], object] | None = None,
        **kwargs: object,
    ) -> T:
        if self._closing:
            raise MediaError("CANCELLED", "Media worker is closing.")
        async with asyncio.timeout_at(deadline_monotonic):
            while not _DECODE_SLOTS.acquire(blocking=False):
                if self._closing:
                    raise MediaError("CANCELLED", "Media worker is closing.")
                # Cancellable waiting uses no thread that could acquire a slot
                # after its logical waiter has gone away.
                await asyncio.sleep(MEMORY_OBSERVATION_SECONDS)
        task = asyncio.create_task(
            self._run(
                operation,
                source,
                *args,
                deadline_monotonic=deadline_monotonic,
                on_progress=on_progress,
                on_started=on_started,
                **kwargs,
            )
        )
        self._active.add(task)
        try:
            return await task
        finally:
            self._active.discard(task)
            _DECODE_SLOTS.release()

    async def _run(
        self,
        operation: Callable[..., T],
        source: MediaSource,
        *args: object,
        deadline_monotonic: float,
        on_progress: Callable[[tuple[float, float]], None] | None = None,
        on_started: Callable[[], object] | None = None,
        **kwargs: object,
    ) -> T:
        if self._closing:
            raise MediaError("CANCELLED", "Media worker is closing.")
        source.assert_unchanged()
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("Media operation deadline expired before admission.")
        if not operation.__module__.startswith("pulsara_agent.media."):
            raise ValueError("Media worker only accepts installed media operations.")
        memory_budget = available_worker_memory()
        progress_read, progress_write = os.pipe()
        payload = pickle.dumps(
            (
                operation,
                source,
                args,
                kwargs,
                progress_write if on_progress else None,
                memory_budget,
            ),
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        # Creation is shielded so cancellation cannot orphan a newly spawned C
        # decoder before its process handle has reached this lifecycle owner.
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "pulsara_agent.media.worker",
                "--child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(source.fd, progress_write),
                cwd="/",
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR"}
                },
            )
        )
        process = None
        progress_task = None
        transport = None
        memory_task = None
        memory_exceeded = False
        try:
            try:
                process = await asyncio.shield(launch)
            except asyncio.CancelledError:
                process = await launch
                raise
            os.close(progress_write)
            progress_write = None
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader),
                os.fdopen(progress_read, "rb"),
            )
            progress_read = None

            async def consume_progress():
                while line := await reader.readline():
                    span = json.loads(line)
                    if on_progress is not None:
                        on_progress((float(span[0]), float(span[1])))

            progress_task = asyncio.create_task(consume_progress())

            async def observe_memory():
                nonlocal memory_exceeded
                observed = psutil.Process(process.pid)
                while process.returncode is None:
                    try:
                        if observed.memory_info().rss > memory_budget:
                            memory_exceeded = True
                            process.kill()
                            return
                    except psutil.NoSuchProcess:
                        return
                    await asyncio.sleep(MEMORY_OBSERVATION_SECONDS)

            memory_task = asyncio.create_task(observe_memory())
            self._processes.add(process)
            if self._closing:
                raise MediaError("CANCELLED", "Media worker is closing.")
            async with asyncio.timeout_at(deadline_monotonic):
                if on_started is not None:
                    started = on_started()
                    if inspect.isawaitable(started):
                        await started
                stdout, stderr = await process.communicate(payload)
                await progress_task
            source.assert_unchanged()
            if memory_exceeded:
                raise MediaError(
                    "RESOURCE_BOUNDARY",
                    "Media worker exceeded its admitted physical memory allocation and was terminated.",
                )
            if process.returncode != 0:
                raise MediaError(
                    "MEDIA_DECODE_FAILED",
                    f"Media worker exited with {process.returncode}: {stderr.decode(errors='replace')}",
                )
            success, result = pickle.loads(stdout)
            if not success:
                code, message = result
                raise MediaError(code, message)
            return result
        finally:
            if memory_task is not None:
                memory_task.cancel()
                await asyncio.gather(memory_task, return_exceptions=True)
            if progress_write is not None:
                os.close(progress_write)
            if progress_read is not None:
                os.close(progress_read)
            if transport is not None:
                transport.close()
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.shield(process.wait())
                self._processes.discard(process)

    async def aclose(self) -> None:
        self._closing = True
        active = tuple(self._active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.difference_update(active)
        processes = tuple(self._processes)
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
        self._processes.difference_update(processes)


def _child() -> None:
    # This private pipe is supplied only by the owning local process; it never
    # accepts provider/user bytes as executable pickle instructions.
    operation, source, args, kwargs, progress_fd, memory_budget = pickle.loads(
        sys.stdin.buffer.read()
    )
    if progress_fd is not None:

        def progress(span):
            os.write(progress_fd, (json.dumps(span) + "\n").encode())

        kwargs["progress"] = progress
    try:
        install_worker_memory_limit(memory_budget)
        result = operation(source, *args, **kwargs)
        source.assert_unchanged()
        response = True, result
    except Exception as exc:
        response = (
            False,
            (
                "RESOURCE_BOUNDARY"
                if isinstance(exc, MemoryError)
                else getattr(exc, "code", "MEDIA_DECODE_FAILED"),
                str(exc),
            ),
        )
    finally:
        if progress_fd is not None:
            os.close(progress_fd)
        source.close()
    try:
        encoded = pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL)
    except MemoryError:
        response = False, ("RESOURCE_BOUNDARY", "Media result serialization exceeded the physical memory allocation.")
        encoded = pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL)
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit("Media worker requires its owning process.")
    _child()
