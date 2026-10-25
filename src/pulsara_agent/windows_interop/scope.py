"""One process-local physical scope across Linux and an exact Windows Job."""

from __future__ import annotations

import asyncio
from concurrent.futures import (
    CancelledError as FutureCancelled,
    Future,
    TimeoutError as FutureTimeout,
)
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
from threading import Event, Lock, Thread

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.windows_interop import InteropConfiguration
from pulsara_agent.windows_interop.relay import InteropRelay


class InteropScopeError(RuntimeError):
    pass


class WindowsInteropScope:
    def __init__(self, configuration: InteropConfiguration) -> None:
        self.configuration = configuration
        self.cancelled = Event()
        self.failed = Event()
        self.physically_joined = Event()
        self.failure: str | None = None
        self.directory: Path | None = None
        self.relay: InteropRelay | None = None
        self.helper: asyncio.subprocess.Process | None = None
        self.diagnostic: asyncio.subprocess.Process | None = None
        self.helper_ready: dict | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._thread_ready = Event()
        self._lock = Lock()
        self._prepare_future: Future | None = None
        self._close_future: Future | None = None
        self._reader_task: asyncio.Task | None = None
        self._anchor_watch_task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._verified: asyncio.Future | None = None
        self._joined: asyncio.Future | None = None
        self._write_lock: asyncio.Lock | None = None
        self._worker_error: BaseException | None = None
        self._stderr_task: asyncio.Task | None = None
        self._cancel_send_task: asyncio.Task | None = None
        self._requested_close = False
        self.diagnostic_error = ""

    def _worker(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except BaseException as error:
            self._worker_error = error
            self._thread_ready.set()
            return
        self._thread_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _start(self) -> Future:
        with self._lock:
            if self._prepare_future is None:
                self._thread = Thread(
                    target=self._worker, name="windows-interop-scope", daemon=False
                )
                try:
                    self._thread.start()
                except BaseException:
                    if not self._thread.is_alive():
                        self._thread = None
                    raise
                self._thread_ready.wait()
                if self._worker_error is not None:
                    raise self._worker_error
                assert self._loop is not None
                self._prepare_future = asyncio.run_coroutine_threadsafe(
                    self._prepare(), self._loop
                )
            return self._prepare_future

    def prepare(self) -> None:
        try:
            self._start().result()
        except BaseException as error:
            self.request_cancel()
            self.finish(cancel=True)
            self.retire()
            if self.diagnostic_error:
                error.add_note(self.diagnostic_error)
            if isinstance(error, FutureCancelled):
                raise InteropScopeError(
                    "Windows interop launch was cancelled"
                ) from error
            raise

    async def prepare_async(self) -> None:
        try:
            await asyncio.shield(asyncio.wrap_future(self._start()))
        except BaseException:
            self.request_cancel()
            try:
                await self.finish_async(cancel=True)
            finally:
                if self.physically_joined.is_set():
                    self.retire()
            raise

    def environment(self, source: dict[str, str]) -> dict[str, str]:
        if self.cancelled.is_set() or self.relay is None or self.helper_ready is None:
            raise InteropScopeError("Windows interop scope is not accepting a launch")
        return {**source, "WSL_INTEROP": str(self.relay.path)}

    async def _send(self, frame: dict) -> None:
        if self.helper is None or self.helper.stdin is None:
            return
        assert self._write_lock is not None
        async with self._write_lock:
            if frame.get("operation") in {"finish", "cancel"}:
                self._requested_close = True
            self.helper.stdin.write(
                (json.dumps(frame, ensure_ascii=False) + "\n").encode()
            )
            await self.helper.stdin.drain()

    async def _read_helper(self) -> None:
        assert self.helper is not None and self.helper.stdout is not None
        futures = {
            "ready": self._ready,
            "verified": self._verified,
            "joined": self._joined,
        }
        failure_frame_seen = False
        try:
            while line := await self.helper.stdout.readline():
                if not line.endswith(b"\n"):
                    raise InteropScopeError("partial Windows interop control frame")
                frame = json.loads(line.decode("utf-8", "strict"))
                if isinstance(frame, dict) and frame.get("type") == "failed":
                    if failure_frame_seen or frame.get("reason") != "control-eof":
                        raise InteropScopeError("invalid Windows interop failure frame")
                    failure_frame_seen = True
                    self._fail(
                        "Windows interop control pipe closed; the task result is unknown"
                    )
                    continue
                if not isinstance(frame, dict) or frame.get("type") not in futures:
                    raise InteropScopeError("invalid Windows interop control frame")
                kind = frame["type"]
                future = futures[kind]
                if future is None or future.done():
                    raise InteropScopeError("duplicate Windows interop control frame")
                if kind == "ready":
                    if not all(
                        isinstance(frame.get(key), str) and frame[key]
                        for key in ("job", "monitorCreation")
                    ) or not all(
                        type(frame.get(key)) is int and frame[key] > 0
                        for key in ("anchorPid", "monitorPid")
                    ):
                        raise InteropScopeError(
                            "invalid Windows interop scope identity"
                        )
                elif kind == "verified":
                    if type(frame.get("pid")) is not int or not isinstance(
                        frame.get("creation"), str
                    ):
                        raise InteropScopeError(
                            "invalid Windows interop diagnostic identity"
                        )
                elif frame.get("reason") not in {
                    "settled",
                    "owner-lost",
                    "monitor-lost",
                }:
                    raise InteropScopeError("invalid Windows interop join result")
                if kind == "joined" and (
                    frame["reason"] != "settled" or not self._requested_close
                ):
                    self._fail(
                        "Windows interop owner failed; the task result is unknown"
                    )
                future.set_result(frame)
        except BaseException as error:
            self._fail(str(error))
        finally:
            if self._joined is not None and not self._joined.done():
                self._fail("Windows interop control ended; the task result is unknown")
            for future in futures.values():
                if future is not None and not future.done():
                    future.set_exception(
                        InteropScopeError(
                            self.failure
                            or "Windows interop control ended without its result"
                        )
                    )

    async def _prepare(self) -> None:
        if self.cancelled.is_set():
            raise InteropScopeError("Windows interop launch was cancelled")
        parent = require_pulsara_home() / "desktop"
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = parent.stat()
        if (
            parent.resolve() != parent
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise InteropScopeError(
                "Windows interop directory must be private to this Linux user"
            )
        self.directory = Path(tempfile.mkdtemp(prefix="i-", dir=parent))
        self.relay = InteropRelay(self.directory)
        binding = self.relay.start()
        loop = asyncio.get_running_loop()
        self._ready, self._verified, self._joined = (
            loop.create_future(),
            loop.create_future(),
            loop.create_future(),
        )
        for future in (self._ready, self._verified, self._joined):
            future.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
        self._write_lock = asyncio.Lock()
        # Bootstrap has no model command or credentials. The Windows helper
        # creates its own Job, then performs the actual -d/-u anchor launch.
        environment = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "WSL_INTEROP", "WSL_DISTRO_NAME")
            if key in os.environ
        }
        self.helper = await asyncio.create_subprocess_exec(
            str(self.configuration.helper_path),
            "--scope",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        self._reader_task = asyncio.create_task(
            self._read_helper(), name="windows-interop-control"
        )
        assert self.helper.stderr is not None
        # Reuse the finite host-command stderr boundary; this is helper
        # diagnostics, not task output or a new task lifetime/output policy.
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._send(
            dict(
                distribution=self.configuration.distribution,
                user=self.configuration.user,
                python=str(self.configuration.python_executable),
                control=str(self.relay.control_path),
            )
        )
        self.helper_ready = await self._ready
        if self.cancelled.is_set():
            self._cancel_on_loop()
        completed, _ = await asyncio.wait(
            (binding, self._joined), return_when=asyncio.FIRST_COMPLETED
        )
        if binding not in completed:
            raise InteropScopeError(
                "Windows interop anchor exited before descriptor binding"
            )
        await binding
        self._anchor_watch_task = asyncio.create_task(
            self._watch_anchor(), name="windows-interop-anchor"
        )
        if self.cancelled.is_set():
            raise InteropScopeError("Windows interop launch was cancelled")
        self.diagnostic = await asyncio.create_subprocess_exec(
            str(self.configuration.helper_path),
            "--probe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**environment, "WSL_INTEROP": str(self.relay.path)},
            start_new_session=True,
        )
        assert self.diagnostic.stdin is not None and self.diagnostic.stdout is not None
        self.diagnostic.stdin.write(
            (json.dumps(dict(job=self.helper_ready["job"])) + "\n").encode()
        )
        await self.diagnostic.stdin.drain()
        line = await self.diagnostic.stdout.readline()
        if not line.endswith(b"\n"):
            raise InteropScopeError("partial Windows interop diagnostic frame")
        probe = json.loads(line.decode("utf-8", "strict"))
        if (
            not isinstance(probe, dict)
            or probe.get("type") != "probe"
            or type(probe.get("pid")) is not int
            or not isinstance(probe.get("creation"), str)
        ):
            raise InteropScopeError("Windows interop route diagnostic failed")
        await self._send(
            dict(operation="verify", pid=probe["pid"], creation=probe["creation"])
        )
        verified = await self._verified
        if (verified["pid"], verified["creation"]) != (probe["pid"], probe["creation"]):
            raise InteropScopeError("Windows interop route identity changed")
        self.diagnostic.stdin.close()
        await self.diagnostic.communicate()
        if self.diagnostic.returncode != 0:
            raise InteropScopeError("Windows interop diagnostic did not exit normally")
        await self._join_linux_group(self.diagnostic, cancel=False)

    def _fail(self, message: str) -> None:
        self.failure = self.failure or message
        self.failed.set()
        self.cancelled.set()
        self._cancel_on_loop()

    async def _watch_anchor(self) -> None:
        assert self.relay is not None and self.relay.peer_pidfd is not None
        descriptor = self.relay.peer_pidfd
        loop = asyncio.get_running_loop()
        exited = loop.create_future()

        def ready() -> None:
            if not exited.done():
                exited.set_result(None)

        loop.add_reader(descriptor, ready)
        try:
            await exited
            if not self._requested_close:
                self._fail("Windows interop anchor exited; the task result is unknown")
        finally:
            loop.remove_reader(descriptor)

    @staticmethod
    async def _join_linux_group(process, *, cancel: bool) -> None:
        def exists():
            try:
                os.killpg(process.pid, 0)
                return True
            except ProcessLookupError:
                return False

        if cancel and exists():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), 0.5)
            except asyncio.TimeoutError:
                pass
            if exists():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await process.wait()
        while exists():
            await asyncio.sleep(0.005)

    def request_cancel(self) -> None:
        self.cancelled.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_on_loop)

    def _cancel_on_loop(self) -> None:
        if self.relay is not None:
            self.relay.request_cancel()
        if (
            self.helper_ready is not None
            and self.helper is not None
            and self.helper.returncode is None
            and self._cancel_send_task is None
        ):
            self._cancel_send_task = asyncio.create_task(self._send_cancel())

    async def _send_cancel(self) -> None:
        if self.relay is not None:
            await self.relay.seal(cancel=True)
        try:
            await self._send(dict(operation="cancel"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _drain_stderr(self) -> None:
        assert self.helper is not None and self.helper.stderr is not None
        retained = bytearray()
        maximum = 4 * 1024 * 1024
        while chunk := await self.helper.stderr.read(64 * 1024):
            remaining = maximum - len(retained)
            retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.failure = (
                    "Windows interop helper stderr exceeded the finite command boundary"
                )
        self.diagnostic_error = retained.decode("utf-8", "replace")

    async def _close(self) -> None:
        try:
            if self._prepare_future is not None:
                # Cancelling admission does not detach in-flight bootstrap.
                await asyncio.gather(
                    asyncio.wrap_future(self._prepare_future), return_exceptions=True
                )
            if self.relay is not None:
                await self.relay.seal(cancel=self.cancelled.is_set())
            if self._cancel_send_task is not None:
                await self._cancel_send_task
            if self.helper is not None:
                try:
                    await self._send(
                        dict(
                            operation="cancel" if self.cancelled.is_set() else "finish"
                        )
                    )
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if self.diagnostic is not None:
                    await self._join_linux_group(self.diagnostic, cancel=True)
                assert self._joined is not None
                joined = await self._joined
                await self.helper.wait()
                if self._reader_task is not None:
                    await self._reader_task
                if self._stderr_task is not None:
                    await self._stderr_task
                await self._join_linux_group(self.helper, cancel=False)
                if joined["reason"] != "settled" or self.helper.returncode != 0:
                    self.failure = (
                        "Windows interop owner failed; the task result is unknown"
                    )
            self.physically_joined.set()
        finally:
            if self._anchor_watch_task is not None:
                self._anchor_watch_task.cancel()
                await asyncio.gather(self._anchor_watch_task, return_exceptions=True)
            if self.relay is not None:
                self.relay.close_descriptors()
            assert self._loop is not None
            self._loop.call_soon(self._loop.stop)

    def finish(self, *, cancel: bool = False, timeout: float | None = None) -> bool:
        if cancel:
            self.request_cancel()
        with self._lock:
            if self._thread is None:
                self.physically_joined.set()
                return True
            if self._loop is None:
                self._thread_ready.wait()
            if self._worker_error is not None:
                self._thread.join()
                self.physically_joined.set()
                return True
            if self._close_future is None:
                assert self._loop is not None
                self._close_future = asyncio.run_coroutine_threadsafe(
                    self._close(), self._loop
                )
            closed = self._close_future
        try:
            closed.result(timeout=timeout)
        except FutureTimeout:
            return False
        finally:
            if closed.done() and self._thread is not None:
                self._thread.join()
        return self.physically_joined.is_set()

    async def finish_async(self, *, cancel: bool = False) -> None:
        worker = asyncio.create_task(asyncio.to_thread(self.finish, cancel=cancel))
        cancelled = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                self.request_cancel()
        worker.result()
        if cancelled is not None:
            raise cancelled

    def retire(self) -> None:
        if not self.physically_joined.is_set():
            raise InteropScopeError("cannot retire an unjoined Windows interop scope")
        if self.directory is not None:
            for name in ("s", "c"):
                path = self.directory / name
                if os.path.lexists(path):
                    path.unlink()
            self.directory.rmdir()
            self.directory = None
