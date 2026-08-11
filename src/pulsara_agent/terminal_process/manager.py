"""Host-owned Terminal sessions, physical processes, and sanitized output."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import pty
import shlex
import signal
import subprocess
import tempfile
from threading import Condition, Event, RLock, Thread, Timer, current_thread
from time import monotonic, sleep
from typing import Callable, IO
from uuid import uuid4

from pulsara_agent.terminal_process.environment import TerminalEnvironmentOwner
from pulsara_agent.terminal_process.models import (
    TerminalIOMode,
    TerminalPhysicalState,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.terminal_process.output import (
    OutputSubscriber,
    TERMINAL_HOST_RETAINED_HARD_BYTES,
    TERMINAL_RETAINED_OUTPUT_HARD_BYTES,
    TerminalOutputOwner,
    TerminalOutputObservationSlice,
    TerminalOutputSnapshot,
)


DEFAULT_TERMINAL_SESSION_ID = "default"
_TIMEOUT_EXIT_CODE = 124
_SESSION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


class ProcessLimitError(RuntimeError):
    pass


class ProcessInputError(RuntimeError):
    pass


class ProcessPhysicalJoinError(RuntimeError):
    pass


TerminalCompletionSubscriber = Callable[
    [TerminalProcessInfo, TerminalOutputSnapshot], None
]


@dataclass(slots=True)
class _ProcessState:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: Path
    owner_host_session_id: str
    origin: TerminalProcessOrigin
    io_mode: TerminalIOMode
    process: subprocess.Popen[bytes]
    output: TerminalOutputOwner
    stdin: IO[bytes] | None
    reader: Thread
    watcher: Thread | None
    master_fd: int | None
    started_at: float
    cwd_probe_path: Path | None
    deadline_timer: Timer | None = None
    timed_out: bool = False
    killed: bool = False
    stdin_closed: bool = False
    ended_at: float | None = None
    physical_state: TerminalPhysicalState = TerminalPhysicalState.RUNNING
    yield_decision: bool | None = None
    completion_offered: bool = False
    physical_completion: Event = field(default_factory=Event)
    lock: RLock = field(default_factory=RLock)

    def refresh(self) -> None:
        code = self.process.poll()
        if code is not None and self.physical_state is TerminalPhysicalState.RUNNING:
            self.physical_state = TerminalPhysicalState.TERMINALIZING


class ProcessRegistry:
    def __init__(
        self,
        *,
        max_live_processes: int = 8,
        max_finished_processes: int = 32,
        finished_ttl_seconds: float = 3600.0,
        maximum_host_retained_bytes: int = TERMINAL_HOST_RETAINED_HARD_BYTES,
        completion_subscriber: TerminalCompletionSubscriber | None = None,
    ) -> None:
        self.max_live_processes = max_live_processes
        self.max_finished_processes = max_finished_processes
        self.finished_ttl_seconds = finished_ttl_seconds
        self.maximum_host_retained_bytes = maximum_host_retained_bytes
        self._completion_subscriber = completion_subscriber
        self._states: dict[str, _ProcessState] = {}
        self._launching_by_owner: dict[str, int] = {}
        self._released_owners: set[str] = set()
        self._closed = False
        self._lock = RLock()
        self._launch_condition = Condition(self._lock)

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
        yield_time_ms: int,
        tty: bool,
        max_lifetime_seconds: int | None,
        owner_host_session_id: str,
        shell_argv: tuple[str, ...],
        env: dict[str, str],
        origin: TerminalProcessOrigin | None = None,
        output_subscriber: OutputSubscriber | None = None,
        cwd_probe_path: Path | None = None,
    ) -> tuple[_ProcessState, bool, str | None]:
        with self._launch_condition:
            if self._closed or owner_host_session_id in self._released_owners:
                raise RuntimeError("terminal process owner is closed")
            self._prune_locked()
            if (
                sum(
                    _occupies_live_capacity(state)
                    for state in self._states.values()
                )
                + sum(self._launching_by_owner.values())
                >= self.max_live_processes
            ):
                raise ProcessLimitError(
                    f"terminal process limit reached: max {self.max_live_processes}"
                )
            self._launching_by_owner[owner_host_session_id] = (
                self._launching_by_owner.get(owner_host_session_id, 0) + 1
            )
        launch_reserved = True
        try:
            state = self._spawn(
                terminal_session_id=terminal_session_id,
                command=command,
                cwd=cwd,
                tty=tty,
                owner_host_session_id=owner_host_session_id,
                shell_argv=shell_argv,
                env=env,
                origin=(
                    origin
                    or TerminalProcessOrigin(
                        turn_id=f"terminal-owner:{owner_host_session_id}",
                        conversation_scope_kind="ROOT",
                    )
                ),
                cwd_probe_path=cwd_probe_path,
            )
        except BaseException:
            with self._launch_condition:
                self._release_launch_locked(owner_host_session_id)
            raise
        live_subscription: str | None = None
        if output_subscriber is not None:
            live_subscription, _ = state.output.subscribe(output_subscriber)
        closed_during_launch = False
        with self._launch_condition:
            if self._closed or owner_host_session_id in self._released_owners:
                closed_during_launch = True
            else:
                self._states[state.process_id] = state
            # Reader, watcher and lifetime timer are installed while the
            # registry lock excludes release_owner().  A visible process state
            # therefore never exists without all of its physical owners.
            self._start_physical_threads(state)
            if max_lifetime_seconds is not None and not closed_during_launch:
                timer = Timer(
                    max_lifetime_seconds, self._expire, args=(state.process_id,)
                )
                timer.daemon = False
                state.deadline_timer = timer
                timer.start()
            if not closed_during_launch:
                # Publication and reservation release share one lock.  A
                # concurrent admission or owner close therefore observes
                # either the launching slot or the fully-owned process state,
                # never a capacity-free gap between them.
                self._release_launch_locked(owner_host_session_id)
                launch_reserved = False
        try:
            if closed_during_launch:
                _terminate_process_group(state)
                if not _join_physical(state, timeout=2.0):
                    raise ProcessPhysicalJoinError(
                        "terminal process owner closed during launch before physical join"
                    )
                raise RuntimeError("terminal process owner closed during launch")
            if yield_time_ms > 0:
                state.physical_completion.wait(yield_time_ms / 1000)
            state.refresh()
            yielded = not state.physical_completion.is_set()
            final_cwd = self.finalize_yield_decision(state, yielded=yielded)
            return state, yielded, final_cwd
        except BaseException as error:
            # No caller received this process identity, so it must not remain
            # as an invisible physical effect.  Freeze the cwd probe as
            # adoption-disallowed, terminate the whole group, and prove the
            # reader/watcher/process exit before propagating the error.
            with state.lock:
                if state.yield_decision is None:
                    state.yield_decision = True
            state.killed = _process_is_live(state)
            _terminate_process_group(state)
            if not _join_physical(state, timeout=2.0):
                raise ProcessPhysicalJoinError(
                    "failed terminal launch did not physically join"
                ) from error
            self._cleanup_disallowed_cwd_probe(state)
            raise
        finally:
            if live_subscription is not None:
                state.output.unsubscribe(live_subscription)
            if launch_reserved:
                with self._launch_condition:
                    self._release_launch_locked(owner_host_session_id)

    def _spawn(
        self,
        *,
        terminal_session_id: str,
        command: str,
        cwd: Path,
        tty: bool,
        owner_host_session_id: str,
        shell_argv: tuple[str, ...],
        env: dict[str, str],
        origin: TerminalProcessOrigin,
        cwd_probe_path: Path | None,
    ) -> _ProcessState:
        process_id = f"proc_{uuid4().hex}"
        output = TerminalOutputOwner(
            owner_epoch=owner_host_session_id,
            process_id=process_id,
            retained_bytes_changed=self._retained_bytes_changed,
        )
        master_fd: int | None = None
        if tty:
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    shell_argv,
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
                daemon=False,
            )
            mode = TerminalIOMode.PTY
        else:
            process = subprocess.Popen(
                shell_argv,
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
                daemon=False,
            )
            mode = TerminalIOMode.PIPE
        return _ProcessState(
            process_id=process_id,
            terminal_session_id=terminal_session_id,
            command=command,
            cwd=cwd,
            owner_host_session_id=owner_host_session_id,
            origin=origin,
            io_mode=mode,
            process=process,
            output=output,
            stdin=stdin,
            reader=reader,
            watcher=None,
            master_fd=master_fd,
            started_at=monotonic(),
            cwd_probe_path=cwd_probe_path,
        )

    def _start_physical_threads(self, state: _ProcessState) -> None:
        state.reader.start()
        watcher = Thread(
            target=self._watch_process,
            args=(state,),
            name=f"terminal-watcher:{state.process_id}",
            daemon=False,
        )
        state.watcher = watcher
        watcher.start()

    def _watch_process(self, state: _ProcessState) -> None:
        code = state.process.wait()
        with state.lock:
            state.physical_state = TerminalPhysicalState.TERMINALIZING
        state.reader.join()
        # The shell leader is not the physical process boundary.  A detached
        # descendant can remain in the same group after closing or redirecting
        # stdout.  Completion, capacity release and monitor wake all wait for
        # the exact group to disappear.
        while _process_group_exists(state.process.pid):
            sleep(0.01)
        with state.lock:
            state.ended_at = monotonic()
        status = _status(state)
        state.output.finalize(status=status.value, exit_code=code)
        if state.deadline_timer is not None:
            state.deadline_timer.cancel()
        self._cleanup_disallowed_cwd_probe(state)
        with state.lock:
            if state.physical_state is not TerminalPhysicalState.PRUNABLE:
                state.physical_state = TerminalPhysicalState.PHYSICALLY_JOINED
        # This is the sole normal physical-completion publication.  It is
        # intentionally after group exit, reader join, sanitizer finalization,
        # and cwd cleanup, but before a best-effort external subscriber can
        # delay waiters or retain process capacity.
        state.physical_completion.set()
        subscriber = self._completion_subscriber
        if subscriber is not None:
            should_offer = False
            with state.lock:
                if not state.completion_offered:
                    state.completion_offered = True
                    should_offer = True
            if should_offer:
                try:
                    subscriber(
                        _info(state),
                        state.output.snapshot(
                            maximum_chars=TERMINAL_RETAINED_OUTPUT_HARD_BYTES
                        ),
                    )
                except Exception:
                    pass

    def _owned(self, process_id: str, owner: str) -> _ProcessState:
        with self._lock:
            state = self._states.get(process_id)
        if state is None or state.owner_host_session_id != owner:
            raise KeyError(process_id)
        state.refresh()
        return state

    def output_owner(
        self, process_id: str, *, owner_host_session_id: str
    ) -> TerminalOutputOwner:
        return self._owned(process_id, owner_host_session_id).output

    def snapshot_and_subscribe(
        self,
        process_id: str,
        *,
        owner_host_session_id: str,
        callback: OutputSubscriber,
        maximum_chars: int = 32_000,
    ) -> tuple[TerminalOutputSnapshot, str]:
        state = self._owned(process_id, owner_host_session_id)
        # OutputOwner serializes both operations with the same lock.  Subscribe
        # first, then snapshot: callbacks may overlap the snapshot but their
        # exact offsets make the overlap mechanically deduplicable.
        token, _cursor = state.output.subscribe(callback)
        try:
            snapshot = state.output.snapshot(maximum_chars=maximum_chars)
        except BaseException:
            state.output.unsubscribe(token)
            raise
        return snapshot, token

    def unsubscribe_output(
        self, process_id: str, token: str, *, owner_host_session_id: str
    ) -> bool:
        return self._owned(process_id, owner_host_session_id).output.unsubscribe(token)

    def poll(
        self,
        process_id: str,
        *,
        max_output_chars: int,
        owner_host_session_id: str,
        since_cursor: str | None = None,
    ) -> TerminalResult:
        return _snapshot(
            self._owned(process_id, owner_host_session_id),
            max_output_chars,
            since_cursor=since_cursor,
        )

    def wait(
        self,
        process_id: str,
        *,
        timeout_seconds: int | None,
        max_output_chars: int,
        owner_host_session_id: str,
        since_cursor: str | None = None,
        output_subscriber: OutputSubscriber | None = None,
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        subscription: str | None = None
        if output_subscriber is not None:
            subscription, _ = state.output.subscribe(output_subscriber)
        try:
            state.physical_completion.wait(timeout_seconds)
            state.refresh()
            return _snapshot(state, max_output_chars, since_cursor=since_cursor)
        finally:
            if subscription is not None:
                state.output.unsubscribe(subscription)

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
        payload = (data + ("\n" if append_newline else "")).encode("utf-8")
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
                    if state.io_mode is TerminalIOMode.PTY:
                        # A duplicated PTY master is not an EOF boundary while
                        # the reader still owns the original master.  Send the
                        # terminal driver's canonical EOT before retiring the
                        # writable duplicate.
                        state.stdin.write(b"\x04")
                        state.stdin.flush()
                    state.stdin.close()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    try:
                        state.stdin.close()
                    except (OSError, ValueError):
                        pass
                    state.stdin_closed = True
                    raise ProcessInputError(
                        "terminal process stdin close failed"
                    ) from exc
                state.stdin_closed = True
        return _snapshot(state, max_output_chars)

    def kill(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalResult:
        state = self._owned(process_id, owner_host_session_id)
        state.killed = True
        _terminate_process_group(state)
        if not _join_physical(state, timeout=2.0):
            raise ProcessPhysicalJoinError("terminal process did not physically join")
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
            running = _process_is_live(state)
            if running and include_running or not running and include_finished:
                result.append(_info(state))
        return sorted(result, key=lambda item: item.started_at_monotonic)

    def log(
        self,
        process_id: str,
        *,
        max_output_chars: int,
        owner_host_session_id: str,
        since_cursor: str | None = None,
    ) -> TerminalProcessLog:
        state = self._owned(process_id, owner_host_session_id)
        snapshot, artifact_candidate = state.output.snapshot_with_artifact_candidate(
            maximum_chars=max_output_chars,
            since_cursor=since_cursor,
        )
        return TerminalProcessLog(
            process=_info(state),
            output=snapshot.text,
            truncated=(
                snapshot.gap_before_output or snapshot.truncated_by_response_bound
            ),
            output_artifact_candidate=artifact_candidate,
            output_disposition=snapshot.disposition,
            output_cursor=snapshot.output_cursor,
            retained_from_cursor=snapshot.retained_from_cursor,
            gap_before_output=snapshot.gap_before_output,
            truncated_by_response_bound=snapshot.truncated_by_response_bound,
            source_coverage=snapshot.source_coverage,
        )

    def observation_slice(
        self,
        process_id: str,
        *,
        maximum_chars: int,
        maximum_bytes: int,
        owner_host_session_id: str,
        since_cursor: str | None,
    ) -> TerminalOutputObservationSlice:
        state = self._owned(process_id, owner_host_session_id)
        return state.output.observation_slice(
            maximum_chars=maximum_chars,
            maximum_bytes=maximum_bytes,
            since_cursor=since_cursor,
        )

    def live_count(self, *, owner_host_session_id: str) -> int:
        with self._lock:
            return sum(
                state.owner_host_session_id == owner_host_session_id
                and _occupies_live_capacity(state)
                for state in self._states.values()
            )

    def finalize_yield_decision(
        self, state: _ProcessState, *, yielded: bool
    ) -> str | None:
        with state.lock:
            if state.yield_decision is not None:
                raise RuntimeError("terminal yield decision is already installed")
            state.yield_decision = yielded
        if yielded:
            self._cleanup_disallowed_cwd_probe(state)
            return None
        return _read_and_cleanup_cwd_probe(state)

    def release_owner(
        self, owner: str, *, timeout_seconds: float
    ) -> list[TerminalResult]:
        if timeout_seconds <= 0:
            raise ValueError("terminal close timeout must be positive")
        deadline = monotonic() + timeout_seconds
        with self._launch_condition:
            self._released_owners.add(owner)
            while self._launching_by_owner.get(owner, 0):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "terminal owner launch reservation did not settle"
                    )
                self._launch_condition.wait(remaining)
            states = [
                state
                for state in self._states.values()
                if state.owner_host_session_id == owner
            ]
        for state in states:
            state.killed = _process_is_live(state)
            _terminate_process_group(state)
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
                {
                    *(state.owner_host_session_id for state in self._states.values()),
                    *self._launching_by_owner,
                }
            )
            self._closed = True
        for owner in owners:
            self.release_owner(owner, timeout_seconds=5.0)

    def _expire(self, process_id: str) -> None:
        with self._lock:
            state = self._states.get(process_id)
        if state is None or not _process_is_live(state):
            return
        state.timed_out = True
        _terminate_process_group(state)

    def _prune_locked(self) -> None:
        now = monotonic()
        finished: list[tuple[float, str, _ProcessState]] = []
        for process_id, state in self._states.items():
            state.refresh()
            if state.ended_at is not None:
                finished.append((state.ended_at, process_id, state))
        for ended, process_id, state in sorted(finished):
            expired = now - ended > self.finished_ttl_seconds
            if expired and _mark_prunable(state):
                self._states.pop(process_id, None)
        remaining = sorted(
            (state.ended_at or now, process_id, state)
            for process_id, state in self._states.items()
            if not _process_is_live(state)
        )
        excess = max(0, len(remaining) - self.max_finished_processes)
        for _ended, process_id, state in remaining[:excess]:
            if _mark_prunable(state):
                self._states.pop(process_id, None)

    def _retained_bytes_changed(self, _owner: TerminalOutputOwner) -> None:
        with self._lock:
            states = tuple(self._states.values())
        total = sum(state.output.retained_utf8_bytes for state in states)
        excess = total - self.maximum_host_retained_bytes
        if excess <= 0:
            return
        finished = sorted(
            (state.ended_at or monotonic(), state)
            for state in states
            if not _process_is_live(state)
        )
        live = sorted(
            (state.started_at, state) for state in states if _process_is_live(state)
        )
        for _order, state in (*finished, *live):
            if excess <= 0:
                break
            current = state.output.retained_utf8_bytes
            removed = state.output.evict_oldest_to(max(0, current - excess))
            excess -= removed
        if excess > 0:
            raise RuntimeError(
                "terminal Host retained byte bound could not be enforced"
            )

    def _release_launch_locked(self, owner: str) -> None:
        count = self._launching_by_owner.get(owner, 0)
        if count <= 0:
            raise RuntimeError("terminal launch reservation is not installed")
        if count == 1:
            self._launching_by_owner.pop(owner, None)
        else:
            self._launching_by_owner[owner] = count - 1
        self._launch_condition.notify_all()

    @staticmethod
    def _cleanup_disallowed_cwd_probe(state: _ProcessState) -> None:
        with state.lock:
            if state.yield_decision is not True or _process_is_live(state):
                return
            path = state.cwd_probe_path
            state.cwd_probe_path = None
        if path is not None:
            path.unlink(missing_ok=True)


@dataclass(slots=True)
class TerminalSession:
    state: TerminalSessionState
    registry: ProcessRegistry
    environment: TerminalEnvironmentOwner

    def execute(
        self,
        request: TerminalRequest,
        *,
        output_subscriber: OutputSubscriber | None = None,
        origin: TerminalProcessOrigin | None = None,
    ) -> TerminalResult:
        current = _nearest_existing_cwd(
            self.state.current_cwd, self.state.workspace_root
        )
        cwd = _resolve_workdir(
            request.workdir, current=current, workspace=self.state.workspace_root
        )
        environment = self.environment.build(cwd=cwd)
        probe = _new_cwd_probe(self.state.workspace_root)
        command = _command_with_cwd_probe(request.command, probe)
        try:
            process, yielded, final_cwd = self.registry.exec_with_yield(
                terminal_session_id=self.state.session_id,
                command=request.command,
                cwd=cwd,
                yield_time_ms=request.yield_time_ms,
                tty=request.tty,
                max_lifetime_seconds=request.max_lifetime_seconds,
                owner_host_session_id=self.state.owner_host_session_id,
                shell_argv=environment.shell.command_argv(command),
                env=environment.values,
                output_subscriber=output_subscriber,
                cwd_probe_path=probe,
                origin=origin,
            )
        except ProcessLimitError as exc:
            probe.unlink(missing_ok=True)
            return TerminalResult(
                status=TerminalStatus.BLOCKED,
                output="",
                exit_code=-1,
                cwd=str(cwd),
                error=str(exc),
                shell_diagnostic=environment.diagnostic,
            )
        except BaseException:
            # ProcessRegistry owns any successfully spawned child and drains
            # it before raising.  This caller still owns the path for failures
            # that happened before a child state existed.
            probe.unlink(missing_ok=True)
            raise
        result = _snapshot(process, request.max_output_chars)
        if not yielded and final_cwd is not None:
            candidate = Path(final_cwd)
            if (
                candidate == self.state.workspace_root
                or self.state.workspace_root in candidate.parents
            ):
                if candidate.is_dir():
                    self.state.current_cwd = candidate
        return replace(
            result,
            cwd=str(self.state.current_cwd if not yielded else cwd),
            shell_diagnostic=environment.diagnostic,
        )


@dataclass(slots=True)
class TerminalSessionManager:
    workspace_root: Path
    max_sessions: int = 4
    max_live_processes: int = 8
    max_finished_processes: int = 32
    finished_ttl_seconds: float = 3600.0
    completion_subscriber: TerminalCompletionSubscriber | None = None
    _sessions: dict[tuple[str, str], TerminalSession] = field(
        default_factory=dict, init=False
    )
    _released_owners: set[str] = field(default_factory=set, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)
    process_registry: ProcessRegistry = field(init=False)
    environment_owner: TerminalEnvironmentOwner = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.process_registry = ProcessRegistry(
            max_live_processes=self.max_live_processes,
            max_finished_processes=self.max_finished_processes,
            finished_ttl_seconds=self.finished_ttl_seconds,
            completion_subscriber=self.completion_subscriber,
        )
        self.environment_owner = TerminalEnvironmentOwner(self.workspace_root)

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
        if (
            not normalized
            or len(normalized) > 32
            or any(char not in _SESSION_CHARS for char in normalized)
        ):
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
                self.environment_owner,
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
        self, owner_host_session_id: str, *, timeout_seconds: float = 5.0
    ) -> list[TerminalResult]:
        deadline = monotonic() + timeout_seconds
        results = self.process_registry.release_owner(
            owner_host_session_id, timeout_seconds=timeout_seconds
        )
        self.environment_owner.close(timeout_seconds=max(0.01, deadline - monotonic()))
        with self._lock:
            self._released_owners.add(owner_host_session_id)
            for key in [
                key for key in self._sessions if key[0] == owner_host_session_id
            ]:
                self._sessions.pop(key, None)
        return results


def _read_stream(stream: IO[bytes], output: TerminalOutputOwner) -> None:
    try:
        # ``BufferedReader.read(n)`` may wait for all ``n`` bytes or EOF even
        # after the child flushes a small progress line.  Read the underlying
        # descriptor so PIPE and PTY both deliver genuinely incremental
        # sanitized output while the process is still running.
        while True:
            try:
                chunk = os.read(stream.fileno(), 64 * 1024)
            except OSError:
                break
            if not chunk:
                break
            output.append_raw(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _read_fd(fd: int, output: TerminalOutputOwner) -> None:
    try:
        while True:
            try:
                chunk = os.read(fd, 64 * 1024)
            except OSError:
                break
            if not chunk:
                break
            output.append_raw(chunk)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _terminate_process_group(state: _ProcessState) -> None:
    with state.lock:
        if state.physical_state is TerminalPhysicalState.RUNNING:
            state.physical_state = TerminalPhysicalState.TERMINALIZING
    # The session leader may already have exited while a descendant remains in
    # its process group (and may still own stdout).  Always target the group;
    # checking only Popen.poll() would strand that descendant and its reader.
    try:
        os.killpg(state.process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        state.process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    group_deadline = monotonic() + 0.5
    while _process_group_exists(state.process.pid) and monotonic() < group_deadline:
        sleep(0.01)
    if _process_group_exists(state.process.pid):
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
    watcher = state.watcher
    if watcher is not None and watcher is not current_thread():
        watcher.join(timeout=max(0.0, deadline - monotonic()))
        if watcher.is_alive():
            return False
    if state.deadline_timer is not None:
        state.deadline_timer.cancel()
        if state.deadline_timer is not current_thread():
            state.deadline_timer.join(timeout=max(0.0, deadline - monotonic()))
            if state.deadline_timer.is_alive():
                return False
    if state.stdin is not None and not state.stdin_closed:
        try:
            state.stdin.close()
        except OSError:
            pass
        state.stdin_closed = True
    state.refresh()
    while _process_group_exists(state.process.pid) and monotonic() < deadline:
        sleep(0.005)
    if _process_group_exists(state.process.pid):
        return False
    with state.lock:
        if state.physical_state is not TerminalPhysicalState.PRUNABLE:
            state.physical_state = TerminalPhysicalState.PHYSICALLY_JOINED
    return True


def _mark_prunable(state: _ProcessState) -> bool:
    with state.lock:
        if state.physical_state is TerminalPhysicalState.PRUNABLE:
            return True
    if not _join_physical(state, timeout=0.1):
        return False
    if state.output.observation_lease_count or state.output.inflight_read_count:
        return False
    with state.lock:
        state.physical_state = TerminalPhysicalState.PRUNABLE
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group that exists but cannot be signalled is still not physically
        # joined by this owner.
        return True
    return True


def _process_is_live(state: _ProcessState) -> bool:
    """Return whether any physical member of this exact process group lives."""

    return state.process.poll() is None or _process_group_exists(state.process.pid)


def _occupies_live_capacity(state: _ProcessState) -> bool:
    """Retain capacity until the complete process-local physical boundary."""

    return not state.physical_completion.is_set()


def _status(state: _ProcessState) -> TerminalStatus:
    code = state.process.poll()
    if _process_is_live(state):
        return TerminalStatus.RUNNING
    if state.timed_out:
        return TerminalStatus.TIMEOUT
    if state.killed:
        return TerminalStatus.KILLED
    return TerminalStatus.SUCCESS if code == 0 else TerminalStatus.ERROR


def _info(state: _ProcessState) -> TerminalProcessInfo:
    state.refresh()
    output = state.output.snapshot(maximum_chars=1)
    ended = state.ended_at
    return TerminalProcessInfo(
        process_id=state.process_id,
        terminal_session_id=state.terminal_session_id,
        command=state.command,
        cwd=str(state.cwd),
        backend_type="local",
        io_mode=state.io_mode.value,
        status=_status(state).value,
        exit_code=None if _process_is_live(state) else state.process.poll(),
        timed_out=state.timed_out,
        stdin_closed=state.stdin_closed,
        started_at_monotonic=state.started_at,
        ended_at_monotonic=ended,
        duration_seconds=(ended or monotonic()) - state.started_at,
        owner_host_session_id=state.owner_host_session_id,
        origin=state.origin,
        stream_id=state.output.stream_id,
        output_revision=output.output_revision,
        output_cursor=output.output_cursor,
        retained_from_cursor=output.retained_from_cursor,
        physical_state=state.physical_state.value,
    )


def _snapshot(
    state: _ProcessState,
    maximum_chars: int,
    *,
    since_cursor: str | None = None,
) -> TerminalResult:
    state.refresh()
    status = _status(state)
    code = state.process.poll()
    snapshot, artifact_candidate = state.output.snapshot_with_artifact_candidate(
        maximum_chars=maximum_chars,
        since_cursor=since_cursor,
    )
    return TerminalResult(
        status=status,
        output=snapshot.text,
        exit_code=(
            _TIMEOUT_EXIT_CODE
            if state.timed_out
            else code
            if not _process_is_live(state) and code is not None
            else -1
        ),
        cwd=str(state.cwd),
        timed_out=state.timed_out,
        truncated=snapshot.gap_before_output or snapshot.truncated_by_response_bound,
        error=None
        if status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
        else status.value,
        process_id=state.process_id,
        output_artifact_candidate=artifact_candidate,
        output_disposition=snapshot.disposition,
        output_cursor=snapshot.output_cursor,
        retained_from_cursor=snapshot.retained_from_cursor,
        gap_before_output=snapshot.gap_before_output,
        truncated_by_response_bound=snapshot.truncated_by_response_bound,
        source_coverage=snapshot.source_coverage,
    )


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


def _nearest_existing_cwd(current: Path, workspace: Path) -> Path:
    candidate = current
    while candidate != workspace and not candidate.is_dir():
        candidate = candidate.parent
    return candidate if candidate.is_dir() else workspace


def _new_cwd_probe(workspace: Path) -> Path:
    descriptor, raw = tempfile.mkstemp(prefix=".pulsara-cwd-", dir=workspace)
    os.close(descriptor)
    path = Path(raw)
    path.unlink(missing_ok=True)
    return path


def _command_with_cwd_probe(command: str, probe: Path) -> str:
    quoted = shlex.quote(str(probe))
    return (
        f"__pulsara_cwd_probe={quoted}; "
        "trap 'pwd -P > \"$__pulsara_cwd_probe\"' EXIT; "
        f"{command}"
    )


def _read_and_cleanup_cwd_probe(state: _ProcessState) -> str | None:
    with state.lock:
        path = state.cwd_probe_path
        state.cwd_probe_path = None
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        value = ""
    finally:
        path.unlink(missing_ok=True)
    return value or None


__all__ = [
    "DEFAULT_TERMINAL_SESSION_ID",
    "ProcessInputError",
    "ProcessLimitError",
    "ProcessPhysicalJoinError",
    "ProcessRegistry",
    "TerminalSession",
    "TerminalSessionManager",
]
