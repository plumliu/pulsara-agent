#!/usr/bin/env python3
"""Real-host SSH/PTTY S0 probe with bounded remote staging and cleanup."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from typing import Any
import uuid

SPIKE_ROOT = Path(__file__).resolve().parents[1]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from probe.parent_probe import ProbeFailure, READY_MARKER  # noqa: E402
from probe.performance_probe import distribution  # noqa: E402


TERM_RELEVANT_LFLAG = termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN
REMOTE_KEYPRESS_P95_LIMIT_MS = 150.0
REMOTE_KEYPRESS_P99_LIMIT_MS = 250.0
REMOTE_STARTUP_LIMIT_MS = 3000.0
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=6",
    "-o",
    "ServerAliveInterval=1",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "LogLevel=ERROR",
)


class RemoteCommandFailure(ProbeFailure):
    pass


def run_command(
    command: list[str],
    *,
    timeout: float = 15.0,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RemoteCommandFailure(
            f"command failed ({completed.returncode}): {command!r}; "
            f"stderr={completed.stderr.decode(errors='replace')!r}"
        )
    return completed


def ssh_command(target: str, remote_command: str, *, check: bool = True) -> bytes:
    completed = run_command(
        ["ssh", "-T", *SSH_OPTIONS, target, remote_command],
        check=check,
    )
    return completed.stdout


def windows_path_to_wsl(path: str) -> str:
    matched = re.fullmatch(r"([A-Za-z]):\\(.*)", path.strip())
    if matched is None:
        raise ProbeFailure(f"unsupported Windows profile path: {path!r}")
    drive = matched.group(1).lower()
    suffix = matched.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{suffix}"


class RemotePTYSession:
    def __init__(self, *, target: str, remote_command: str) -> None:
        self.master_fd, slave_fd = pty.openpty()
        self._saved_termios = termios.tcgetattr(self.master_fd)
        self._set_winsize(120, 32)
        child_env = dict(os.environ)
        child_env.update({"TERM": "xterm-256color", "LC_CTYPE": "UTF-8"})

        def child_setup() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            ["ssh", "-tt", *SSH_OPTIONS, target, remote_command],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env,
            close_fds=True,
            preexec_fn=child_setup,
        )
        os.close(slave_fd)
        self._output = bytearray()
        self._output_lock = threading.Lock()
        self._reader_done = threading.Event()
        self._reader = threading.Thread(
            target=self._read_output,
            name="s0-real-ssh-pty-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_output(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self.master_fd, 65536)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        return
                    raise
                if not chunk:
                    return
                with self._output_lock:
                    self._output.extend(chunk)
        finally:
            self._reader_done.set()

    def _set_winsize(self, width: int, height: int) -> None:
        packed = struct.pack("HHHH", height, width, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)

    def output_position(self) -> int:
        with self._output_lock:
            return len(self._output)

    def output(self) -> bytes:
        with self._output_lock:
            return bytes(self._output)

    def wait_for(
        self, needle: bytes, *, timeout: float = 10.0, since: int = 0
    ) -> float:
        started = time.perf_counter()
        deadline = started + timeout
        while time.perf_counter() < deadline:
            with self._output_lock:
                if needle in self._output[since:]:
                    return time.perf_counter() - started
            if self.process.poll() is not None:
                break
            time.sleep(0.002)
        tail = self.output()[-4000:].decode(errors="replace")
        raise ProbeFailure(
            f"remote PTY did not show {needle!r}; "
            f"returncode={self.process.poll()} tail={tail!r}"
        )

    def write(self, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(self.master_fd, view)
            view = view[written:]

    def wait(self, *, timeout: float = 12.0) -> int:
        try:
            return self.process.wait(timeout=timeout)
        finally:
            self._reader_done.wait(timeout=2.0)

    def terminal_flags(self) -> int:
        return termios.tcgetattr(self.master_fd)[3] & TERM_RELEVANT_LFLAG

    def saved_terminal_flags(self) -> int:
        return self._saved_termios[3] & TERM_RELEVANT_LFLAG

    def emergency_restore(self) -> None:
        termios.tcsetattr(self.master_fd, termios.TCSANOW, self._saved_termios)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5.0)
        self._reader_done.wait(timeout=1.0)
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def remote_process_present(target: str, process_name: str) -> bool:
    completed = run_command(
        [
            "ssh",
            "-T",
            *SSH_OPTIONS,
            target,
            f"wsl.exe -e pgrep -x {process_name}",
        ],
        check=False,
    )
    return completed.returncode == 0


def remote_wsl_path_present(target: str, path: str) -> bool:
    completed = run_command(
        ["ssh", "-T", *SSH_OPTIONS, target, f"wsl.exe -e test -e {path}"],
        check=False,
    )
    return completed.returncode == 0


def run_interactive(
    *,
    target: str,
    launch_command: str,
) -> dict[str, Any]:
    session = RemotePTYSession(target=target, remote_command=launch_command)
    started = time.perf_counter()
    try:
        startup_seconds = session.wait_for(READY_MARKER, timeout=15.0)
        flags_during = session.terminal_flags()
        base = "SSH中文🙂ASCII"
        position = session.output_position()
        session.write(base.encode())
        session.wait_for(base.encode(), since=position)

        key_latencies_ms: list[float] = []
        for index in range(20):
            marker = chr(0x4E00 + index)
            position = session.output_position()
            key_started = time.perf_counter()
            session.write(marker.encode())
            session.wait_for(marker.encode(), timeout=2.0, since=position)
            key_latencies_ms.append((time.perf_counter() - key_started) * 1000)

        session.write(b"\x11")
        returncode = session.wait()
        flags_after = session.terminal_flags()
        output = session.output()
        if returncode != 0:
            raise ProbeFailure(f"remote interactive child exited {returncode}")
        for required in (
            READY_MARKER,
            base.encode(),
            b"\x1b[?1049h",
            b"\x1b[?1049l",
            b"\x1b[?2004h",
            b"\x1b[?2004l",
        ):
            if required not in output:
                raise ProbeFailure(f"remote interactive output missed {required!r}")
        key_distribution = distribution(key_latencies_ms)
        startup_ms = round(startup_seconds * 1000, 3)
        checks = {
            "startup": startup_ms <= REMOTE_STARTUP_LIMIT_MS,
            "keypress_p95": key_distribution["p95"] <= REMOTE_KEYPRESS_P95_LIMIT_MS,
            "keypress_p99": key_distribution["p99"] <= REMOTE_KEYPRESS_P99_LIMIT_MS,
        }
        if not all(checks.values()):
            raise ProbeFailure(
                f"remote interactive latency gate failed: {checks}; "
                f"startup_ms={startup_ms}; keypress={key_distribution}"
            )
        return {
            "status": "pass",
            "startup_ms": startup_ms,
            "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "keypress_latency_ms": key_distribution,
            "latency_gate": {
                "status": "pass",
                "checks": checks,
                "thresholds": {
                    "startup_ms": REMOTE_STARTUP_LIMIT_MS,
                    "keypress_p95_ms": REMOTE_KEYPRESS_P95_LIMIT_MS,
                    "keypress_p99_ms": REMOTE_KEYPRESS_P99_LIMIT_MS,
                },
            },
            "cjk_input_visible": True,
            "alternate_screen_entered": True,
            "alternate_screen_restored": True,
            "bracketed_paste_entered": True,
            "bracketed_paste_restored": True,
            "terminal_was_raw": flags_during != session.saved_terminal_flags(),
            "terminal_restored": flags_after == session.saved_terminal_flags(),
        }
    finally:
        session.close()


def run_abrupt_disconnect(
    *,
    target: str,
    launch_command: str,
    process_name: str,
) -> dict[str, Any]:
    session = RemotePTYSession(target=target, remote_command=launch_command)
    try:
        session.wait_for(READY_MARKER, timeout=15.0)
        flags_during = session.terminal_flags()
        session.process.send_signal(signal.SIGKILL)
        returncode = session.wait()
        flags_after = session.terminal_flags()
        emergency_restore_used = False
        if flags_after != session.saved_terminal_flags():
            session.emergency_restore()
            emergency_restore_used = True
        remote_exited = False
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline:
            if not remote_process_present(target, process_name):
                remote_exited = True
                break
            time.sleep(0.2)
        if not remote_exited:
            raise ProbeFailure("remote Bubble Tea process survived SSH disconnect")
        return {
            "status": "pass",
            "local_ssh_returncode": returncode,
            "terminal_was_raw": flags_during != session.saved_terminal_flags(),
            "terminal_restored_by_ssh": flags_after == session.saved_terminal_flags(),
            "emergency_restore_used": emergency_restore_used,
            "remote_process_exited": True,
        }
    finally:
        session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", required=True, help="OpenSSH target, e.g. user@host"
    )
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        raise ProbeFailure(f"binary not found: {binary}")

    token = uuid.uuid4().hex[:8]
    process_name = f"ps0-{token}"
    windows_name = f"{process_name}.bin"
    wsl_binary = f"/tmp/{process_name}"
    staged_windows = False
    staged_wsl = False
    wsl_source: str | None = None
    result: dict[str, Any] | None = None
    cleanup_confirmed = False
    try:
        hostname = ssh_command(args.target, "hostname").decode(errors="replace").strip()
        remote_identity = (
            ssh_command(args.target, "whoami").decode(errors="replace").strip()
        )
        profile = (
            ssh_command(args.target, "cmd.exe /d /c echo %USERPROFILE%")
            .decode(errors="replace")
            .strip()
        )
        wsl_profile = windows_path_to_wsl(profile)
        wsl_source = f"{wsl_profile}/{windows_name}"
        wsl_identity = (
            ssh_command(
                args.target,
                'wsl.exe -e sh -lc "uname -srmo; id -un; locale charmap"',
            )
            .decode(errors="replace")
            .splitlines()
        )

        staged_windows = True
        run_command(
            [
                "scp",
                *SSH_OPTIONS,
                str(binary),
                f"{args.target}:{windows_name}",
            ],
            timeout=30.0,
        )
        staged_wsl = True
        ssh_command(args.target, f"wsl.exe -e cp {wsl_source} {wsl_binary}")
        ssh_command(args.target, f"wsl.exe -e chmod 700 {wsl_binary}")
        remote_sha256 = (
            ssh_command(args.target, f"wsl.exe -e sha256sum {wsl_binary}")
            .decode(errors="replace")
            .split()[0]
        )
        local_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
        if remote_sha256 != local_sha256:
            raise ProbeFailure(
                f"remote binary digest mismatch: {remote_sha256} != {local_sha256}"
            )

        launch_command = (
            'wsl.exe -e sh -lc "stty cols 120 rows 32; '
            f"exec env LC_ALL=C.UTF-8 TERM=xterm-256color {wsl_binary}"
            '"'
        )
        interactive = run_interactive(
            target=args.target,
            launch_command=launch_command,
        )
        disconnect = run_abrupt_disconnect(
            target=args.target,
            launch_command=launch_command,
            process_name=process_name,
        )
        reconnect_version = (
            ssh_command(args.target, f"wsl.exe -e {wsl_binary} --version")
            .decode(errors="replace")
            .strip()
        )
        if not reconnect_version.startswith("pulsara-tui-s0 "):
            raise ProbeFailure(f"unexpected reconnect response: {reconnect_version!r}")

        result = {
            "schema_version": "pulsara.terminal.s0.real-ssh-result.v1",
            "status": "pass",
            "transport": "macOS OpenSSH -> Windows OpenSSH/ConPTY -> WSL2 Linux",
            "target": args.target,
            "remote_hostname": hostname,
            "remote_identity": remote_identity,
            "remote_runtime": {
                "uname": wsl_identity[0] if wsl_identity else "unknown",
                "user": wsl_identity[1] if len(wsl_identity) > 1 else "unknown",
                "locale_charmap": (
                    wsl_identity[2] if len(wsl_identity) > 2 else "unknown"
                ),
                "term": "xterm-256color",
            },
            "binary": {
                "target": "linux/amd64",
                "sha256": local_sha256,
                "remote_digest_matched": True,
            },
            "interactive": interactive,
            "abrupt_disconnect": disconnect,
            "reconnect_version": reconnect_version,
        }
    finally:
        if staged_wsl:
            ssh_command(args.target, f"wsl.exe -e pkill -x {process_name}", check=False)
            ssh_command(args.target, f"wsl.exe -e rm -f {wsl_binary}", check=False)
        if staged_windows:
            ssh_command(
                args.target,
                f"cmd.exe /d /c del /q {windows_name}",
                check=False,
            )

        process_absent = not staged_wsl or not remote_process_present(
            args.target, process_name
        )
        wsl_binary_absent = not staged_wsl or not remote_wsl_path_present(
            args.target, wsl_binary
        )
        windows_source_absent = (
            not staged_windows
            or wsl_source is None
            or not remote_wsl_path_present(args.target, wsl_source)
        )
        cleanup_confirmed = (
            process_absent and wsl_binary_absent and windows_source_absent
        )

    if result is None:
        raise ProbeFailure("real SSH probe finished without a result")
    result["remote_staging_cleaned"] = cleanup_confirmed
    if not cleanup_confirmed:
        raise ProbeFailure("remote process or staging file survived cleanup")

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
