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

from pulsara_agent.terminal_client import resolve_terminal_client_binary
from pulsara_agent.terminal_client.launcher import (
    build_terminal_client_bootstrap_carrier,
)
from pulsara_agent.terminal_protocol.gateway import TerminalProtocolServer
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire


def legacy_python_gateway_to_go_fresh_generation_zero_bootstrap(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "pulsara-tui"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/pulsara-tui"],
        cwd="clients/terminal",
        check=True,
    )
    core = _core(monkeypatch, ScriptedTransport([]))

    async def scenario() -> None:
        session = await _open_project_session(core, tmp_path)
        socket_root = TemporaryDirectory(prefix="pulsara-tui-genesis-", dir="/tmp")
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
            client_instance_id="terminal-client:fresh-generation-zero",
        )
        payload = carrier.SerializeToString(deterministic=True)
        read_fd, write_fd = os.pipe()
        master_fd, slave_fd = pty.openpty()
        os.set_inheritable(read_fd, True)
        import fcntl

        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 80, 0, 0),
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
                _read_pty_until,
                master_fd,
                (b"ready", b"controller", b"Type a message"),
                8.0,
            )
            assert b"ready" in output
            assert b"controller" in output
            assert b"Type a message" in output
            assert b"fatal" not in output
            assert b"terminal reconnect timer authority is stale" not in output
            os.write(master_fd, b"\x04")
            restored = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"\x1b[?1049l",),
                5.0,
            )
            assert await asyncio.wait_for(process.wait(), timeout=5.0) == 0
            assert b"\x1b[?1049l" in restored
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            os.close(master_fd)
            await server.close()
            socket_root.cleanup()
            await core.shutdown()

    asyncio.run(scenario())


def legacy_python_gateway_to_go_s1_read_only_viewport(
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
            requested_attachment_role=wire.ATTACHMENT_ROLE_OBSERVER,
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


def legacy_python_gateway_to_go_s3_controller_submit_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "pulsara-tui"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/pulsara-tui"],
        cwd="clients/terminal",
        check=True,
    )
    expected_reply = "S3 submitted prompt reached the runtime 你好"
    core = _core(
        monkeypatch,
        ScriptedTransport([{"text": "S3 setup transcript"}, {"text": expected_reply}]),
    )

    async def scenario() -> None:
        session = await _open_project_session(core, tmp_path)
        setup = await session.run_turn("prepare the S3 transcript")
        assert setup.final_text == "S3 setup transcript"
        await _wait_for_presented_text(session, setup.final_text)
        socket_root = TemporaryDirectory(prefix="pulsara-tui-s3-", dir="/tmp")
        socket_path = Path(socket_root.name) / "client.sock"
        server = TerminalProtocolServer(
            socket_path=socket_path,
            session_provider=lambda host_id: (
                session
                if host_id == session.host_session_id
                else (_ for _ in ()).throw(KeyError(host_id))
            ),
        )
        observed_outcomes: list[str] = []
        original_observe_next = server._observe_next

        async def observed_next(request, state, host):
            response = await original_observe_next(request, state, host)
            observation = response.observation
            outer = observation.WhichOneof("outcome") or ""
            durable = ""
            revision = 0
            if outer == "batch" and observation.batch.HasField("durable"):
                durable = observation.batch.durable.WhichOneof("outcome") or ""
                if durable == "root_advanced":
                    revision = observation.batch.durable.root_advanced.resulting_projection_revision
            contains_reply = expected_reply.encode() in response.SerializeToString()
            observed_outcomes.append(
                f"{outer}:{durable}:revision={revision}:reply={contains_reply}"
            )
            return response

        server._observe_next = observed_next
        await server.start()
        carrier = build_terminal_client_bootstrap_carrier(
            server=server,
            host_session=session,
            client_instance_id="terminal-client:s3-cross-language",
        )
        payload = carrier.SerializeToString(deterministic=True)
        read_fd, write_fd = os.pipe()
        master_fd, slave_fd = pty.openpty()
        os.set_inheritable(read_fd, True)
        import fcntl

        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
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
            ready = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"ready", b"controller", b"Type a message"),
                8.0,
            )
            assert (
                b"ready" in ready
                and b"controller" in ready
                and b"Type a message" in ready
            )
            os.write(master_fd, b"Please answer this S3 prompt\r")
            receipt_output = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"RUN_COMPLETED",),
                10.0,
            )
            assert b"RUN_COMPLETED" in receipt_output
            await _wait_for_presented_text(session, expected_reply)
            observation_deadline = asyncio.get_running_loop().time() + 8.0
            while (
                not any("reply=True" in item for item in observed_outcomes)
                and asyncio.get_running_loop().time() < observation_deadline
            ):
                await asyncio.sleep(0.01)
            assert any("reply=True" in item for item in observed_outcomes)
            assert process.returncode is None
            os.write(master_fd, b"\x04")
            restored = await asyncio.to_thread(
                _read_pty_until,
                master_fd,
                (b"\x1b[?1049l",),
                5.0,
            )
            assert await asyncio.wait_for(process.wait(), timeout=5.0) == 0
            assert b"\x1b[?1049l" in restored
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    process.kill()
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
