"""Minimal Host-owned terminal session and process registry."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import os
from pathlib import Path
import pty
import re
import signal
import subprocess
from threading import RLock, Thread, Timer
from time import monotonic
from typing import IO
from uuid import uuid4

from pulsara_agent.terminal_process.models import (
    TerminalIOMode,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
    ToolOutputSourceFormatHint,
)


DEFAULT_TERMINAL_SESSION_ID = "default"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=.*$"
)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+")
_TIMEOUT_EXIT_CODE = 124


class ProcessLimitError(RuntimeError):
    pass


class ProcessInputError(RuntimeError):
    pass


def _public_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1=<redacted>", text)
    return _BEARER_RE.sub("Bearer <redacted>", text)


@dataclass(slots=True)
class _BoundedOutput:
    maximum_bytes: int = 8 * 1024 * 1024
    _chunks: list[bytes] = field(default_factory=list)
    _bytes: int = 0
    _truncated: bool = False
    _lock: RLock = field(default_factory=RLock)

    def append(self, value: bytes) -> None:
        if not value:
            return
        with self._lock:
            self._chunks.append(bytes(value))
            self._bytes += len(value)
            while self._bytes > self.maximum_bytes and self._chunks:
                removed = self._chunks.pop(0)
                self._bytes -= len(removed)
                self._truncated = True

    def snapshot(self, maximum_chars: int) -> tuple[str, bool]:
        with self._lock:
            public = _public_output(b"".join(self._chunks))
            truncated = self._truncated or len(public) > maximum_chars
        visible = public[-maximum_chars:] if len(public) > maximum_chars else public
        return visible, truncated

    def artifact_candidate(self) -> ToolOutputArtifactCandidate:
        """Freeze the complete currently retained sanitized observation."""

        with self._lock:
            public = _public_output(b"".join(self._chunks))
            retention_gap = self._truncated
        encoded_size = len(public.encode("utf-8"))
        return ToolOutputArtifactCandidate(
            role="OUTPUT",
            text=public,
            source_coverage=(
                ToolOutputSourceCoverage.RETAINED_SNAPSHOT
                if retention_gap
                else ToolOutputSourceCoverage.COMPLETE
            ),
            source_coverage_reason=(
                ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
                if retention_gap
                else None
            ),
            original_utf8_bytes=None if retention_gap else encoded_size,
            source_format_hint=ToolOutputSourceFormatHint.JSON,
        )


@dataclass(slots=True)
class _ProcessState:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: Path
    owner_host_session_id: str
    io_mode: TerminalIOMode
    process: subprocess.Popen[bytes]
    output: _BoundedOutput
    stdin: IO[bytes] | None
    reader: Thread
    master_fd: int | None
    started_at: float
    deadline_timer: Timer | None = None
    timed_out: bool = False
    killed: bool = False
    stdin_closed: bool = False
    ended_at: float | None = None
    lock: RLock = field(default_factory=RLock)

    def refresh(self) -> None:
        code = self.process.poll()
        if code is not None and self.ended_at is None:
            self.ended_at = monotonic()


class ProcessRegistry:
    def __init__(
        self,
        *,
        max_live_processes: int = 8,
        max_finished_processes: int = 32,
        finished_ttl_seconds: float = 3600.0,
    ) -> None:
        self.max_live_processes = max_live_processes
        self.max_finished_processes = max_finished_processes
        self.finished_ttl_seconds = finished_ttl_seconds
        self._states: dict[str, _ProcessState] = {}
        self._released_owners: set[str] = set()
        self._closed = False
        self._lock = RLock()

    def activate_owner(self, owner: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal process registry is closed")
            self._released_owners.discard(owner)

    def exec_with_yield(
        self,
        *,
        terminal_session_id: str,
        command: str,
        cwd: Path,
        max_output_chars: int,
        yield_time_ms: int,
        tty: bool,
        max_lifetime_seconds: int | None,
        owner_host_session_id: str,
        env: dict[str, str],
    ) -> tuple[_ProcessState, bool]:
        del max_output_chars
        with self._lock:
            if self._closed or owner_host_session_id in self._released_owners:
                raise RuntimeError("terminal process owner is closed")
            self._prune_locked()
            if (
                sum(state.process.poll() is None for state in self._states.values())
                >= self.max_live_processes
            ):
                raise ProcessLimitError(
                    f"terminal process limit reached: max {self.max_live_processes}"
                )
        state = self._spawn(
            terminal_session_id=terminal_session_id,
            command=command,
            cwd=cwd,
            tty=tty,
            owner_host_session_id=owner_host_session_id,
            env=env,
        )
        with self._lock:
            if self._closed or owner_host_session_id in self._released_owners:
                _terminate_process_group(state)
                raise RuntimeError("terminal process owner closed during launch")
            self._states[state.process_id] = state
        if max_lifetime_seconds is not None:
            timer = Timer(max_lifetime_seconds, self._expire, args=(state.process_id,))
            timer.daemon = True
            state.deadline_timer = timer
            timer.start()
        if yield_time_ms > 0:
            try:
                state.process.wait(timeout=yield_time_ms / 1000)
            except subprocess.TimeoutExpired:
                pass
        state.refresh()
        return state, state.process.poll() is None

    def _spawn(
        self,
        *,
        terminal_session_id: str,
        command: str,
        cwd: Path,
        tty: bool,
        owner_host_session_id: str,
        env: dict[str, str],
    ) -> _ProcessState:
        process_id = f"proc_{uuid4().hex}"
        shell = os.environ.get("SHELL") or "/bin/sh"
        output = _BoundedOutput()
        master_fd: int | None = None
        if tty:
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    [shell, "-c", command],
                    cwd=cwd,
                    env=env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                os.close(slave_fd)
            stdin: IO[bytes] | None = os.fdopen(os.dup(master_fd), "wb", buffering=0)
            reader = Thread(
                target=_read_fd,
                args=(master_fd, output),
                name=f"terminal-reader:{process_id}",
                daemon=True,
            )
            mode = TerminalIOMode.PTY
        else:
            process = subprocess.Popen(
                [shell, "-c", command],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            assert process.stdout is not None
            stdin = process.stdin
            reader = Thread(
                target=_read_stream,
                args=(process.stdout, output),
                name=f"terminal-reader:{process_id}",
                daemon=True,
            )
            mode = TerminalIOMode.PIPE
        state = _ProcessState(
            process_id=process_id,
            terminal_session_id=terminal_session_id,
            command=command,
            cwd=cwd,
            owner_host_session_id=owner_host_session_id,
            io_mode=mode,
            process=process,
            output=output,
            stdin=stdin,
            reader=reader,
            master_fd=master_fd,
            started_at=monotonic(),
        )
        reader.start()
        return state

    def _owned(self, process_id: str, owner: str) -> _ProcessState:
        with self._lock:
            state = self._states.get(process_id)
        if state is None or state.owner_host_session_id != owner:
            raise KeyError(process_id)
        state.refresh()
        return state

    def poll(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalResult:
        return _snapshot(
            self._owned(process_id, owner_host_session_id), max_output_chars
        )

    def wait(
        self,
        process_id: str,
        *,
        timeout_seconds: int | None,
        max_output_chars: int,
        owner_host_session_id: str,
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        try:
            state.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            pass
        state.refresh()
        return _snapshot(state, max_output_chars)

    def write(
        self,
        process_id: str,
        data: str,
        *,
        append_newline: bool,
        max_output_chars: int,
        owner_host_session_id: str,
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        if (
            state.process.poll() is not None
            or state.stdin is None
            or state.stdin_closed
        ):
            raise ProcessInputError("terminal process stdin is closed")
        payload = (data + ("\n" if append_newline else "")).encode()
        try:
            state.stdin.write(payload)
            state.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ProcessInputError("terminal process stdin write failed") from exc
        return _snapshot(state, max_output_chars)

    def close_stdin(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        with state.lock:
            if not state.stdin_closed and state.stdin is not None:
                try:
                    state.stdin.close()
                except OSError:
                    pass
                state.stdin_closed = True
        return _snapshot(state, max_output_chars)

    def kill(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        state.killed = True
        _terminate_process_group(state)
        _join_physical(state, timeout=2.0)
        return _snapshot(state, max_output_chars)

    def list_processes(
        self,
        *,
        owner_host_session_id: str,
        include_finished: bool,
        include_running: bool,
    ) -> list[TerminalProcessInfo]:
        with self._lock:
            states = [
                state
                for state in self._states.values()
                if state.owner_host_session_id == owner_host_session_id
            ]
        result: list[TerminalProcessInfo] = []
        for state in states:
            state.refresh()
            running = state.process.poll() is None
            if (running and include_running) or (not running and include_finished):
                result.append(_info(state))
        return sorted(result, key=lambda item: item.started_at_monotonic)

    def log(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalProcessLog:
        state = self._owned(process_id, owner_host_session_id)
        output, truncated = state.output.snapshot(max_output_chars)
        return TerminalProcessLog(
            _info(state),
            output,
            truncated,
            state.output.artifact_candidate(),
        )

    def live_count(self, *, owner_host_session_id: str) -> int:
        return sum(
            item.status == TerminalStatus.RUNNING.value
            for item in self.list_processes(
                owner_host_session_id=owner_host_session_id,
                include_finished=False,
                include_running=True,
            )
        )

    def release_owner(
        self, owner: str, *, timeout_seconds: float
    ) -> list[TerminalResult]:
        if timeout_seconds <= 0:
            raise ValueError("terminal close timeout must be positive")
        with self._lock:
            self._released_owners.add(owner)
            states = [
                state
                for state in self._states.values()
                if state.owner_host_session_id == owner
            ]
        for state in states:
            state.killed = state.process.poll() is None
            _terminate_process_group(state)
        deadline = monotonic() + timeout_seconds
        results: list[TerminalResult] = []
        for state in states:
            remaining = deadline - monotonic()
            if remaining <= 0 or not _join_physical(state, timeout=remaining):
                raise TimeoutError("terminal owner physical close did not finish")
            results.append(_snapshot(state, 32_000))
        with self._lock:
            for state in states:
                self._states.pop(state.process_id, None)
        return results

    def shutdown(self) -> None:
        with self._lock:
            owners = sorted(
                {state.owner_host_session_id for state in self._states.values()}
            )
            self._closed = True
        for owner in owners:
            self.release_owner(owner, timeout_seconds=5.0)

    def _expire(self, process_id: str) -> None:
        with self._lock:
            state = self._states.get(process_id)
        if state is None or state.process.poll() is not None:
            return
        state.timed_out = True
        _terminate_process_group(state)

    def _prune_locked(self) -> None:
        now = monotonic()
        finished = []
        for process_id, state in self._states.items():
            state.refresh()
            if state.ended_at is not None:
                finished.append((state.ended_at, process_id, state))
        for ended, process_id, state in sorted(finished):
            if now - ended > self.finished_ttl_seconds:
                _join_physical(state, timeout=0.1)
                self._states.pop(process_id, None)
        remaining = sorted(
            (
                (state.ended_at or now, process_id, state)
                for process_id, state in self._states.items()
                if state.process.poll() is not None
            )
        )
        for _ended, process_id, state in remaining[: -self.max_finished_processes]:
            _join_physical(state, timeout=0.1)
            self._states.pop(process_id, None)


@dataclass(slots=True)
class TerminalSession:
    state: TerminalSessionState
    registry: ProcessRegistry

    def execute(self, request: TerminalRequest) -> TerminalResult:
        cwd = _resolve_workdir(
            request.workdir,
            current=self.state.current_cwd,
            workspace=self.state.workspace_root,
        )
        env = _subprocess_environment(cwd, self.state.workspace_root)
        try:
            process, yielded = self.registry.exec_with_yield(
                terminal_session_id=self.state.session_id,
                command=request.command,
                cwd=cwd,
                max_output_chars=request.max_output_chars,
                yield_time_ms=request.yield_time_ms,
                tty=request.tty,
                max_lifetime_seconds=request.max_lifetime_seconds,
                owner_host_session_id=self.state.owner_host_session_id,
                env=env,
            )
        except ProcessLimitError as exc:
            return TerminalResult(
                status=TerminalStatus.BLOCKED,
                output="",
                exit_code=-1,
                cwd=str(cwd),
                error=str(exc),
            )
        result = _snapshot(process, request.max_output_chars)
        if not yielded:
            self.state.current_cwd = cwd
        return result


@dataclass(slots=True)
class TerminalSessionManager:
    workspace_root: Path
    max_sessions: int = 4
    max_live_processes: int = 8
    max_finished_processes: int = 32
    finished_ttl_seconds: float = 3600.0
    _sessions: dict[tuple[str, str], TerminalSession] = field(
        default_factory=dict, init=False
    )
    _released_owners: set[str] = field(default_factory=set, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)
    process_registry: ProcessRegistry = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.process_registry = ProcessRegistry(
            max_live_processes=self.max_live_processes,
            max_finished_processes=self.max_finished_processes,
            finished_ttl_seconds=self.finished_ttl_seconds,
        )

    def activate_owner(self, owner_host_session_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal manager is closed")
            self._released_owners.discard(owner_host_session_id)
        self.process_registry.activate_owner(owner_host_session_id)

    def get_or_create(
        self, session_id: str | None = None, *, owner_host_session_id: str
    ) -> TerminalSession:
        normalized = session_id or DEFAULT_TERMINAL_SESSION_ID
        if not _SESSION_ID_RE.fullmatch(normalized):
            raise ValueError("terminal session id is invalid")
        key = (owner_host_session_id, normalized)
        with self._lock:
            if self._closed or owner_host_session_id in self._released_owners:
                raise RuntimeError("terminal owner is closed")
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            if len(self._sessions) >= self.max_sessions:
                raise ValueError(
                    f"terminal session limit reached: max {self.max_sessions}"
                )
            session = TerminalSession(
                TerminalSessionState(
                    session_id=normalized,
                    workspace_root=self.workspace_root,
                    current_cwd=self.workspace_root,
                    owner_host_session_id=owner_host_session_id,
                ),
                self.process_registry,
            )
            self._sessions[key] = session
            return session

    def list_processes(self, **kwargs):
        return self.process_registry.list_processes(**kwargs)

    def log_process(self, process_id: str, **kwargs):
        return self.process_registry.log(process_id, **kwargs)

    def poll_process(self, process_id: str, **kwargs):
        return self.process_registry.poll(process_id, **kwargs)

    def wait_process(self, process_id: str, **kwargs):
        return self.process_registry.wait(process_id, **kwargs)

    def write_process(self, process_id: str, data: str, **kwargs):
        return self.process_registry.write(process_id, data, **kwargs)

    def close_process_stdin(self, process_id: str, **kwargs):
        return self.process_registry.close_stdin(process_id, **kwargs)

    def kill_process(self, process_id: str, **kwargs):
        return self.process_registry.kill(process_id, **kwargs)

    def live_process_count(self, *, owner_host_session_id: str) -> int:
        return self.process_registry.live_count(
            owner_host_session_id=owner_host_session_id
        )

    def release_owner(
        self,
        owner_host_session_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> list[TerminalResult]:
        results = self.process_registry.release_owner(
            owner_host_session_id, timeout_seconds=timeout_seconds
        )
        with self._lock:
            self._released_owners.add(owner_host_session_id)
            for key in [
                key for key in self._sessions if key[0] == owner_host_session_id
            ]:
                self._sessions.pop(key, None)
        return results


def _subprocess_environment(cwd: Path, workspace: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not re.search(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD)$", key)
    }
    entries: list[str] = []
    current = cwd
    while True:
        candidate = current / ".venv" / "bin"
        if candidate.is_dir():
            entries.append(str(candidate))
            break
        if current == workspace or workspace not in current.parents:
            break
        current = current.parent
    entries.extend(item for item in env.get("PATH", "").split(os.pathsep) if item)
    env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
    return env


def _resolve_workdir(raw: str | None, *, current: Path, workspace: Path) -> Path:
    candidate = current if not raw else Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = current / candidate
    resolved = candidate.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError("terminal workdir must remain inside workspace")
    if not resolved.is_dir():
        raise ValueError("terminal workdir does not exist")
    return resolved


def _read_stream(stream: IO[bytes], output: _BoundedOutput) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            output.append(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _read_fd(fd: int, output: _BoundedOutput) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    del decoder  # bytes are decoded once by the public snapshot owner
    try:
        while True:
            try:
                chunk = os.read(fd, 64 * 1024)
            except OSError:
                break
            if not chunk:
                break
            output.append(chunk)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _terminate_process_group(state: _ProcessState) -> None:
    if state.process.poll() is not None:
        state.refresh()
        return
    try:
        os.killpg(state.process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        state.process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(state.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _join_physical(state: _ProcessState, *, timeout: float) -> bool:
    deadline = monotonic() + max(0.0, timeout)
    try:
        state.process.wait(timeout=max(0.0, deadline - monotonic()))
    except subprocess.TimeoutExpired:
        return False
    state.reader.join(timeout=max(0.0, deadline - monotonic()))
    if state.reader.is_alive():
        return False
    if state.deadline_timer is not None:
        state.deadline_timer.cancel()
    if state.stdin is not None and not state.stdin_closed:
        try:
            state.stdin.close()
        except OSError:
            pass
        state.stdin_closed = True
    state.refresh()
    return True


def _status(state: _ProcessState) -> TerminalStatus:
    code = state.process.poll()
    if code is None:
        return TerminalStatus.RUNNING
    if state.timed_out:
        return TerminalStatus.TIMEOUT
    if state.killed:
        return TerminalStatus.KILLED
    return TerminalStatus.SUCCESS if code == 0 else TerminalStatus.ERROR


def _info(state: _ProcessState) -> TerminalProcessInfo:
    state.refresh()
    ended = state.ended_at
    return TerminalProcessInfo(
        process_id=state.process_id,
        terminal_session_id=state.terminal_session_id,
        command=state.command,
        cwd=str(state.cwd),
        backend_type="local",
        io_mode=state.io_mode.value,
        status=_status(state).value,
        exit_code=state.process.poll(),
        timed_out=state.timed_out,
        stdin_closed=state.stdin_closed,
        started_at_monotonic=state.started_at,
        ended_at_monotonic=ended,
        duration_seconds=(ended or monotonic()) - state.started_at,
        owner_host_session_id=state.owner_host_session_id,
    )


def _snapshot(state: _ProcessState, maximum_chars: int) -> TerminalResult:
    state.refresh()
    output, truncated = state.output.snapshot(maximum_chars)
    status = _status(state)
    code = state.process.poll()
    return TerminalResult(
        status=status,
        output=output,
        exit_code=(
            _TIMEOUT_EXIT_CODE if state.timed_out else code if code is not None else -1
        ),
        cwd=str(state.cwd),
        timed_out=state.timed_out,
        truncated=truncated,
        error=None
        if status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
        else status.value,
        process_id=state.process_id,
        output_artifact_candidate=state.output.artifact_candidate(),
    )
