from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import textwrap
from types import SimpleNamespace

import pytest

from pulsara_agent.cli import build_parser
from pulsara_agent.terminal_client.binary import (
    TerminalClientBinaryError,
    resolve_terminal_client_binary,
)
from pulsara_agent.terminal_client.launcher import launch_terminal_client
import pulsara_agent.terminal_client.launcher as launcher_module
from pulsara_agent.terminal_protocol.codec import (
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT,
)
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire


def test_binary_resolver_requires_exact_protocol_identity(tmp_path: Path) -> None:
    valid = _fake_binary(tmp_path, schema=PROTOCOL_SCHEMA_FINGERPRINT)
    invalid = _fake_binary(
        tmp_path,
        schema="sha256:" + "0" * 64,
        filename="pulsara-tui-invalid",
    )

    async def scenario() -> None:
        resolved = await resolve_terminal_client_binary(valid)
        assert resolved.path == valid
        assert resolved.protocol_major == PROTOCOL_MAJOR
        with pytest.raises(TerminalClientBinaryError, match="does not match"):
            await resolve_terminal_client_binary(invalid)

    asyncio.run(scenario())


def test_binary_resolver_rejects_symlink_and_bounded_version_output(
    tmp_path: Path,
) -> None:
    valid = _fake_binary(tmp_path, schema=PROTOCOL_SCHEMA_FINGERPRINT)
    symlink = tmp_path / "pulsara-tui-symlink"
    symlink.symlink_to(valid)
    oversized = _fake_version_probe(
        tmp_path,
        filename="pulsara-tui-oversized-version",
        behavior="print('x' * 20000)",
    )

    async def scenario() -> None:
        with pytest.raises(TerminalClientBinaryError, match="regular file"):
            await resolve_terminal_client_binary(symlink)
        with pytest.raises(TerminalClientBinaryError, match="hard bound"):
            await resolve_terminal_client_binary(oversized)

    asyncio.run(scenario())


def test_binary_version_timeout_physically_reaps_probe(
    tmp_path: Path, monkeypatch
) -> None:
    binary = _fake_version_probe(
        tmp_path,
        filename="pulsara-tui-hanging-version",
        behavior="""
import pathlib
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()), encoding='ascii')
while True:
    time.sleep(1)
""",
    )
    pid_path = binary.with_suffix(".pid")
    import pulsara_agent.terminal_client.binary as binary_module

    # Leave enough time for a loaded CI worker to start the interpreter and
    # publish its physical PID before exercising the verifier's timeout path.
    # The timeout still remains far below the production three-second bound.
    monkeypatch.setattr(binary_module, "_VERSION_QUERY_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(binary_module, "_VERSION_PROCESS_GRACE_SECONDS", 0.05)

    async def scenario() -> None:
        with pytest.raises(TerminalClientBinaryError, match="timed out"):
            await resolve_terminal_client_binary(binary)

    asyncio.run(scenario())
    probe_pid = int(pid_path.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(probe_pid, signal.SIGCONT)


def test_launcher_sends_secret_only_through_one_shot_bootstrap_pipe(
    tmp_path: Path,
) -> None:
    binary = _fake_binary(tmp_path, schema=PROTOCOL_SCHEMA_FINGERPRINT)
    observed_path = binary.with_suffix(".observed")
    session = SimpleNamespace(
        host_session_id="host:launcher",
        runtime_session_id="runtime:launcher",
    )

    async def scenario() -> None:
        result = await launch_terminal_client(
            host_session=session,
            binary_path=binary,
        )
        assert result.returncode == 0

    asyncio.run(scenario())
    raw = bytes.fromhex(observed_path.read_text(encoding="ascii"))
    carrier = wire.TerminalClientBootstrapCarrier()
    carrier.ParseFromString(raw)
    assert carrier.host_session_id == session.host_session_id
    assert carrier.runtime_session_id == session.runtime_session_id
    assert carrier.requested_attachment_role == wire.ATTACHMENT_ROLE_OBSERVER
    assert len(carrier.launch_capability) == 32
    assert len(carrier.carrier_nonce) == 32
    script = binary.read_text(encoding="utf-8")
    assert carrier.launch_capability.hex() not in script


def test_host_tui_clear_scrollback_is_explicit_and_defaults_off() -> None:
    parser = build_parser()
    ordinary = parser.parse_args(["host", "tui"])
    private = parser.parse_args(["host", "tui", "--clear-scrollback"])

    assert ordinary.clear_scrollback is False
    assert private.clear_scrollback is True


def test_host_tui_clear_scrollback_help_discloses_irreversible_history_loss(
    capsys,
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["host", "tui", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    normalized_help = " ".join(help_text.split())
    assert "Irreversibly erase" in help_text
    assert "display and scrollback" in normalized_help


def test_explicit_scrollback_erase_uses_one_bounded_terminal_write() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as output:
            original_isatty = launcher_module.os.isatty
            launcher_module.os.isatty = lambda fd: fd == write_fd
            try:
                launcher_module._clear_terminal_display_and_scrollback(output)
            finally:
                launcher_module.os.isatty = original_isatty
        observed = os.read(read_fd, 64)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert observed == b"\x1b[H\x1b[2J\x1b[3J"


def test_explicit_scrollback_erase_fails_closed_for_non_tty() -> None:
    output = SimpleNamespace(fileno=lambda: 123, flush=lambda: None)
    original_isatty = launcher_module.os.isatty
    launcher_module.os.isatty = lambda _fd: False
    try:
        with pytest.raises(
            launcher_module.TerminalClientLaunchError,
            match="requires a writable terminal",
        ):
            launcher_module._clear_terminal_display_and_scrollback(output)
    finally:
        launcher_module.os.isatty = original_isatty


def test_explicit_scrollback_erase_fails_closed_on_partial_write(monkeypatch) -> None:
    output = SimpleNamespace(fileno=lambda: 123, flush=lambda: None)
    monkeypatch.setattr(launcher_module.os, "isatty", lambda _fd: True)
    writes = iter((1, 0))
    monkeypatch.setattr(launcher_module.os, "write", lambda _fd, _payload: next(writes))
    with pytest.raises(
        launcher_module.TerminalClientLaunchError,
        match="could not be erased",
    ):
        launcher_module._clear_terminal_display_and_scrollback(output)


def test_default_launch_never_invokes_irreversible_scrollback_erase(
    tmp_path: Path, monkeypatch
) -> None:
    binary = _fake_binary(tmp_path, schema=PROTOCOL_SCHEMA_FINGERPRINT)
    session = SimpleNamespace(
        host_session_id="host:ordinary",
        runtime_session_id="runtime:ordinary",
    )
    monkeypatch.setattr(
        launcher_module,
        "_clear_terminal_display_and_scrollback",
        lambda _output: (_ for _ in ()).throw(
            AssertionError("default launch attempted irreversible erase")
        ),
    )

    async def scenario() -> None:
        result = await launch_terminal_client(
            host_session=session,
            binary_path=binary,
        )
        assert result.returncode == 0

    asyncio.run(scenario())


def test_private_scrollback_erase_runs_once_before_parent_relaunch(
    tmp_path: Path, monkeypatch
) -> None:
    binary = _fake_relaunch_binary(tmp_path)
    session = SimpleNamespace(
        host_session_id="host:private-relaunch",
        runtime_session_id="runtime:private-relaunch",
    )
    calls: list[object] = []
    monkeypatch.setattr(
        launcher_module,
        "_clear_terminal_display_and_scrollback",
        lambda output: calls.append(output),
    )

    async def scenario() -> None:
        result = await launch_terminal_client(
            host_session=session,
            binary_path=binary,
            clear_scrollback=True,
        )
        assert result.returncode == 0

    asyncio.run(scenario())
    assert calls == [launcher_module.sys.stdout]


def test_launcher_relaunches_once_with_fresh_candidate_credential(
    tmp_path: Path,
) -> None:
    binary = _fake_relaunch_binary(tmp_path)
    observed_path = binary.with_suffix(".observed")
    session = SimpleNamespace(
        host_session_id="host:relaunch",
        runtime_session_id="runtime:relaunch",
    )

    async def scenario() -> None:
        result = await launch_terminal_client(
            host_session=session,
            binary_path=binary,
        )
        assert result.returncode == 0

    asyncio.run(scenario())
    carriers: list[wire.TerminalClientBootstrapCarrier] = []
    for encoded in observed_path.read_text(encoding="ascii").splitlines():
        carrier = wire.TerminalClientBootstrapCarrier()
        carrier.ParseFromString(bytes.fromhex(encoded))
        carriers.append(carrier)
    assert len(carriers) == 2
    assert carriers[0].client_instance_id != carriers[1].client_instance_id
    assert carriers[0].launch_id != carriers[1].launch_id
    assert carriers[0].launch_capability != carriers[1].launch_capability


def test_launcher_cancellation_physically_reaps_unresponsive_child(
    tmp_path: Path, monkeypatch
) -> None:
    binary = _fake_unresponsive_binary(tmp_path)
    pid_path = binary.with_suffix(".pid")
    session = SimpleNamespace(
        host_session_id="host:cancel",
        runtime_session_id="runtime:cancel",
    )
    monkeypatch.setattr(launcher_module, "_CHILD_GRACEFUL_EXIT_SECONDS", 0.05)
    monkeypatch.setattr(launcher_module, "_CHILD_FORCE_EXIT_SECONDS", 1.0)

    async def scenario() -> int:
        task = asyncio.create_task(
            launch_terminal_client(host_session=session, binary_path=binary)
        )
        for _ in range(200):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("terminal child never reached its physical owner")
        pid = int(pid_path.read_text(encoding="ascii"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        return pid

    child_pid = asyncio.run(scenario())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGCONT)


def _fake_binary(
    root: Path,
    *,
    schema: str,
    filename: str = "pulsara-tui",
) -> Path:
    path = root / filename
    version = json.dumps(
        {
            "version": "test",
            "commit": "test",
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "schema_fingerprint": schema,
            "dependency_lock_fingerprint": "test",
            "go_version": "go-test",
            "goos": "test",
            "goarch": "test",
        },
        separators=(",", ":"),
    )
    source = f"""#!{os.sys.executable}
import os
import pathlib
import sys

if '--version-json' in sys.argv:
    print({version!r})
    raise SystemExit(0)
fd = int(sys.argv[sys.argv.index('--bootstrap-fd') + 1])
header = os.read(fd, 4)
size = int.from_bytes(header, 'big')
payload = bytearray()
while len(payload) < size:
    chunk = os.read(fd, size - len(payload))
    if not chunk:
        raise SystemExit(3)
    payload.extend(chunk)
if os.read(fd, 1):
    raise SystemExit(4)
pathlib.Path(__file__).with_suffix('.observed').write_text(bytes(payload).hex(), encoding='ascii')
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path


def _fake_relaunch_binary(root: Path) -> Path:
    path = root / "pulsara-tui-relaunch"
    version = json.dumps(
        {
            "version": "test",
            "commit": "test",
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "schema_fingerprint": PROTOCOL_SCHEMA_FINGERPRINT,
            "dependency_lock_fingerprint": "test",
            "go_version": "go-test",
            "goos": "test",
            "goarch": "test",
        },
        separators=(",", ":"),
    )
    source = f"""#!{os.sys.executable}
import os
import pathlib
import sys

if '--version-json' in sys.argv:
    print({version!r})
    raise SystemExit(0)
fd = int(sys.argv[sys.argv.index('--bootstrap-fd') + 1])
size = int.from_bytes(os.read(fd, 4), 'big')
payload = bytearray()
while len(payload) < size:
    payload.extend(os.read(fd, size - len(payload)))
observed = pathlib.Path(__file__).with_suffix('.observed')
with observed.open('a', encoding='ascii') as stream:
    stream.write(bytes(payload).hex() + '\\n')
attempt = pathlib.Path(__file__).with_suffix('.attempt')
if not attempt.exists():
    attempt.write_text('1', encoding='ascii')
    raise SystemExit(75)
raise SystemExit(0)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path


def _fake_unresponsive_binary(root: Path) -> Path:
    path = root / "pulsara-tui-unresponsive"
    version = json.dumps(
        {
            "version": "test",
            "commit": "test",
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "schema_fingerprint": PROTOCOL_SCHEMA_FINGERPRINT,
            "dependency_lock_fingerprint": "test",
            "go_version": "go-test",
            "goos": "test",
            "goarch": "test",
        },
        separators=(",", ":"),
    )
    source = f"""#!{os.sys.executable}
import os
import pathlib
import signal
import sys
import time

if '--version-json' in sys.argv:
    print({version!r})
    raise SystemExit(0)
fd = int(sys.argv[sys.argv.index('--bootstrap-fd') + 1])
size = int.from_bytes(os.read(fd, 4), 'big')
payload = bytearray()
while len(payload) < size:
    payload.extend(os.read(fd, size - len(payload)))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()), encoding='ascii')
while True:
    time.sleep(1)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path


def _fake_version_probe(root: Path, *, filename: str, behavior: str) -> Path:
    path = root / filename
    indented_behavior = textwrap.indent(behavior.strip(), "    ")
    source = f"""#!{os.sys.executable}
import os
import sys

if '--version-json' in sys.argv:
{indented_behavior}
    raise SystemExit(0)
raise SystemExit(2)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path
