#!/usr/bin/env python3
"""Python parent/PTY probe for the disposable Bubble Tea S0 client.

This intentionally uses only the fake protobuf schema in this directory. It
does not import Pulsara Runtime, EventLog, terminal protocol, or secret code.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Any, Callable

SPIKE_ROOT = Path(__file__).resolve().parents[1]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from probe_wire import probe_pb2  # noqa: E402


READY_MARKER = b"Pulsara Bubble Tea S0"
LARGE_PASTE_MARKER = b"large paste: 1048576 bytes"
TERM_RELEVANT_LFLAG = termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN


class ProbeFailure(RuntimeError):
    pass


class ChildSession:
    def __init__(
        self,
        binary: Path,
        *,
        with_stream: bool = True,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self.binary = binary
        self.master_fd, slave_fd = pty.openpty()
        self._saved_termios = termios.tcgetattr(self.master_fd)
        self._set_winsize(120, 32)

        self.stream_read_fd = -1
        self.stream_write_fd = -1
        pass_fds: tuple[int, ...] = ()
        command = [str(binary)]
        if with_stream:
            self.stream_read_fd, self.stream_write_fd = os.pipe()
            command.extend(["--stream-fd", str(self.stream_read_fd)])
            pass_fds = (self.stream_read_fd,)

        metrics_handle = tempfile.NamedTemporaryFile(
            prefix="pulsara-s0-", suffix=".json", delete=False
        )
        metrics_handle.close()
        self.metrics_path = Path(metrics_handle.name)
        command.extend(["--metrics-file", str(self.metrics_path)])
        command.extend(extra_args)

        child_env = dict(os.environ)
        child_env.update({"TERM": "xterm-256color", "LC_CTYPE": "UTF-8"})

        def child_setup() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env,
            close_fds=True,
            pass_fds=pass_fds,
            preexec_fn=child_setup,
        )
        os.close(slave_fd)
        if self.stream_read_fd >= 0:
            os.close(self.stream_read_fd)
            self.stream_read_fd = -1

        self._output = bytearray()
        self._output_lock = threading.Lock()
        self._reader_done = threading.Event()
        self._reader = threading.Thread(
            target=self._read_output, name="s0-pty-reader", daemon=True
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

    def resize(self, width: int, height: int) -> None:
        self._set_winsize(width, height)

    def output_position(self) -> int:
        with self._output_lock:
            return len(self._output)

    def output(self) -> bytes:
        with self._output_lock:
            return bytes(self._output)

    def wait_for(self, needle: bytes, *, timeout: float = 8.0, since: int = 0) -> float:
        started = time.perf_counter()
        deadline = started + timeout
        while time.perf_counter() < deadline:
            with self._output_lock:
                if needle in self._output[since:]:
                    return time.perf_counter() - started
            if self.process.poll() is not None:
                break
            time.sleep(0.002)
        tail = self.output()[-4000:].decode("utf-8", errors="replace")
        raise ProbeFailure(
            f"did not observe {needle!r}; returncode={self.process.poll()} tail={tail!r}"
        )

    def write_input(self, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(self.master_fd, view)
            view = view[written:]

    def send_frame(self, frame: probe_pb2.ProbeFrame) -> None:
        if self.stream_write_fd < 0:
            raise ProbeFailure("session has no protobuf stream")
        payload = frame.SerializeToString()
        packet = struct.pack(">I", len(payload)) + payload
        view = memoryview(packet)
        while view:
            written = os.write(self.stream_write_fd, view)
            view = view[written:]

    def close_stream(self) -> None:
        if self.stream_write_fd >= 0:
            os.close(self.stream_write_fd)
            self.stream_write_fd = -1

    def wait(self, timeout: float = 10.0) -> int:
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

    def metrics(self) -> dict[str, Any]:
        if not self.metrics_path.exists() or self.metrics_path.stat().st_size == 0:
            return {}
        return json.loads(self.metrics_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.close_stream()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5.0)
        self._reader_done.wait(timeout=1.0)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.metrics_path.unlink(missing_ok=True)


def run_concurrent_stream(
    binary: Path, *, rate_hz: int, delta_count: int
) -> dict[str, Any]:
    session = ChildSession(binary)
    try:
        session.wait_for(READY_MARKER)
        session.send_frame(
            probe_pb2.ProbeFrame(
                snapshot=probe_pb2.Snapshot(
                    revision=1,
                    lines=["fake protobuf snapshot", "中文 snapshot 🙂"],
                )
            )
        )

        stop_writer = threading.Event()

        def stream_deltas() -> None:
            for sequence in range(1, delta_count + 1):
                if stop_writer.is_set():
                    return
                session.send_frame(
                    probe_pb2.ProbeFrame(
                        delta=probe_pb2.Delta(
                            sequence=sequence,
                            content=f"fake delta {sequence:04d} 界",
                            sent_unix_nanos=time.time_ns(),
                        )
                    )
                )
                time.sleep(1 / rate_hz)

        writer = threading.Thread(target=stream_deltas, name=f"s0-{rate_hz}hz-writer")
        writer.start()

        key_latencies_ms: list[float] = []
        typed = "持续输入中文🙂ASCII"
        session.write_input(typed.encode("utf-8"))
        for marker in "①②③④⑤⑥⑦⑧⑨⑩":
            start_position = session.output_position()
            started = time.perf_counter()
            session.write_input(marker.encode("utf-8"))
            session.wait_for(marker.encode("utf-8"), timeout=2.0, since=start_position)
            key_latencies_ms.append((time.perf_counter() - started) * 1000)

        session.write_input(b"\x1b[200~small-paste\nline-2\x1b[201~")
        for width, height in ((80, 24), (120, 32), (160, 40), (12, 4), (120, 32)):
            session.resize(width, height)
            time.sleep(0.03)

        large_payload = b"x" * (1024 * 1024)
        started_large_paste = time.perf_counter()
        start_position = session.output_position()
        session.write_input(b"\x1b[200~" + large_payload + b"\x1b[201~")
        session.wait_for(LARGE_PASTE_MARKER, timeout=8.0, since=start_position)
        large_paste_ms = (time.perf_counter() - started_large_paste) * 1000

        writer.join(timeout=8.0)
        if writer.is_alive():
            stop_writer.set()
            raise ProbeFailure(f"{rate_hz}Hz protobuf writer did not drain")
        session.close_stream()
        session.write_input(b"\x11")  # Ctrl-Q, owned by the Go UI only.
        returncode = session.wait()
        metrics = session.metrics()
        if returncode != 0:
            raise ProbeFailure(f"concurrent stream child exited {returncode}")
        if metrics.get("metrics", {}).get("delta_count") != delta_count:
            raise ProbeFailure(f"delta count mismatch: {metrics}")
        draft = metrics.get("metrics", {}).get("draft", "")
        if typed not in draft or "small-paste\nline-2" not in draft:
            raise ProbeFailure(f"draft lost during stream: {draft!r}")
        if len(draft) >= 1024 * 1024:
            raise ProbeFailure("large paste remained resident in textarea")
        if metrics.get("metrics", {}).get("large_paste_bytes") != 1024 * 1024:
            raise ProbeFailure(f"large paste not recorded: {metrics}")

        ordered = sorted(key_latencies_ms)
        p95_index = max((95 * len(ordered) + 99) // 100 - 1, 0)
        return {
            "status": "pass",
            "delta_rate_hz": rate_hz,
            "delta_count": delta_count,
            "keypress_p95_ms": round(ordered[p95_index], 3),
            "keypress_max_ms": round(max(ordered), 3),
            "large_paste_ms": round(large_paste_ms, 3),
            "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
            "go_metrics": metrics,
        }
    finally:
        session.close()


def run_crash_case(
    binary: Path, name: str, action: Callable[[ChildSession], None]
) -> dict[str, Any]:
    operation_ticks = 0
    stop_operation = threading.Event()

    def operation() -> None:
        nonlocal operation_ticks
        while not stop_operation.wait(0.01):
            operation_ticks += 1

    operation_thread = threading.Thread(target=operation, name=f"s0-operation-{name}")
    operation_thread.start()
    session = ChildSession(binary, with_stream=False)
    try:
        session.wait_for(READY_MARKER)
        flags_during = session.terminal_flags()
        ticks_before = operation_ticks
        action(session)
        returncode = session.wait()
        time.sleep(0.08)
        ticks_after = operation_ticks
        flags_after_exit = session.terminal_flags()
        restored_by_child = flags_after_exit == session.saved_terminal_flags()
        emergency_restore_used = False
        if not restored_by_child:
            session.emergency_restore()
            emergency_restore_used = True
        restored_final = session.terminal_flags() == session.saved_terminal_flags()
        if not restored_final:
            raise ProbeFailure(f"{name}: terminal flags were not restored")
        if ticks_after <= ticks_before:
            raise ProbeFailure(f"{name}: child exit cancelled parent operation")
        return {
            "status": "pass",
            "returncode": returncode,
            "terminal_was_raw": flags_during != session.saved_terminal_flags(),
            "restored_by_child": restored_by_child,
            "emergency_restore_used": emergency_restore_used,
            "parent_operation_survived": True,
        }
    finally:
        stop_operation.set()
        operation_thread.join(timeout=1.0)
        session.close()


def run_crash_matrix(binary: Path) -> dict[str, Any]:
    return {
        "normal_quit": run_crash_case(
            binary, "normal_quit", lambda session: session.write_input(b"\x11")
        ),
        "panic": run_crash_case(
            binary, "panic", lambda session: session.write_input(b"\x07")
        ),
        "sigterm": run_crash_case(
            binary,
            "sigterm",
            lambda session: session.process.send_signal(signal.SIGTERM),
        ),
        "parent_kill_child": run_crash_case(
            binary,
            "parent_kill_child",
            lambda session: session.process.send_signal(signal.SIGINT),
        ),
        "sigkill": run_crash_case(
            binary, "sigkill", lambda session: session.process.kill()
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mode", choices=("concurrent", "crash", "all"), default="all")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        raise ProbeFailure(f"binary not found: {binary}")
    result: dict[str, Any] = {
        "schema_version": "pulsara.terminal.s0.probe-result.v1",
        "binary": binary.name,
        "platform": os.uname().sysname + "/" + os.uname().machine,
    }
    if args.mode in {"concurrent", "all"}:
        result["concurrent_stream"] = {
            "20hz": run_concurrent_stream(binary, rate_hz=20, delta_count=60),
            "100hz": run_concurrent_stream(binary, rate_hz=100, delta_count=300),
        }
    if args.mode in {"crash", "all"}:
        result["crash_matrix"] = run_crash_matrix(binary)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
