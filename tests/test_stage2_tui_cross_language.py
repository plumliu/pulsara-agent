"""Protocol-v3 Python Gateway to Go TUI activation dogfood."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.terminal_client.binary import resolve_terminal_client_binary
from pulsara_agent.terminal_client.v3_launcher import _bootstrap
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class _TuiHost:
    def __init__(self, repository: ConversationKernelRepository, session_id: str) -> None:
        self.repository = repository
        self.session_id = session_id
        self.runtime_session_id = session_id
        self.host_session_id = _name("host")
        self.live_bus = LiveAgentEventBus()
        self.live_control = SessionLiveControlOwner(
            session_id=session_id, owner_epoch=1
        )
        self._controller = ""

    def attach_controller(self, attachment_id: str) -> bool:
        if self._controller:
            return False
        self._controller = attachment_id
        return True

    async def controller_detached(self, attachment_id: str) -> None:
        if self._controller == attachment_id:
            self._controller = ""

    async def stop_current_turn(self) -> bool:
        return False

    async def query_command(self, _command_id: str):
        return None


def _read_until(fd: int, needles: tuple[bytes, ...], timeout: float) -> bytes:
    deadline = monotonic() + timeout
    output = bytearray()
    while monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], min(0.1, deadline - monotonic()))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        output.extend(chunk)
        if all(needle in output for needle in needles):
            break
    return bytes(output)


def test_stage2_python_gateway_to_go_tui_fresh_snapshot_and_detach(
    stage2_migrated_postgres_database,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "pulsara-tui"
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(binary), "./cmd/pulsara-tui"],
        cwd="clients/terminal",
        check=True,
    )

    async def scenario() -> None:
        provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
        repository = ConversationKernelRepository(provider)
        session_id = _name("session")
        lease = repository.acquire_host_writer(
            session_id=session_id,
            workspace_id=_name("workspace"),
            writer_owner_id=_name("host"),
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        repository.start_root_turn(
            lease.guard,
            command_id=_name("command"),
            turn_id=_name("turn"),
            entry_id=_name("entry"),
            context_binding_revision_id=_name("context-revision"),
            permission_snapshot_id=_name("permission-snapshot"),
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            content=InlineContent.from_bytes(b"PROTOCOL_V3_FRESH_SNAPSHOT"),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        host = _TuiHost(repository, session_id)
        socket_root = TemporaryDirectory(prefix="pulsara-tui-v3-test-", dir="/tmp")
        server = TerminalKernelProtocolServer(
            socket_path=Path(socket_root.name) / "client.sock",
            session_provider=lambda host_id: (
                host
                if host_id == host.host_session_id
                else (_ for _ in ()).throw(KeyError(host_id))
            ),
        )
        await server.start()
        carrier = _bootstrap(
            server=server,
            host_session=host,  # type: ignore[arg-type]
            client_instance_id=_name("terminal-client"),
        )
        payload = carrier.SerializeToString(deterministic=True)
        read_fd, write_fd = os.pipe()
        master_fd, slave_fd = pty.openpty()
        os.set_inheritable(read_fd, True)
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 100, 0, 0),
        )
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--bootstrap-fd",
            str(read_fd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env={
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                "PULSARA_TUI_BOOTSTRAP_FD": str(read_fd),
            },
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        os.close(read_fd)
        os.close(slave_fd)
        os.write(write_fd, len(payload).to_bytes(4, "big") + payload)
        os.close(write_fd)
        try:
            output = await asyncio.to_thread(
                _read_until,
                master_fd,
                (b"ready", b"controller", b"PROTOCOL_V3_FRESH_SNAPSHOT"),
                10.0,
            )
            assert b"fatal" not in output
            assert b"PROTOCOL_V3_FRESH_SNAPSHOT" in output
            os.write(master_fd, b"\x04")
            restored = await asyncio.to_thread(
                _read_until, master_fd, (b"\x1b[?1049l",), 5.0
            )
            assert await asyncio.wait_for(process.wait(), timeout=5.0) == 0
            assert b"\x1b[?1049l" in restored
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            os.close(master_fd)
            await server.close()
            host.live_control.close()
            host.live_bus.close()
            socket_root.cleanup()

        verified = await resolve_terminal_client_binary(binary)
        assert verified.protocol_major == 3

    asyncio.run(scenario())
