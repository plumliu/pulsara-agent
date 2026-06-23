"""Managed local terminal processes and yielded process registry."""

from __future__ import annotations

import os
import pty
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import RLock, Thread
from typing import Callable, Mapping
from uuid import uuid4

from pulsara_agent.event import AgentEvent, EventContext, TerminalProcessCompletedEvent
from pulsara_agent.runtime.terminal.env import build_default_subprocess_env
from pulsara_agent.runtime.terminal.models import (
    TerminalBackendType,
    TerminalIOMode,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalResult,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.output import OutputAccumulator
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig, detect_terminal_shell


_TIMEOUT_EXIT_CODE = 124


class TerminalKillReason(StrEnum):
    USER = "user_tool_kill"
    TEARDOWN = "teardown"
    LIFETIME_WATCHDOG = "lifetime_watchdog"


class ProcessLimitError(RuntimeError):
    """Raised when a newly yielded terminal process would exceed the live limit."""


class ProcessInputError(RuntimeError):
    """Raised when stdin cannot be written for a managed process."""


@dataclass(slots=True)
class TerminalProcessState:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: Path
    backend_type: TerminalBackendType
    io_mode: TerminalIOMode
    process: subprocess.Popen[bytes]
    stdin_pipe: bool
    max_output_chars: int
    env_diagnostics: dict[str, object] = field(default_factory=dict)
    capture_cwd_file: Path | None = None
    pty_master_fd: int | None = None
    yielded: bool = False
    status: TerminalStatus = TerminalStatus.RUNNING
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdin_closed: bool = False
    output: OutputAccumulator = field(default_factory=OutputAccumulator)
    output_callback: Callable[[str], None] | None = field(default=None, repr=False)
    shell: TerminalShellConfig = field(default_factory=detect_terminal_shell)
    owner_host_session_id: str | None = None
    owner_conversation_id: str | None = None
    origin_run_id: str | None = None
    origin_turn_id: str | None = None
    origin_reply_id: str | None = None
    origin_tool_call_id: str | None = None
    completion_event_recorded: bool = False
    completion_suppressed: bool = False
    completion_reason: str | None = None
    record_event: Callable[[AgentEvent], AgentEvent] | None = field(default=None, repr=False)
    reader_thread: Thread | None = None
    lifetime_watchdog: Thread | None = None
    lock: RLock = field(default_factory=RLock, repr=False)

    @property
    def is_running(self) -> bool:
        with self.lock:
            return self.status is TerminalStatus.RUNNING

    @property
    def is_finished(self) -> bool:
        with self.lock:
            return self.status is not TerminalStatus.RUNNING


@dataclass(slots=True)
class ProcessRegistry:
    max_live_processes: int = 8
    max_finished_processes: int = 32
    finished_ttl_seconds: float = 3600.0
    _processes: dict[str, TerminalProcessState] = field(default_factory=dict, init=False, repr=False)

    def exec_with_yield(
        self,
        *,
        terminal_session_id: str,
        command: str,
        cwd: Path,
        max_output_chars: int,
        yield_time_ms: int,
        backend_type: TerminalBackendType = TerminalBackendType.LOCAL,
        tty: bool = False,
        max_lifetime_seconds: int | None = None,
        output_callback: Callable[[str], None] | None = None,
        shell: TerminalShellConfig | None = None,
        env: Mapping[str, str] | None = None,
        env_diagnostics: Mapping[str, object] | None = None,
        owner_host_session_id: str | None = None,
        owner_conversation_id: str | None = None,
        origin_event_context: EventContext | None = None,
        origin_tool_call_id: str | None = None,
        record_event: Callable[[AgentEvent], AgentEvent] | None = None,
    ) -> tuple[TerminalProcessState, bool]:
        self._cleanup_finished()
        state = spawn_local_process(
            terminal_session_id=terminal_session_id,
            command=command,
            cwd=cwd,
            max_output_chars=max_output_chars,
            backend_type=backend_type,
            io_mode=TerminalIOMode.PTY if tty else TerminalIOMode.PIPE,
            stdin_pipe=True,
            capture_cwd=True,
            output_callback=output_callback,
            shell=shell,
            env=env,
            env_diagnostics=env_diagnostics,
            owner_host_session_id=owner_host_session_id,
            owner_conversation_id=owner_conversation_id,
            origin_event_context=origin_event_context,
            origin_tool_call_id=origin_tool_call_id,
            record_event=record_event,
        )
        if max_lifetime_seconds is not None:
            state.lifetime_watchdog = _arm_lifetime_watchdog(state, max_lifetime_seconds)
        finished = wait_for_process(
            state,
            timeout_seconds=max(yield_time_ms, 0) / 1000,
            kill_on_timeout=False,
        )
        if finished:
            return state, False
        if self._live_count() >= self.max_live_processes:
            kill_process(state, reason=TerminalKillReason.TEARDOWN)
            _cleanup_cwd_file(state)
            raise ProcessLimitError(f"max live terminal processes reached: {self.max_live_processes}")
        with state.lock:
            state.yielded = True
            state.output_callback = None
        self._processes[state.process_id] = state
        _maybe_record_completion_event(state)
        return state, True

    def poll(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalResult:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        return snapshot_process(state, max_output_chars=max_output_chars)

    def wait(
        self,
        process_id: str,
        *,
        timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalResult:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        wait_for_process(state, timeout_seconds=timeout_seconds, kill_on_timeout=False)
        return snapshot_process(state, max_output_chars=max_output_chars)

    def kill(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalResult:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        kill_process(state, reason=TerminalKillReason.USER)
        return snapshot_process(state, max_output_chars=max_output_chars)

    def write(
        self,
        process_id: str,
        data: str,
        *,
        append_newline: bool = False,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalResult:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        write_process_input(state, data, append_newline=append_newline)
        return snapshot_process(state, max_output_chars=max_output_chars)

    def close_stdin(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalResult:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        close_process_stdin(state)
        return snapshot_process(state, max_output_chars=max_output_chars)

    def shutdown(self) -> None:
        for state in list(self._processes.values()):
            if state.is_running:
                kill_process(state, reason=TerminalKillReason.TEARDOWN)

    def kill_owned(self, owner_host_session_id: str) -> list[TerminalResult]:
        results: list[TerminalResult] = []
        for state in list(self._processes.values()):
            if state.owner_host_session_id != owner_host_session_id:
                continue
            if state.is_running:
                kill_process(state, reason=TerminalKillReason.TEARDOWN)
            results.append(snapshot_process(state))
        return results

    def list_owned(self, owner_host_session_id: str) -> list[TerminalResult]:
        self._cleanup_finished()
        return [
            snapshot_process(state)
            for state in self._processes.values()
            if state.owner_host_session_id == owner_host_session_id
        ]

    def list_processes(
        self,
        *,
        owner_host_session_id: str | None = None,
        include_finished: bool = True,
        include_running: bool = True,
    ) -> list[TerminalProcessInfo]:
        self._cleanup_finished()
        processes = [
            process_info(state)
            for state in self._processes.values()
            if state.yielded
            and (owner_host_session_id is None or state.owner_host_session_id == owner_host_session_id)
            and ((include_running and state.is_running) or (include_finished and state.is_finished))
        ]
        return sorted(
            processes,
            key=lambda info: (
                0 if info.status == TerminalStatus.RUNNING.value else 1,
                -(info.ended_at_monotonic or info.started_at_monotonic),
            ),
        )

    def log(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ) -> TerminalProcessLog:
        state = self._get(process_id, owner_host_session_id=owner_host_session_id)
        return process_log(state, max_output_chars=max_output_chars or state.max_output_chars)

    def live_count(self, *, owner_host_session_id: str | None = None) -> int:
        return sum(
            1
            for state in self._processes.values()
            if state.yielded
            and state.is_running
            and (owner_host_session_id is None or state.owner_host_session_id == owner_host_session_id)
        )

    def finished_count(self, *, owner_host_session_id: str | None = None) -> int:
        return sum(
            1
            for state in self._processes.values()
            if state.yielded
            and state.is_finished
            and (owner_host_session_id is None or state.owner_host_session_id == owner_host_session_id)
        )

    def _get(
        self,
        process_id: str,
        *,
        owner_host_session_id: str | None = None,
    ) -> TerminalProcessState:
        self._cleanup_finished()
        try:
            state = self._processes[process_id]
        except KeyError as exc:
            raise KeyError(f"terminal process not found or expired: {process_id}") from exc
        if owner_host_session_id is not None and state.owner_host_session_id != owner_host_session_id:
            raise KeyError(f"terminal process not found or not owned by this session: {process_id}")
        return state

    def _live_count(self) -> int:
        return sum(1 for state in self._processes.values() if state.yielded and state.is_running)

    def _cleanup_finished(self) -> None:
        now = time.monotonic()
        expired = [
            process_id
            for process_id, state in self._processes.items()
            if state.yielded
            and state.is_finished
            and state.ended_at is not None
            and now - state.ended_at > self.finished_ttl_seconds
        ]
        for process_id in expired:
            self._processes.pop(process_id, None)

        finished = [
            (process_id, state.ended_at or state.started_at)
            for process_id, state in self._processes.items()
            if state.yielded and state.is_finished
        ]
        finished.sort(key=lambda item: item[1])
        while len(finished) > self.max_finished_processes:
            process_id, _ = finished.pop(0)
            self._processes.pop(process_id, None)


def spawn_local_process(
    *,
    terminal_session_id: str,
    command: str,
    cwd: Path,
    max_output_chars: int,
    backend_type: TerminalBackendType = TerminalBackendType.LOCAL,
    io_mode: TerminalIOMode = TerminalIOMode.PIPE,
    stdin_pipe: bool,
    capture_cwd: bool,
    output_callback: Callable[[str], None] | None = None,
    shell: TerminalShellConfig | None = None,
    env: Mapping[str, str] | None = None,
    env_diagnostics: Mapping[str, object] | None = None,
    owner_host_session_id: str | None = None,
    owner_conversation_id: str | None = None,
    origin_event_context: EventContext | None = None,
    origin_tool_call_id: str | None = None,
    record_event: Callable[[AgentEvent], AgentEvent] | None = None,
) -> TerminalProcessState:
    process_id = f"proc_{uuid4().hex}"
    shell = shell or detect_terminal_shell()
    subprocess_env = dict(env) if env is not None else build_default_subprocess_env()
    cwd_file = _new_cwd_file() if capture_cwd else None
    wrapped = _wrap_command(command, cwd=cwd, cwd_file=cwd_file) if capture_cwd else command
    pty_master_fd: int | None = None
    if io_mode is TerminalIOMode.PTY:
        pty_master_fd, pty_slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                shell.argv(wrapped),
                cwd=str(cwd),
                stdin=pty_slave_fd,
                stdout=pty_slave_fd,
                stderr=pty_slave_fd,
                preexec_fn=os.setsid,
                close_fds=True,
                env=subprocess_env,
            )
        finally:
            os.close(pty_slave_fd)
    else:
        proc = subprocess.Popen(
            shell.argv(wrapped),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
            preexec_fn=os.setsid,
            env=subprocess_env,
        )
    state = TerminalProcessState(
        process_id=process_id,
        terminal_session_id=terminal_session_id,
        command=command,
        cwd=cwd,
        backend_type=backend_type,
        io_mode=io_mode,
        process=proc,
        stdin_pipe=stdin_pipe,
        max_output_chars=max_output_chars,
        env_diagnostics=dict(env_diagnostics or {}),
        capture_cwd_file=cwd_file,
        pty_master_fd=pty_master_fd,
        output_callback=output_callback,
        shell=shell,
        owner_host_session_id=owner_host_session_id,
        owner_conversation_id=owner_conversation_id,
        origin_run_id=origin_event_context.run_id if origin_event_context is not None else None,
        origin_turn_id=origin_event_context.turn_id if origin_event_context is not None else None,
        origin_reply_id=origin_event_context.reply_id if origin_event_context is not None else None,
        origin_tool_call_id=origin_tool_call_id,
        record_event=record_event,
        output=OutputAccumulator(),
    )
    reader = Thread(target=_reader_loop, args=(state,), daemon=True, name=f"pulsara-terminal-{state.process_id}")
    state.reader_thread = reader
    reader.start()
    return state


def wait_for_process(
    state: TerminalProcessState,
    *,
    timeout_seconds: int | None,
    kill_on_timeout: bool,
) -> bool:
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        if state.is_finished:
            _join_reader(state)
            return True
        if deadline is not None and time.monotonic() >= deadline:
            if kill_on_timeout:
                _mark_timed_out(state)
                _terminate_process_group(state.process)
                _join_reader(state)
            return False
        time.sleep(0.02)


def kill_process(
    state: TerminalProcessState,
    *,
    reason: TerminalKillReason | str = TerminalKillReason.USER,
) -> None:
    if state.is_finished:
        return
    _mark_kill_reason(state, reason)
    _mark_status(state, TerminalStatus.KILLED, exit_code=-signal.SIGTERM)
    _terminate_process_group(state.process)
    _join_reader(state)


def write_process_input(
    state: TerminalProcessState,
    data: str,
    *,
    append_newline: bool,
) -> None:
    with state.lock:
        if state.status is not TerminalStatus.RUNNING:
            raise ProcessInputError("cannot write to a finished terminal process")
        if state.stdin_closed:
            raise ProcessInputError("terminal process stdin is closed")
        if state.io_mode is TerminalIOMode.PTY:
            if state.pty_master_fd is None:
                state.stdin_closed = True
                raise ProcessInputError("terminal process PTY is closed")
            stdin = None
            pty_master_fd = state.pty_master_fd
        else:
            stdin = state.process.stdin
            pty_master_fd = None
            if stdin is None:
                state.stdin_closed = True
                raise ProcessInputError("terminal process stdin is closed")
    payload = data + ("\n" if append_newline else "")
    try:
        if state.io_mode is TerminalIOMode.PTY:
            assert pty_master_fd is not None
            os.write(pty_master_fd, payload.encode("utf-8"))
        else:
            assert stdin is not None
            stdin.write(payload.encode("utf-8"))
            stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        with state.lock:
            state.stdin_closed = True
        raise ProcessInputError("terminal process stdin is closed") from exc


def close_process_stdin(state: TerminalProcessState) -> None:
    with state.lock:
        if state.status is not TerminalStatus.RUNNING:
            raise ProcessInputError("cannot close stdin for a finished terminal process")
        if state.stdin_closed:
            raise ProcessInputError("terminal process stdin is already closed")
        stdin = state.process.stdin
        if state.io_mode is TerminalIOMode.PTY:
            stdin = None
            if state.pty_master_fd is None:
                state.stdin_closed = True
                raise ProcessInputError("terminal process PTY is closed")
        elif stdin is None:
            state.stdin_closed = True
            raise ProcessInputError("terminal process stdin is closed")
        state.stdin_closed = True
    try:
        if state.io_mode is TerminalIOMode.PTY:
            assert state.pty_master_fd is not None
            os.write(state.pty_master_fd, b"\x04")
        else:
            assert stdin is not None
            stdin.close()
    except OSError as exc:
        raise ProcessInputError("terminal process stdin is closed") from exc


def snapshot_process(
    state: TerminalProcessState,
    *,
    max_output_chars: int | None = None,
    cwd: Path | None = None,
) -> TerminalResult:
    with state.lock:
        status = state.status
        exit_code = state.exit_code
        timed_out = state.timed_out
        process_id = state.process_id if state.yielded else None
        result_cwd = cwd or state.cwd
        duration_seconds = _duration_seconds_locked(state)
        stdin_closed = state.stdin_closed
    processed = state.output.snapshot(max_chars=max_output_chars or state.max_output_chars)
    full_output_text = state.output.full_text()
    return TerminalResult(
        status=status,
        output=processed.text,
        exit_code=exit_code if exit_code is not None else -1,
        cwd=str(result_cwd),
        timed_out=timed_out,
        truncated=processed.truncated,
        process_id=process_id,
        full_output_text=full_output_text,
        metadata={
            "command": state.command,
            "backend_type": state.backend_type.value,
            "io_mode": state.io_mode.value,
            "process_id": process_id,
            "terminal_session_id": state.terminal_session_id,
            "duration_seconds": duration_seconds,
            "stdin_closed": stdin_closed,
            "shell": state.shell.to_metadata(),
            "env": dict(state.env_diagnostics),
            "owner_host_session_id": state.owner_host_session_id,
            "owner_conversation_id": state.owner_conversation_id,
        },
    )


def process_info(state: TerminalProcessState) -> TerminalProcessInfo:
    with state.lock:
        return TerminalProcessInfo(
            process_id=state.process_id,
            terminal_session_id=state.terminal_session_id,
            command=state.command,
            cwd=str(state.cwd),
            backend_type=state.backend_type.value,
            io_mode=state.io_mode.value,
            status=state.status.value,
            exit_code=state.exit_code,
            timed_out=state.timed_out,
            stdin_closed=state.stdin_closed,
            started_at_monotonic=state.started_at,
            ended_at_monotonic=state.ended_at,
            duration_seconds=_duration_seconds_locked(state),
            owner_host_session_id=state.owner_host_session_id,
            owner_conversation_id=state.owner_conversation_id,
        )


def process_log(state: TerminalProcessState, *, max_output_chars: int) -> TerminalProcessLog:
    info = process_info(state)
    processed = state.output.snapshot(max_chars=max_output_chars)
    return TerminalProcessLog(
        process=info,
        output=processed.text,
        truncated=processed.truncated,
        full_output_text=state.output.full_text(),
    )


def read_captured_cwd(state: TerminalProcessState) -> Path | None:
    cwd_file = state.capture_cwd_file
    if cwd_file is None:
        return None
    try:
        raw = cwd_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    finally:
        try:
            cwd_file.unlink(missing_ok=True)
        except OSError:
            pass
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _reader_loop(state: TerminalProcessState) -> None:
    try:
        fd = _reader_fd(state)
        if fd is not None:
            while True:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                flushed = state.output.append(chunk)
                if flushed:
                    _emit_output_delta(state, flushed)
    finally:
        flushed = state.output.finish()
        if flushed:
            _emit_output_delta(state, flushed)
        try:
            exit_code = state.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_process_group(state.process)
            exit_code = state.process.wait(timeout=1)
        except Exception:
            exit_code = state.process.returncode
        with state.lock:
            if state.status is TerminalStatus.RUNNING:
                state.status = TerminalStatus.SUCCESS if exit_code == 0 else TerminalStatus.ERROR
                state.exit_code = exit_code if exit_code is not None else -1
                state.ended_at = time.monotonic()
            elif state.exit_code is None and exit_code is not None:
                state.exit_code = exit_code
                state.ended_at = state.ended_at or time.monotonic()
        if state.process.stdout is not None:
            try:
                state.process.stdout.close()
            except Exception:
                pass
        if state.pty_master_fd is not None:
            try:
                os.close(state.pty_master_fd)
            except OSError:
                pass
            state.pty_master_fd = None
        if state.yielded:
            _cleanup_cwd_file(state)
        _maybe_record_completion_event(state)


def _reader_fd(state: TerminalProcessState) -> int | None:
    if state.io_mode is TerminalIOMode.PTY:
        return state.pty_master_fd
    stdout = state.process.stdout
    return stdout.fileno() if stdout is not None else None


def _emit_output_delta(state: TerminalProcessState, delta: str) -> None:
    with state.lock:
        callback = state.output_callback
    if callback is None:
        return
    try:
        callback(delta)
    except Exception:
        pass


def _mark_timed_out(state: TerminalProcessState) -> None:
    _mark_status(state, TerminalStatus.TIMEOUT, exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)


def _mark_status(
    state: TerminalProcessState,
    status: TerminalStatus,
    *,
    exit_code: int,
    timed_out: bool = False,
) -> None:
    with state.lock:
        if state.status is not TerminalStatus.RUNNING:
            return
        state.status = status
        state.exit_code = exit_code
        state.timed_out = timed_out
        state.ended_at = time.monotonic()


def _mark_kill_reason(state: TerminalProcessState, reason: TerminalKillReason | str) -> None:
    reason_value = str(reason)
    with state.lock:
        state.completion_reason = reason_value
        if reason_value in {TerminalKillReason.TEARDOWN.value, TerminalKillReason.LIFETIME_WATCHDOG.value}:
            state.completion_suppressed = True
            state.record_event = None


def _duration_seconds_locked(state: TerminalProcessState) -> float:
    end = state.ended_at if state.ended_at is not None else time.monotonic()
    return max(0.0, end - state.started_at)


def _maybe_record_completion_event(state: TerminalProcessState) -> AgentEvent | None:
    event_data = _completion_event_data(state)
    if event_data is None:
        return None
    record_event, fields = event_data
    event = TerminalProcessCompletedEvent(**fields)
    try:
        return record_event(event)
    except Exception:
        return None


def _completion_event_data(
    state: TerminalProcessState,
) -> tuple[Callable[[AgentEvent], AgentEvent], dict[str, object]] | None:
    with state.lock:
        if not state.yielded:
            return None
        if state.status is TerminalStatus.RUNNING:
            return None
        if state.completion_suppressed:
            return None
        if state.completion_event_recorded:
            return None
        if (
            state.record_event is None
            or state.origin_run_id is None
            or state.origin_turn_id is None
            or state.origin_reply_id is None
        ):
            return None
        state.completion_event_recorded = True
        record_event = state.record_event
        fields: dict[str, object] = {
            "run_id": state.origin_run_id,
            "turn_id": state.origin_turn_id,
            "reply_id": state.origin_reply_id,
            "process_id": state.process_id,
            "terminal_session_id": state.terminal_session_id,
            "command": state.command,
            "status": state.status.value,
            "exit_code": state.exit_code if state.exit_code is not None else -1,
            "cwd": str(state.cwd),
            "timed_out": state.timed_out,
            "duration_seconds": _duration_seconds_locked(state),
            "backend_type": state.backend_type.value,
            "io_mode": state.io_mode.value,
            "tool_call_id": state.origin_tool_call_id,
            "metadata": {"completion_reason": state.completion_reason},
        }
    processed = state.output.snapshot(max_chars=2000)
    fields["output_preview"] = processed.text
    fields["output_truncated"] = processed.truncated
    return record_event, fields


def _join_reader(state: TerminalProcessState) -> None:
    if state.reader_thread is not None:
        state.reader_thread.join(timeout=2)


def _arm_lifetime_watchdog(state: TerminalProcessState, seconds: int) -> Thread:
    def _watch() -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if state.is_finished:
                return
            time.sleep(0.1)
        if state.is_running:
            kill_process(state, reason=TerminalKillReason.LIFETIME_WATCHDOG)

    watcher = Thread(
        target=_watch,
        daemon=True,
        name=f"pulsara-terminal-lifetime-{state.process_id}",
    )
    watcher.start()
    return watcher


def _cleanup_cwd_file(state: TerminalProcessState) -> None:
    cwd_file = state.capture_cwd_file
    if cwd_file is None:
        return
    try:
        cwd_file.unlink(missing_ok=True)
    except OSError:
        pass


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _wrap_command(command: str, *, cwd: Path, cwd_file: Path | None) -> str:
    quoted_cwd = shlex.quote(str(cwd))
    escaped = command.replace("'", "'\\''")
    lines = [
        f"cd -- {quoted_cwd} || exit 126",
        f"eval '{escaped}'",
        "__pulsara_ec=$?",
    ]
    if cwd_file is not None:
        lines.append(f"pwd -P > {shlex.quote(str(cwd_file))} 2>/dev/null || true")
    lines.append("exit $__pulsara_ec")
    return "\n".join(lines)


def _new_cwd_file() -> Path:
    return Path(tempfile.gettempdir()) / f"pulsara-terminal-cwd-{uuid4().hex}.txt"
