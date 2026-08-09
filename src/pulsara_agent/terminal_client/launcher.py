"""Launch the Go client while Python retains protocol and session authority."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pulsara_agent.terminal_client.binary import (
    TerminalClientBinary,
    resolve_terminal_client_binary,
)
from pulsara_agent.terminal_client.supervision import (
    restore_foreground,
    transfer_foreground_to_child,
)
if TYPE_CHECKING:
    from pulsara_agent.terminal_protocol.gateway import TerminalProtocolServer
    from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire


_BOOTSTRAP_MAXIMUM_BYTES = 16 * 1024
_BOOTSTRAP_TTL_SECONDS = 30.0
_PARENT_RELAUNCH_EXIT_CODE = 75
_MAXIMUM_PARENT_RELAUNCHES = 1
_CHILD_GRACEFUL_EXIT_SECONDS = 5.0
_CHILD_FORCE_EXIT_SECONDS = 2.0
_CLEAR_TERMINAL_DISPLAY_AND_SCROLLBACK = b"\x1b[H\x1b[2J\x1b[3J"


class TerminalClientLaunchError(RuntimeError):
    """The client process could not be securely launched or supervised."""


@dataclass(frozen=True, slots=True)
class TerminalClientExit:
    returncode: int
    binary: TerminalClientBinary
    client_instance_id: str


@dataclass(slots=True)
class _TerminalOutputOwnershipLease:
    """Give the renderer exclusive writes to the physical terminal.

    The Go child writes through duplicates of the original terminal file
    descriptions.  While it is alive, the Python parent's terminal-backed
    stdout/stderr descriptors point at ``/dev/null`` so third-party imports,
    C extensions, and logging handlers cannot corrupt Bubble Tea's cursor
    state.  Non-terminal streams retain their existing ownership.
    """

    child_stdout_fd: int | None
    child_stderr_fd: int | None
    _saved_by_fd: dict[int, int]
    _streams: tuple[object, ...]
    _restored: bool = False

    def restore(self) -> None:
        if self._restored:
            return
        for stream in self._streams:
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass
        restore_error: OSError | None = None
        try:
            for target_fd, saved_fd in self._saved_by_fd.items():
                try:
                    os.dup2(saved_fd, target_fd)
                except OSError as exc:
                    restore_error = restore_error or exc
        finally:
            for saved_fd in self._saved_by_fd.values():
                try:
                    os.close(saved_fd)
                except OSError:
                    pass
            self._restored = True
        if restore_error is not None:
            raise TerminalClientLaunchError(
                "terminal parent output ownership could not be restored"
            ) from restore_error


async def launch_terminal_client(
    *,
    host_session,
    binary_path: Path | str | None = None,
    clear_scrollback: bool = False,
) -> TerminalClientExit:
    """Serve one controller-preferred client until local quit; never close the conversation."""

    binary = await resolve_terminal_client_binary(binary_path)
    from pulsara_agent.terminal_protocol.gateway import TerminalProtocolServer

    runtime_root = Path(tempfile.mkdtemp(prefix="pulsara-tui-", dir="/tmp"))
    os.chmod(runtime_root, 0o700)
    socket_path = runtime_root / "client.sock"
    server = TerminalProtocolServer(
        socket_path=socket_path,
        session_provider=lambda host_id: _exact_host_session(host_session, host_id),
    )
    client_instance_id = ""
    try:
        await server.start()
        if clear_scrollback:
            _clear_terminal_display_and_scrollback(sys.stdout)
        launch_id, launch_capability = server.launch_id, server.launch_capability
        for relaunch_count in range(_MAXIMUM_PARENT_RELAUNCHES + 1):
            client_instance_id = f"terminal-client:{uuid4().hex}"
            carrier = build_terminal_client_bootstrap_carrier(
                server=server,
                host_session=host_session,
                client_instance_id=client_instance_id,
                launch_id=launch_id,
                launch_capability=launch_capability,
            )
            returncode = await _launch_one_terminal_client(
                binary=binary,
                carrier=carrier,
            )
            if returncode == 0:
                return TerminalClientExit(
                    returncode=returncode,
                    binary=binary,
                    client_instance_id=client_instance_id,
                )
            if (
                returncode != _PARENT_RELAUNCH_EXIT_CODE
                or relaunch_count >= _MAXIMUM_PARENT_RELAUNCHES
            ):
                raise TerminalClientLaunchError(
                    f"terminal client exited with status {returncode}"
                )
            launch_id, launch_capability = server.issue_launch_credential()
        raise AssertionError("terminal relaunch loop is not exhaustive")
    finally:
        await server.close()
        shutil.rmtree(runtime_root, ignore_errors=True)


def build_terminal_client_bootstrap_carrier(
    *,
    server: TerminalProtocolServer,
    host_session,
    client_instance_id: str,
    launch_id: str | None = None,
    launch_capability: bytes | None = None,
    requested_attachment_role: int | None = None,
) -> wire.TerminalClientBootstrapCarrier:
    from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
    from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
        install_protobuf_fingerprint,
    )

    requested_attachment_role = (
        wire.ATTACHMENT_ROLE_CONTROLLER
        if requested_attachment_role is None
        else requested_attachment_role
    )
    if requested_attachment_role not in {
        wire.ATTACHMENT_ROLE_OBSERVER,
        wire.ATTACHMENT_ROLE_CONTROLLER,
    }:
        raise ValueError("terminal bootstrap attachment role is unknown")
    now = datetime.now(UTC)
    carrier = wire.TerminalClientBootstrapCarrier(
        carrier_version=1,
        launch_id=launch_id or server.launch_id,
        client_instance_id=client_instance_id,
        host_session_id=host_session.host_session_id,
        runtime_session_id=host_session.runtime_session_id,
        unix_socket_path=str(server.socket_path),
        launch_capability=(
            bytes(launch_capability)
            if launch_capability is not None
            else server.launch_capability
        ),
        requested_attachment_role=requested_attachment_role,
        parent_pid=os.getpid(),
        issued_at_utc=now.isoformat().replace("+00:00", "Z"),
        expires_at_utc=(now + timedelta(seconds=_BOOTSTRAP_TTL_SECONDS))
        .isoformat()
        .replace("+00:00", "Z"),
        carrier_nonce=secrets.token_bytes(32),
    )
    install_protobuf_fingerprint(
        "terminal-client-bootstrap:v1",
        carrier,
        own_field="bootstrap_fingerprint",
    )
    return carrier


async def _launch_one_terminal_client(
    *,
    binary: TerminalClientBinary,
    carrier: wire.TerminalClientBootstrapCarrier,
) -> int:
    payload = carrier.SerializeToString(deterministic=True)
    if not payload or len(payload) > _BOOTSTRAP_MAXIMUM_BYTES:
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
            # The child needs an isolated foreground process group, but must
            # remain in the parent's terminal session so tcsetpgrp() is legal.
            process_group=0,
        )
        os.close(read_fd)
        read_fd = -1
        # The bootstrap pipe is the child's startup gate. Transfer foreground
        # ownership before releasing that gate so Bubble Tea can never race
        # its raw-mode/input initialization while it is still a background
        # process group.
        foreground_lease = transfer_foreground_to_child(process.pid)
        await asyncio.to_thread(_write_bootstrap_once, write_fd, payload)
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


def _exact_host_session(host_session, host_session_id: str):
    if host_session_id != host_session.host_session_id:
        raise KeyError(host_session_id)
    return host_session


def _acquire_terminal_output_ownership(
    *,
    stdout,
    stderr,
) -> _TerminalOutputOwnershipLease:
    """Route parent writes away while preserving exact TTY handles for Go."""

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
        _saved_by_fd=saved_by_fd,
        _streams=streams,
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


def _clear_terminal_display_and_scrollback(output) -> None:
    """Perform the explicitly requested, irreversible terminal-local erase."""

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
            asyncio.shield(process.wait()), timeout=_CHILD_GRACEFUL_EXIT_SECONDS
        )
        return
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await asyncio.wait_for(
        asyncio.shield(process.wait()), timeout=_CHILD_FORCE_EXIT_SECONDS
    )


__all__ = [
    "TerminalClientExit",
    "TerminalClientLaunchError",
    "build_terminal_client_bootstrap_carrier",
    "launch_terminal_client",
]
