"""Renderer-neutral supervision for the Protocol-v3 terminal child."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import sys

from pulsara_agent.terminal_client.binary import TerminalClientBinary
from pulsara_agent.terminal_client.supervision import (
    restore_foreground,
    transfer_foreground_to_child,
)


BOOTSTRAP_MAXIMUM_BYTES = 16 * 1024
CHILD_GRACEFUL_EXIT_SECONDS = 5.0
CHILD_FORCE_EXIT_SECONDS = 2.0
_CLEAR_TERMINAL_DISPLAY_AND_SCROLLBACK = b"\x1b[H\x1b[2J\x1b[3J"


class TerminalClientLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TerminalClientExit:
    returncode: int
    binary: TerminalClientBinary
    client_instance_id: str


@dataclass(slots=True)
class _TerminalOutputOwnershipLease:
    child_stdout_fd: int | None
    child_stderr_fd: int | None
    saved_by_fd: dict[int, int]
    streams: tuple[object, ...]
    restored: bool = False

    def restore(self) -> None:
        if self.restored:
            return
        for stream in self.streams:
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass
        restore_error: OSError | None = None
        try:
            for target_fd, saved_fd in self.saved_by_fd.items():
                try:
                    os.dup2(saved_fd, target_fd)
                except OSError as exc:
                    restore_error = restore_error or exc
        finally:
            for saved_fd in self.saved_by_fd.values():
                try:
                    os.close(saved_fd)
                except OSError:
                    pass
            self.restored = True
        if restore_error is not None:
            raise TerminalClientLaunchError(
                "terminal parent output ownership could not be restored"
            ) from restore_error


async def launch_terminal_child(
    *,
    binary: TerminalClientBinary,
    bootstrap_payload: bytes,
) -> int:
    if not bootstrap_payload or len(bootstrap_payload) > BOOTSTRAP_MAXIMUM_BYTES:
        raise TerminalClientLaunchError("terminal bootstrap exceeds its hard bound")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    process: asyncio.subprocess.Process | None = None
    foreground_lease = None
    output_lease: _TerminalOutputOwnershipLease | None = None
    try:
        output_lease = _acquire_terminal_output_ownership(
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        process = await asyncio.create_subprocess_exec(
            str(binary.path),
            "--bootstrap-fd",
            str(read_fd),
            stdin=None,
            stdout=output_lease.child_stdout_fd,
            stderr=output_lease.child_stderr_fd,
            env=_sanitized_child_environment(read_fd),
            pass_fds=(read_fd,),
            process_group=0,
        )
        os.close(read_fd)
        read_fd = -1
        foreground_lease = transfer_foreground_to_child(process.pid)
        await asyncio.to_thread(_write_bootstrap_once, write_fd, bootstrap_payload)
        os.close(write_fd)
        write_fd = -1
        return await process.wait()
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await _terminate_child(process)
        raise
    except OSError as exc:
        if process is not None and process.returncode is None:
            await _terminate_child(process)
        raise TerminalClientLaunchError(
            "terminal client process or foreground-group setup failed"
        ) from exc
    finally:
        try:
            try:
                restore_foreground(foreground_lease)
            finally:
                if output_lease is not None:
                    output_lease.restore()
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


def clear_terminal_display_and_scrollback(output) -> None:
    try:
        file_descriptor = output.fileno()
    except (AttributeError, OSError, ValueError) as exc:
        raise TerminalClientLaunchError(
            "terminal scrollback erase requires a writable terminal"
        ) from exc
    if not os.isatty(file_descriptor):
        raise TerminalClientLaunchError(
            "terminal scrollback erase requires a writable terminal"
        )
    try:
        output.flush()
        remaining = memoryview(_CLEAR_TERMINAL_DISPLAY_AND_SCROLLBACK)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError("terminal erase write stopped early")
            remaining = remaining[written:]
    except (OSError, ValueError) as exc:
        raise TerminalClientLaunchError(
            "terminal display and scrollback could not be erased"
        ) from exc


def _acquire_terminal_output_ownership(
    *, stdout, stderr
) -> _TerminalOutputOwnershipLease:
    streams = (stdout, stderr)
    stream_fds: list[int | None] = []
    terminal_fds: set[int] = set()
    for stream in streams:
        try:
            stream.flush()
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            stream_fds.append(None)
            continue
        stream_fds.append(fd)
        if os.isatty(fd):
            terminal_fds.add(fd)
    saved_by_fd: dict[int, int] = {}
    sink_fd = -1
    try:
        for fd in terminal_fds:
            saved_by_fd[fd] = os.dup(fd)
        if terminal_fds:
            sink_fd = os.open(os.devnull, os.O_WRONLY)
            for fd in terminal_fds:
                os.dup2(sink_fd, fd)
    except OSError as exc:
        for target_fd, saved_fd in saved_by_fd.items():
            try:
                os.dup2(saved_fd, target_fd)
            except OSError:
                pass
            try:
                os.close(saved_fd)
            except OSError:
                pass
        raise TerminalClientLaunchError(
            "terminal parent output ownership could not be isolated"
        ) from exc
    finally:
        if sink_fd >= 0:
            os.close(sink_fd)

    def child_target(index: int) -> int | None:
        fd = stream_fds[index]
        return None if fd is None else saved_by_fd.get(fd)

    return _TerminalOutputOwnershipLease(
        child_stdout_fd=child_target(0),
        child_stderr_fd=child_target(1),
        saved_by_fd=saved_by_fd,
        streams=streams,
    )


def _sanitized_child_environment(bootstrap_fd: int) -> dict[str, str]:
    allowed_exact = {"TERM", "COLORTERM", "LANG", "NO_COLOR", "TMUX", "SSH_TTY"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed_exact or key.startswith("LC_")
    }
    environment["PULSARA_TUI_BOOTSTRAP_FD"] = str(bootstrap_fd)
    return environment


def _write_bootstrap_once(fd: int, payload: bytes) -> None:
    framed = len(payload).to_bytes(4, "big") + payload
    view = memoryview(framed)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise TerminalClientLaunchError("terminal bootstrap pipe stopped early")
        view = view[written:]


async def _terminate_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await asyncio.shield(process.wait())
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(process.wait()), timeout=CHILD_GRACEFUL_EXIT_SECONDS
        )
        return
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await asyncio.wait_for(
        asyncio.shield(process.wait()), timeout=CHILD_FORCE_EXIT_SECONDS
    )


__all__ = [
    "TerminalClientExit",
    "TerminalClientLaunchError",
    "clear_terminal_display_and_scrollback",
    "launch_terminal_child",
]
