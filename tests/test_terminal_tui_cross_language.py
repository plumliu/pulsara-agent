from __future__ import annotations

import asyncio
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
from tempfile import TemporaryDirectory
import time

from tests.test_host_core import ScriptedTransport, _core, _open_project_session

from pulsara_agent.terminal_client import (
    build_terminal_client_bootstrap_carrier,
    resolve_terminal_client_binary,
)
from pulsara_agent.terminal_protocol.gateway import TerminalProtocolServer


def test_python_gateway_to_go_s1_read_only_viewport(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "pulsara-tui"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/pulsara-tui"],
        cwd="clients/terminal",
        check=True,
    )
    expected_reply = "S1 real transcript sentinel 你好 🌍"
    core = _core(monkeypatch, ScriptedTransport([{"text": expected_reply}]))

    async def scenario() -> None:
        verified_binary = await resolve_terminal_client_binary(binary)
        assert verified_binary.protocol_major == 2
        session = await _open_project_session(core, tmp_path)
        result = await session.run_turn("S1 cross-language user prompt")
        assert result.final_text == expected_reply
        await _wait_for_presented_text(session, expected_reply)
        socket_root = TemporaryDirectory(prefix="pulsara-tui-cross-", dir="/tmp")
        socket_path = Path(socket_root.name) / "client.sock"
        server = TerminalProtocolServer(
            socket_path=socket_path,
            session_provider=lambda host_id: (
                session
                if host_id == session.host_session_id
                else (_ for _ in ()).throw(KeyError(host_id))
            ),
        )
        await server.start()
        carrier = build_terminal_client_bootstrap_carrier(
            server=server,
            host_session=session,
            client_instance_id="terminal-client:cross-language",
        )
        payload = carrier.SerializeToString(deterministic=True)
        read_fd, write_fd = os.pipe()
        master_fd, slave_fd = pty.openpty()
        os.set_inheritable(read_fd, True)
        # 40 rows, 120 columns.
        import fcntl

        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
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
                _read_pty_until,
                master_fd,
                (b"ready", b"Pulsara", b"S1 real transcript sentinel"),
                8.0,
            )
            assert b"Pulsara" in output
            assert b"ready" in output
            assert b"S1 real transcript sentinel" in output
            assert b"\x1b[?1049h" in output
            assert b"\x1b[?1002h" in output
            assert b"\x1b[?1006h" in output
            assert b"\x1b[3J" not in output

            # The production program must consume the real PTY size rather than
            # a hidden renderer clamp. Width 30 selects the compact footer.
            fcntl.ioctl(
                master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 8, 30, 0, 0),
            )
            os.kill(process.pid, signal.SIGWINCH)
            resized = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"read-only",),
                3.0,
            )
            assert b"read-only" in resized
            assert b"\x1b[3J" not in resized
            os.write(master_fd, b"q")
            restored = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"\x1b[?1049l", b"\x1b[?1002l", b"\x1b[?1006l"),
                5.0,
            )
            assert await asyncio.wait_for(process.wait(), timeout=5.0) == 0
            assert b"\x1b[?1049l" in restored
            assert b"\x1b[?1002l" in restored
            assert b"\x1b[?1006l" in restored
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            os.close(master_fd)
            await server.close()
            socket_root.cleanup()
            await core.shutdown()

    asyncio.run(scenario())


async def _wait_for_presented_text(session, expected: str) -> None:
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        snapshot = session.terminal_application_services.query.snapshot()
        if any(
            expected in str(item.history_entry.cell.model_dump(mode="json"))
            for item in snapshot.viewport.ordered_resident_entries
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("real assistant reply never reached the presentation viewport")


def _read_pty_until(fd: int, needles: tuple[bytes, ...], timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
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
            return bytes(output)
    return bytes(output)
