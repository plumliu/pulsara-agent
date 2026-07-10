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
from threading import RLock, Thread, Timer
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
_COMPLETION_RECORD_RETRY_DELAYS_SECONDS = (0.05, 0.2, 1.0)


class TerminalKillReason(StrEnum):
    USER = "user_tool_kill"
    TEARDOWN = "teardown"
    LIFETIME_WATCHDOG = "lifetime_watchdog"


class TerminalCompletionRecordState(StrEnum):
    PENDING = "pending"
    RECORDING = "recording"
    RECORDED = "recorded"


class ProcessLimitError(RuntimeError):
    """Raised when a newly yielded terminal process would exceed the live limit."""


class ProcessInputError(RuntimeError):
    """Raised when stdin cannot be written for a managed process."""


class PendingTerminalCompletionError(RuntimeError):
    """Owner release could not durably drain its completion facts."""

    def __init__(self, *, owner_host_session_id: str, pending_count: int) -> None:
        self.owner_host_session_id = owner_host_session_id
        self.pending_count = pending_count
        super().__init__(
            f"terminal owner {owner_host_session_id!r} still has "
            f"{pending_count} pending completion record(s)"
        )


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
    completion_record_state: TerminalCompletionRecordState = (
        TerminalCompletionRecordState.PENDING
    )
    completion_event_id: str = field(default_factory=lambda: uuid4().hex)
    completion_event_candidate: TerminalProcessCompletedEvent | None = field(
        default=None,
        repr=False,
    )
    completion_record_attempts: int = 0
    completion_retry_timer: Timer | None = field(default=None, repr=False)
    completion_suppressed: bool = False
    completion_reason: TerminalKillReason | None = None
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

    @property
    def completion_event_recorded(self) -> bool:
        with self.lock:
            return self.completion_record_state is TerminalCompletionRecordState.RECORDED


@dataclass(slots=True)
class ProcessRegistry:
    max_live_processes: int = 8
    max_finished_processes: int = 32
    max_pending_completion_records: int = 8
    finished_ttl_seconds: float = 3600.0
    _processes: dict[str, TerminalProcessState] = field(default_factory=dict, init=False, repr=False)
    _released_owners: set[str] = field(default_factory=set, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def activate_owner(self, owner_host_session_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal process registry is closed")
            self._released_owners.discard(owner_host_session_id)

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
        with self._lock:
            self._require_accepting_locked(owner_host_session_id)
            self._cleanup_finished_locked()
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
        over_limit = False
        pending_completion_over_limit = False
        released = False
        with self._lock:
            if not self._is_accepting_locked(owner_host_session_id):
                released = True
            elif self._live_count_locked() >= self.max_live_processes:
                over_limit = True
            elif (
                _completion_record_contract_present(state)
                and self._pending_completion_count_locked()
                >= self.max_pending_completion_records
            ):
                pending_completion_over_limit = True
            else:
                with state.lock:
                    state.yielded = True
                    state.output_callback = None
                self._processes[state.process_id] = state
        if released or over_limit or pending_completion_over_limit:
            kill_process(state, reason=TerminalKillReason.TEARDOWN)
            _cleanup_cwd_file(state)
            if released:
                raise ProcessLimitError("terminal owner was released while command was running")
            if pending_completion_over_limit:
                raise ProcessLimitError(
                    "max pending terminal completion records reached: "
                    f"{self.max_pending_completion_records}"
                )
            raise ProcessLimitError(f"max live terminal processes reached: {self.max_live_processes}")
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
        _maybe_record_completion_event(state)
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
        _maybe_record_completion_event(state)
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
        # User-visible kill completion is ordered before any immediately
        # following owner/host teardown can suppress callbacks. The reader
        # thread may race here, but _maybe_record_completion_event() grants one
        # recording owner under state.lock and only marks success after commit.
        _maybe_record_completion_event(state)
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
        with self._lock:
            if self._closed:
                return
            self._closed = True
            states = list(self._processes.values())
        for state in states:
            if state.is_running:
                kill_process(state, reason=TerminalKillReason.TEARDOWN)

    def kill_owned(self, owner_host_session_id: str) -> list[TerminalResult]:
        with self._lock:
            states = [
                state
                for state in self._processes.values()
                if state.owner_host_session_id == owner_host_session_id
            ]
        return self._kill_states(states)

    def release_owner(
        self,
        owner_host_session_id: str,
        *,
        completion_drain_timeout_seconds: float = 1.0,
    ) -> list[TerminalResult]:
        """Drain canonical completions before irrevocably revoking an owner."""
        with self._lock:
            states = [
                state
                for state in self._processes.values()
                if state.owner_host_session_id == owner_host_session_id
            ]
        results = self._kill_states(states)
        self.drain_pending_completions(
            owner_host_session_id,
            timeout_seconds=completion_drain_timeout_seconds,
        )
        with self._lock:
            self._released_owners.add(owner_host_session_id)
        return results

    def drain_pending_completions(
        self,
        owner_host_session_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            with self._lock:
                pending = [
                    state
                    for state in self._processes.values()
                    if state.owner_host_session_id == owner_host_session_id
                    and _completion_record_is_pending(state)
                ]
            if not pending:
                return
            for state in pending:
                _start_completion_event_recording(state)
            with self._lock:
                remaining = [
                    state
                    for state in self._processes.values()
                    if state.owner_host_session_id == owner_host_session_id
                    and _completion_record_is_pending(state)
                ]
            if not remaining:
                return
            if time.monotonic() >= deadline:
                raise PendingTerminalCompletionError(
                    owner_host_session_id=owner_host_session_id,
                    pending_count=len(remaining),
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _kill_states(self, states: list[TerminalProcessState]) -> list[TerminalResult]:
        results: list[TerminalResult] = []
        for state in states:
            if state.is_running:
                kill_process(state, reason=TerminalKillReason.TEARDOWN)
            results.append(snapshot_process(state))
        return results

    def list_owned(self, owner_host_session_id: str) -> list[TerminalResult]:
        with self._lock:
            self._cleanup_finished_locked()
            states = [
                state
                for state in self._processes.values()
                if state.owner_host_session_id == owner_host_session_id
            ]
        return [
            snapshot_process(state)
            for state in states
        ]

    def list_processes(
        self,
        *,
        owner_host_session_id: str | None = None,
        include_finished: bool = True,
        include_running: bool = True,
    ) -> list[TerminalProcessInfo]:
        with self._lock:
            self._cleanup_finished_locked()
            states = list(self._processes.values())
        for state in states:
            if state.is_finished:
                _maybe_record_completion_event(state)
        processes = [
            process_info(state)
            for state in states
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
        _maybe_record_completion_event(state)
        return process_log(state, max_output_chars=max_output_chars or state.max_output_chars)

    def live_count(self, *, owner_host_session_id: str | None = None) -> int:
        with self._lock:
            states = list(self._processes.values())
        return sum(
            1
            for state in states
            if state.yielded
            and state.is_running
            and (owner_host_session_id is None or state.owner_host_session_id == owner_host_session_id)
        )

    def finished_count(self, *, owner_host_session_id: str | None = None) -> int:
        with self._lock:
            states = list(self._processes.values())
        return sum(
            1
            for state in states
            if state.yielded
            and state.is_finished
            and (owner_host_session_id is None or state.owner_host_session_id == owner_host_session_id)
        )

    def pending_completion_count(
        self,
        *,
        owner_host_session_id: str | None = None,
    ) -> int:
        with self._lock:
            states = list(self._processes.values())
        return sum(
            1
            for state in states
            if _completion_record_is_pending(state)
            and (
                owner_host_session_id is None
                or state.owner_host_session_id == owner_host_session_id
            )
        )

    def _get(
        self,
        process_id: str,
        *,
        owner_host_session_id: str | None = None,
    ) -> TerminalProcessState:
        with self._lock:
            self._cleanup_finished_locked()
            try:
                state = self._processes[process_id]
            except KeyError as exc:
                raise KeyError(f"terminal process not found or expired: {process_id}") from exc
        if owner_host_session_id is not None and state.owner_host_session_id != owner_host_session_id:
            raise KeyError(f"terminal process not found or not owned by this session: {process_id}")
        return state

    def _live_count_locked(self) -> int:
        return sum(1 for state in self._processes.values() if state.yielded and state.is_running)

    def _pending_completion_count_locked(self) -> int:
        return sum(
            1
            for state in self._processes.values()
            if _completion_record_is_pending(state)
        )

    def _is_accepting_locked(self, owner_host_session_id: str | None) -> bool:
        return not self._closed and (
            owner_host_session_id is None or owner_host_session_id not in self._released_owners
        )

    def _require_accepting_locked(self, owner_host_session_id: str | None) -> None:
        if self._closed:
            raise ProcessLimitError("terminal process registry is closed")
        if owner_host_session_id is not None and owner_host_session_id in self._released_owners:
            raise ProcessLimitError(f"terminal owner has been released: {owner_host_session_id}")

    def _cleanup_finished(self) -> None:
        with self._lock:
            self._cleanup_finished_locked()

    def _cleanup_finished_locked(self) -> None:
        now = time.monotonic()
        expired = [
            process_id
            for process_id, state in self._processes.items()
            if state.yielded
            and state.is_finished
            and state.ended_at is not None
            and now - state.ended_at > self.finished_ttl_seconds
            and not _completion_record_is_pending(state)
        ]
        for process_id in expired:
            self._processes.pop(process_id, None)

        finished = [
            (process_id, state.ended_at or state.started_at)
            for process_id, state in self._processes.items()
            if state.yielded
            and state.is_finished
            and not _completion_record_is_pending(state)
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
                start_new_session=True,
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
            start_new_session=True,
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
    reason: TerminalKillReason = TerminalKillReason.USER,
) -> bool:
    if not _claim_kill_transition(state, reason):
        return False
    _terminate_process_group(state.process)
    _join_reader(state)
    return True


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


def _claim_kill_transition(
    state: TerminalProcessState,
    reason: TerminalKillReason,
) -> bool:
    """Atomically win kill ownership or preserve the existing terminal fact."""

    with state.lock:
        if state.status is not TerminalStatus.RUNNING:
            return False
        state.completion_reason = reason
        if reason in {TerminalKillReason.TEARDOWN, TerminalKillReason.LIFETIME_WATCHDOG}:
            state.completion_suppressed = True
            state.record_event = None
        state.status = TerminalStatus.KILLED
        state.exit_code = -signal.SIGTERM
        state.ended_at = time.monotonic()
        return True


def _duration_seconds_locked(state: TerminalProcessState) -> float:
    end = state.ended_at if state.ended_at is not None else time.monotonic()
    return max(0.0, end - state.started_at)


def _maybe_record_completion_event(state: TerminalProcessState) -> AgentEvent | None:
    event_data = _claim_completion_event_recording(state)
    if event_data is None:
        return None
    return _record_claimed_completion_event(state, event_data)


def _start_completion_event_recording(state: TerminalProcessState) -> bool:
    """Claim one attempt synchronously, but run its recorder off the caller."""

    event_data = _claim_completion_event_recording(state)
    if event_data is None:
        return False
    try:
        worker = Thread(
            target=_completion_recording_worker,
            args=(state, event_data),
            name=f"terminal-completion-{state.process_id}",
            daemon=True,
        )
        worker.start()
    except BaseException as exc:
        _finish_completion_event_recording(state, success=False)
        if isinstance(exc, Exception):
            return False
        raise
    return True


def _completion_recording_worker(
    state: TerminalProcessState,
    event_data: tuple[
        Callable[[AgentEvent], AgentEvent],
        dict[str, object],
        TerminalProcessCompletedEvent | None,
    ],
) -> None:
    try:
        _record_claimed_completion_event(state, event_data)
    except BaseException:
        # _record_claimed_completion_event() restores ownership before
        # propagating non-Exception BaseException values. A daemon worker must
        # not leak those through threading.excepthook during interpreter close.
        return


def _record_claimed_completion_event(
    state: TerminalProcessState,
    event_data: tuple[
        Callable[[AgentEvent], AgentEvent],
        dict[str, object],
        TerminalProcessCompletedEvent | None,
    ],
) -> AgentEvent | None:
    record_event, fields, candidate = event_data
    try:
        event = candidate
        if event is None:
            processed = state.output.snapshot(max_chars=2000)
            fields["output_preview"] = processed.text
            fields["output_truncated"] = processed.truncated
            event = TerminalProcessCompletedEvent(**fields)
            with state.lock:
                if state.completion_event_candidate is None:
                    state.completion_event_candidate = event
                else:
                    event = state.completion_event_candidate
        stored = record_event(event)
    except BaseException as exc:
        _finish_completion_event_recording(state, success=False)
        _schedule_completion_event_retry(state)
        if isinstance(exc, Exception):
            return None
        raise
    _finish_completion_event_recording(state, success=True)
    return stored


def _claim_completion_event_recording(
    state: TerminalProcessState,
) -> tuple[
    Callable[[AgentEvent], AgentEvent],
    dict[str, object],
    TerminalProcessCompletedEvent | None,
] | None:
    with state.lock:
        if not state.yielded:
            return None
        if state.status is TerminalStatus.RUNNING:
            return None
        if state.completion_suppressed:
            return None
        if state.completion_record_state is not TerminalCompletionRecordState.PENDING:
            return None
        if (
            state.record_event is None
            or state.origin_run_id is None
            or state.origin_turn_id is None
            or state.origin_reply_id is None
        ):
            return None
        state.completion_record_state = TerminalCompletionRecordState.RECORDING
        state.completion_record_attempts += 1
        record_event = state.record_event
        fields: dict[str, object] = {
            "id": state.completion_event_id,
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
            "completion_reason": state.completion_reason.value if state.completion_reason is not None else None,
        }
        candidate = state.completion_event_candidate
    return record_event, fields, candidate


def _finish_completion_event_recording(
    state: TerminalProcessState,
    *,
    success: bool,
) -> None:
    retry_timer: Timer | None = None
    with state.lock:
        if state.completion_record_state is not TerminalCompletionRecordState.RECORDING:
            return
        state.completion_record_state = (
            TerminalCompletionRecordState.RECORDED
            if success
            else TerminalCompletionRecordState.PENDING
        )
        if success:
            retry_timer = state.completion_retry_timer
            state.completion_retry_timer = None
    if retry_timer is not None:
        retry_timer.cancel()


def _schedule_completion_event_retry(state: TerminalProcessState) -> None:
    with state.lock:
        if state.completion_record_state is not TerminalCompletionRecordState.PENDING:
            return
        if state.completion_retry_timer is not None:
            return
        retry_index = state.completion_record_attempts - 1
        if retry_index >= len(_COMPLETION_RECORD_RETRY_DELAYS_SECONDS):
            return
        timer = Timer(
            _COMPLETION_RECORD_RETRY_DELAYS_SECONDS[retry_index],
            _retry_completion_event,
            args=(state,),
        )
        timer.daemon = True
        state.completion_retry_timer = timer
    try:
        timer.start()
    except BaseException as exc:
        with state.lock:
            if state.completion_retry_timer is timer:
                state.completion_retry_timer = None
        if not isinstance(exc, Exception):
            raise


def _retry_completion_event(state: TerminalProcessState) -> None:
    with state.lock:
        state.completion_retry_timer = None
    _maybe_record_completion_event(state)


def _completion_record_is_pending(state: TerminalProcessState) -> bool:
    with state.lock:
        required = state.yielded and _completion_record_contract_present_locked(state)
        return required and (
            state.completion_record_state is not TerminalCompletionRecordState.RECORDED
        )


def _completion_record_contract_present(state: TerminalProcessState) -> bool:
    with state.lock:
        return _completion_record_contract_present_locked(state)


def _completion_record_contract_present_locked(state: TerminalProcessState) -> bool:
    return (
        not state.completion_suppressed
        and state.record_event is not None
        and state.origin_run_id is not None
        and state.origin_turn_id is not None
        and state.origin_reply_id is not None
    )


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
