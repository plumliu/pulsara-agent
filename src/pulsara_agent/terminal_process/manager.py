"""Host-owned independent commands, physical processes, and sanitized output."""

from __future__ import annotations

from pulsara_agent.identity import random_id, require_id

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
import os
from pathlib import Path
import pty
import signal
import subprocess
from threading import Condition, Event, RLock, Thread, Timer, current_thread
from time import monotonic, sleep
from typing import Callable, IO, Iterator

from pulsara_agent.windows_interop import create_scope
from pulsara_agent.windows_interop.scope import WindowsInteropScope

from pulsara_agent.terminal_process.environment import TerminalEnvironmentOwner
from pulsara_agent.terminal_process.models import (
    TerminalCwdScope,
    TerminalFailureReason,
    TerminalIOMode,
    TerminalPhysicalState,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalStatus,
    TerminalTerminationDisposition,
    TerminalTerminationResult,
)
from pulsara_agent.terminal_process.output import (
    OutputSubscriber,
    TERMINAL_HOST_RETAINED_HARD_BYTES,
    TERMINAL_RETAINED_OUTPUT_HARD_BYTES,
    TerminalOutputOwner,
    TerminalOutputObservationSlice,
    TerminalOutputSnapshot,
)


_TIMEOUT_EXIT_CODE = 124


class ProcessLimitError(RuntimeError):
    pass


class ProcessInputError(RuntimeError):
    pass


class ProcessDecisionIdentityConflict(RuntimeError):
    """Admission rejected without acquiring the existing decision owner."""


class ProcessPhysicalJoinError(RuntimeError):
    pass


TerminalCompletionSubscriber = Callable[
    [TerminalProcessInfo, TerminalOutputSnapshot], None
]


class TerminalForegroundDecisionState(StrEnum):
    PREPARING = "PREPARING"
    PROCESS_INSTALLED = "PROCESS_INSTALLED"
    ABORT_REQUESTED = "ABORT_REQUESTED"
    RESULT_READY = "RESULT_READY"
    SETTLED = "SETTLED"


@dataclass(frozen=True, slots=True)
class TerminalForegroundDecisionAttemptHandle:
    attempt_id: str
    owner_host_session_id: str


@dataclass(slots=True)
class _TerminalForegroundDecisionAttempt:
    handle: TerminalForegroundDecisionAttemptHandle
    state: TerminalForegroundDecisionState
    process_id: str | None = None
    adoption_allowed: bool = True
    timer: Timer | None = None
    interop_scope: WindowsInteropScope | None = None
    launch_failed: bool = False


@dataclass(slots=True)
class _ProcessState:
    process_id: str
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
    interop_scope: WindowsInteropScope | None = None
    deadline_timer: Timer | None = None
    termination_intent: TerminalStatus | None = None
    final_status: TerminalStatus | None = None
    final_exit_code: int | None = None
    stdin_closed: bool = False
    ended_at: float | None = None
    physical_state: TerminalPhysicalState = TerminalPhysicalState.RUNNING
    yield_decision: bool | None = None
    background_adopted: bool = False
    completion_offered: bool = False
    reader_started: bool = False
    watcher_started: bool = False
    deadline_timer_started: bool = False
    physical_completion: Event = field(default_factory=Event)
    lock: RLock = field(default_factory=RLock)

    def refresh(self) -> None:
        code = self.process.poll()
        if code is not None and self.physical_state is TerminalPhysicalState.RUNNING:
            self.physical_state = TerminalPhysicalState.TERMINALIZING

    @property
    def timed_out(self) -> bool:
        return self.termination_intent is TerminalStatus.TIMEOUT

    @property
    def killed(self) -> bool:
        return self.termination_intent is TerminalStatus.KILLED


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
        self._owner_identity = random_id("terminal-owner")
        self.max_live_processes = max_live_processes
        self.max_finished_processes = max_finished_processes
        self.finished_ttl_seconds = finished_ttl_seconds
        self.maximum_host_retained_bytes = maximum_host_retained_bytes
        self._completion_subscriber = completion_subscriber
        self._states: dict[str, _ProcessState] = {}
        self._launching_by_owner: dict[str, int] = {}
        self._decision_attempts: dict[str, _TerminalForegroundDecisionAttempt] = {}
        self._released_owners: set[str] = set()
        self._closed = False
        self._lock = RLock()
        self._launch_condition = Condition(self._lock)
        self._retirement_thread: Thread | None = None
        self._retirement_stopping = False

    def activate_owner(self, owner: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal process registry is closed")
            self._released_owners.discard(owner)

    def exec_with_yield(
        self,
        *,
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
        decision_attempt_id: str | None = None,
        decision_deadline_monotonic: float | None = None,
        initial_result_reader: Callable[[_ProcessState], None] | None = None,
    ) -> tuple[_ProcessState, bool]:
        decision_handle = TerminalForegroundDecisionAttemptHandle(
            attempt_id=decision_attempt_id or random_id("terminal-decision"),
            owner_host_session_id=owner_host_session_id,
        )
        self._prune()
        with self._launch_condition:
            if self._closed or owner_host_session_id in self._released_owners:
                raise RuntimeError("terminal process owner is closed")
            if (
                sum(_occupies_live_capacity(state) for state in self._states.values())
                + sum(self._launching_by_owner.values())
                + sum(
                    attempt.interop_scope is not None
                    and not attempt.interop_scope.physically_joined.is_set()
                    and attempt.process_id not in self._states
                    and attempt.launch_failed
                    for attempt in self._decision_attempts.values()
                )
                >= self.max_live_processes
            ):
                raise ProcessLimitError(
                    f"terminal process limit reached: max {self.max_live_processes}"
                )
            self._launching_by_owner[owner_host_session_id] = (
                self._launching_by_owner.get(owner_host_session_id, 0) + 1
            )
            if decision_attempt_id is None:
                while decision_handle.attempt_id in self._decision_attempts:
                    decision_handle = TerminalForegroundDecisionAttemptHandle(
                        attempt_id=random_id("terminal-decision"),
                        owner_host_session_id=owner_host_session_id,
                    )
            if decision_handle.attempt_id in self._decision_attempts:
                self._release_launch_locked(owner_host_session_id)
                raise ProcessDecisionIdentityConflict(
                    "IDENTITY_CONFLICT: terminal foreground decision attempt already exists"
                )
            decision = _TerminalForegroundDecisionAttempt(
                handle=decision_handle,
                state=TerminalForegroundDecisionState.PREPARING,
            )
            self._decision_attempts[decision_handle.attempt_id] = decision
            if decision_deadline_monotonic is None:
                self._release_launch_locked(owner_host_session_id)
                self._decision_attempts.pop(decision_handle.attempt_id, None)
                raise ValueError(
                    "terminal foreground decision requires a policy-owned deadline"
                )
            decision_deadline = decision_deadline_monotonic
            remaining = decision_deadline - monotonic()
            if remaining <= 0:
                decision.state = TerminalForegroundDecisionState.ABORT_REQUESTED
                decision.adoption_allowed = False
            else:
                try:
                    timer = Timer(
                        remaining,
                        self._abort_foreground_decision,
                        args=(decision_handle.attempt_id,),
                    )
                    timer.daemon = False
                    decision.timer = timer
                    timer.start()
                except BaseException:
                    self._release_launch_locked(owner_host_session_id)
                    self._settle_foreground_decision_locked(decision_handle.attempt_id)
                    raise
        launch_reserved = True
        try:
            if remaining <= 0:
                raise TimeoutError("terminal foreground decision expired before spawn")
            state = self._spawn(
                decision_attempt_id=decision_handle.attempt_id,
                command=command,
                cwd=cwd,
                tty=tty,
                owner_host_session_id=owner_host_session_id,
                shell_argv=shell_argv,
                env=env,
                origin=(
                    origin
                    or TerminalProcessOrigin(
                        turn_id=self._owner_identity,
                        conversation_scope_kind="ROOT",
                    )
                ),
            )
        except BaseException as error:
            scope = decision.interop_scope
            unjoined = scope is not None and not scope.physically_joined.is_set()
            with self._launch_condition:
                decision.launch_failed = True
                self._release_launch_locked(owner_host_session_id)
                if not unjoined:
                    self._settle_foreground_decision_locked(decision_handle.attempt_id)
            if unjoined:
                raise ProcessPhysicalJoinError(
                    "terminal interop launch has an unknown physical boundary"
                ) from error
            raise
        live_subscription = (
            None if output_subscriber is None else random_id("subscription")
        )
        closed_during_launch = False
        decision_aborted = False
        initial_read = ExitStack()
        initial_read.enter_context(state.output.reading())
        reading_initial_result = False

        def finish_result(yielded: bool) -> tuple[_ProcessState, bool]:
            nonlocal reading_initial_result
            reading_initial_result = True
            if initial_result_reader is not None:
                initial_result_reader(state)
            return state, yielded

        try:
            (
                live_subscription,
                closed_during_launch,
                decision_aborted,
                launch_released,
            ) = self._install_spawned_process(
                state,
                decision_attempt_id=decision_handle.attempt_id,
                owner_host_session_id=owner_host_session_id,
                live_subscription=live_subscription,
                output_subscriber=output_subscriber,
                max_lifetime_seconds=max_lifetime_seconds,
            )
            if launch_released:
                launch_reserved = False
            self._ensure_retirement()
            if closed_during_launch:
                _terminate_process_group(state)
                if not _join_physical(state, timeout=2.0):
                    raise ProcessPhysicalJoinError(
                        "terminal process owner closed during launch before physical join"
                    )
                raise RuntimeError("terminal process owner closed during launch")
            if decision_aborted:
                with state.lock:
                    state.yield_decision = True
                _accept_termination_intent(state, TerminalStatus.KILLED)
                _terminate_process_group(state)
                if not _join_physical(state, timeout=2.0):
                    raise ProcessPhysicalJoinError(
                        "terminal decision abort did not physically join"
                    )
                self._mark_foreground_decision_result_ready(
                    decision_handle.attempt_id,
                    state.process_id,
                    background_adopted=False,
                )
                return finish_result(False)
            if yield_time_ms > 0:
                state.physical_completion.wait(yield_time_ms / 1000)
            if self._foreground_decision_abort_requested(decision_handle.attempt_id):
                with state.lock:
                    if state.yield_decision is None:
                        state.yield_decision = True
                _accept_termination_intent(state, TerminalStatus.KILLED)
                _terminate_process_group(state)
                if not _join_physical(state, timeout=2.0):
                    raise ProcessPhysicalJoinError(
                        "terminal decision watchdog did not physically join"
                    )
                self._mark_foreground_decision_result_ready(
                    decision_handle.attempt_id,
                    state.process_id,
                    background_adopted=False,
                )
                return finish_result(False)
            state.refresh()
            yielded = not state.physical_completion.is_set()
            self.finalize_yield_decision(state, yielded=yielded)
            published = self._mark_foreground_decision_result_ready(
                decision_handle.attempt_id,
                state.process_id,
                background_adopted=yielded,
            )
            if yielded and not published:
                _accept_termination_intent(state, TerminalStatus.KILLED)
                _terminate_process_group(state)
                if not _join_physical(state, timeout=2.0):
                    raise ProcessPhysicalJoinError(
                        "terminal decision abort did not physically join"
                    )
                return finish_result(False)
            return finish_result(yielded)
        except BaseException as error:
            initial_read.close()
            if live_subscription is not None:
                state.output.unsubscribe(live_subscription)
                live_subscription = None
            if reading_initial_result:
                # Execution/adoption has already settled. A read-only result
                # failure keeps that execution, just as the original caller's
                # snapshot/finally boundary did; it only retires this decision.
                self.settle_foreground_decision(decision_handle.attempt_id)
                raise
            # No caller received this process identity, so it must not remain
            # as an invisible physical effect. Disallow adoption, terminate the
            # whole group, and prove reader/watcher/process exit before failing.
            with state.lock:
                if state.yield_decision is None:
                    state.yield_decision = True
            _accept_termination_intent(state, TerminalStatus.KILLED)
            _terminate_process_group(state)
            if not _join_physical(state, timeout=2.0):
                # Preserve the still-physical owner for Host close rather than
                # releasing an invisible child.  This is a quarantine path,
                # not a successful launch publication.
                with self._launch_condition:
                    self._states[state.process_id] = state
                    self._wake_retirement_locked()
                    attempt = self._decision_attempts.get(decision_handle.attempt_id)
                    if attempt is not None:
                        attempt.process_id = state.process_id
                        attempt.state = TerminalForegroundDecisionState.ABORT_REQUESTED
                        attempt.adoption_allowed = False
                self._ensure_retirement()
                raise ProcessPhysicalJoinError(
                    "failed terminal launch did not physically join"
                ) from error
            with self._launch_condition:
                if (
                    self._states.get(state.process_id) is state
                    and state.output.close_reads_if_idle()
                ):
                    self._states.pop(state.process_id)
                self._settle_foreground_decision_locked(decision_handle.attempt_id)
            raise
        finally:
            try:
                if live_subscription is not None:
                    state.output.unsubscribe(live_subscription)
                if launch_reserved:
                    with self._launch_condition:
                        self._release_launch_locked(owner_host_session_id)
            finally:
                initial_read.close()

    def settle_foreground_decision(self, attempt_id: str) -> None:
        with self._launch_condition:
            self._settle_foreground_decision_locked(attempt_id)

    def foreground_decision_state(self, attempt_id: str) -> str | None:
        with self._lock:
            attempt = self._decision_attempts.get(attempt_id)
            return None if attempt is None else attempt.state.value

    def abort_foreground_decision(self, attempt_id: str) -> bool:
        """Abort one exact Terminal invocation still deciding foreground yield."""

        with self._lock:
            attempt = self._decision_attempts.get(attempt_id)
            if attempt is None or attempt.state in {
                TerminalForegroundDecisionState.RESULT_READY,
                TerminalForegroundDecisionState.SETTLED,
            }:
                return False
        self._abort_foreground_decision(attempt_id)
        return True

    def _abort_foreground_decision(self, attempt_id: str) -> None:
        state: _ProcessState | None = None
        with self._launch_condition:
            attempt = self._decision_attempts.get(attempt_id)
            if attempt is None or attempt.state in {
                TerminalForegroundDecisionState.RESULT_READY,
                TerminalForegroundDecisionState.SETTLED,
            }:
                return
            attempt.state = TerminalForegroundDecisionState.ABORT_REQUESTED
            attempt.adoption_allowed = False
            scope = attempt.interop_scope
            if attempt.process_id is not None:
                state = self._states.get(attempt.process_id)
        if scope is not None:
            scope.request_cancel()
        if state is not None:
            _accept_termination_intent(state, TerminalStatus.KILLED)
            _terminate_process_group(state)

    def _foreground_decision_abort_requested(self, attempt_id: str) -> bool:
        with self._lock:
            attempt = self._decision_attempts.get(attempt_id)
            return (
                attempt is not None
                and attempt.state is TerminalForegroundDecisionState.ABORT_REQUESTED
            )

    def _mark_foreground_decision_result_ready(
        self,
        attempt_id: str,
        process_id: str,
        *,
        background_adopted: bool,
    ) -> bool:
        with self._launch_condition:
            attempt = self._decision_attempts.get(attempt_id)
            if attempt is None:
                raise RuntimeError("terminal foreground decision attempt is absent")
            state = self._states.get(process_id)
            adoption_published = bool(
                background_adopted
                and attempt.adoption_allowed
                and attempt.state is not TerminalForegroundDecisionState.ABORT_REQUESTED
                and state is not None
                and state.yield_decision is True
            )
            if adoption_published:
                with state.lock:
                    state.background_adopted = True
            attempt.process_id = process_id
            attempt.state = TerminalForegroundDecisionState.RESULT_READY
            if attempt.timer is not None:
                attempt.timer.cancel()
                attempt.timer = None
            return adoption_published or not background_adopted

    def _settle_foreground_decision_locked(self, attempt_id: str) -> None:
        attempt = self._decision_attempts.get(attempt_id)
        if attempt is None:
            return
        if attempt.timer is not None:
            attempt.timer.cancel()
            attempt.timer = None
        if (
            attempt.interop_scope is not None
            and not attempt.interop_scope.physically_joined.is_set()
            and attempt.process_id not in self._states
        ):
            # The command caller may settle a failed decision, but cannot erase
            # its only remaining physical owner after bootstrap loss.
            attempt.state = TerminalForegroundDecisionState.ABORT_REQUESTED
            attempt.adoption_allowed = False
            return
        attempt.state = TerminalForegroundDecisionState.SETTLED
        if not attempt.adoption_allowed and attempt.process_id is not None:
            state = self._states.get(attempt.process_id)
            if (
                state is not None
                and state.physical_completion.is_set()
                and state.output.close_reads_if_idle()
            ):
                self._states.pop(attempt.process_id)
        self._decision_attempts.pop(attempt_id, None)
        self._wake_retirement_locked()

    def _spawn(
        self,
        *,
        decision_attempt_id: str,
        command: str,
        cwd: Path,
        tty: bool,
        owner_host_session_id: str,
        shell_argv: tuple[str, ...],
        env: dict[str, str],
        origin: TerminalProcessOrigin,
    ) -> _ProcessState:
        with self._lock:
            process_id = random_id("process")
            while process_id in self._states or any(
                attempt.process_id == process_id
                for attempt in self._decision_attempts.values()
            ):
                process_id = random_id("process")
            # Reserve in the already-held foreground decision before spawning.
            # Another pending launch must see this identity as occupied too.
            decision = self._decision_attempts[decision_attempt_id]
            decision.process_id = process_id
            scope = create_scope()
            decision.interop_scope = scope
            if scope is not None and (
                decision.state is TerminalForegroundDecisionState.ABORT_REQUESTED
                or self._closed
                or owner_host_session_id in self._released_owners
            ):
                scope.request_cancel()
        output = TerminalOutputOwner(
            owner_epoch=owner_host_session_id,
            process_id=process_id,
            retained_bytes_changed=self._retained_bytes_changed,
        )
        master_fd: int | None = None
        process: subprocess.Popen[bytes] | None = None
        stdin: IO[bytes] | None = None
        try:
            if scope is not None:
                scope.prepare()
                env = scope.environment(env)
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
                input_fd = os.dup(master_fd)
                try:
                    stdin = os.fdopen(input_fd, "wb", buffering=0)
                except BaseException:
                    os.close(input_fd)
                    raise
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
                interop_scope=scope,
            )
        except BaseException:
            # Until a state is returned, this local frame owns every descriptor
            # and any child already created.  Do not release its reservation
            # while a failed fd/thread/state construction leaves an invisible child.
            if scope is not None:
                scope.request_cancel()
            if process is not None:
                _terminate_spawned_process_group(process)
            for stream in (
                stdin,
                None if process is None else process.stdin,
                None if process is None else process.stdout,
                None if process is None else process.stderr,
            ):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if master_fd is not None:
                os.close(master_fd)
            if process is not None:
                process.wait()
                while _process_group_exists(process.pid):
                    sleep(0.01)
            if scope is not None:
                scope.finish(cancel=True)
                scope.retire()
            raise

    def _start_physical_threads(self, state: _ProcessState) -> None:
        state.reader.start()
        state.reader_started = True
        watcher = Thread(
            target=self._watch_process,
            args=(state,),
            name=f"terminal-watcher:{state.process_id}",
            daemon=False,
        )
        state.watcher = watcher
        watcher.start()
        state.watcher_started = True

    def _install_spawned_process(
        self,
        state: _ProcessState,
        *,
        decision_attempt_id: str,
        owner_host_session_id: str,
        live_subscription: str | None,
        output_subscriber: OutputSubscriber | None,
        max_lifetime_seconds: int | None,
    ) -> tuple[str | None, bool, bool, bool]:
        """Install every post-spawn physical owner under one rollback boundary.

        The returned booleans are ``closed``, ``decision_aborted`` and
        ``launch_reservation_released``.  Any exception leaves the launch
        reservation installed so the caller's single rollback path owns it.
        """

        if output_subscriber is not None:
            if live_subscription is None:
                raise RuntimeError("terminal output subscription identity is absent")
            state.output.install_subscription(live_subscription, output_subscriber)
        closed_during_launch = False
        decision_aborted = False
        with self._launch_condition:
            if self._closed or owner_host_session_id in self._released_owners:
                closed_during_launch = True
            else:
                current_decision = self._decision_attempts.get(decision_attempt_id)
                if (
                    current_decision is None
                    or current_decision.state
                    is TerminalForegroundDecisionState.ABORT_REQUESTED
                ):
                    decision_aborted = True
                else:
                    if state.process_id in self._states:
                        raise RuntimeError(
                            "IDENTITY_CONFLICT: process identity occupied after spawn"
                        )
                    current_decision.state = (
                        TerminalForegroundDecisionState.PROCESS_INSTALLED
                    )
                    current_decision.process_id = state.process_id
                    self._states[state.process_id] = state
                    self._wake_retirement_locked()
            # A visible state is published only while the same lock excludes
            # release_owner().  If any start below raises, the caller removes
            # that exact publication before releasing the launch reservation.
            self._start_physical_threads(state)
            if (
                max_lifetime_seconds is not None
                and not closed_during_launch
                and not decision_aborted
            ):
                timer = Timer(
                    max_lifetime_seconds, self._expire, args=(state.process_id,)
                )
                timer.daemon = False
                state.deadline_timer = timer
                timer.start()
                state.deadline_timer_started = True
            if not closed_during_launch:
                self._release_launch_locked(owner_host_session_id)
                return live_subscription, False, decision_aborted, True
        return live_subscription, True, decision_aborted, False

    def _watch_process(self, state: _ProcessState) -> None:
        while True:
            try:
                code = state.process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if (
                    state.interop_scope is not None
                    and state.interop_scope.failed.is_set()
                ):
                    _terminate_process_group(state)
        with state.lock:
            state.physical_state = TerminalPhysicalState.TERMINALIZING
        state.reader.join()
        # The shell leader is not the physical process boundary.  A detached
        # descendant can remain in the same group after closing or redirecting
        # stdout.  Completion, capacity release and monitor wake all wait for
        # the exact group to disappear.
        while _process_group_exists(state.process.pid):
            if state.interop_scope is not None and state.interop_scope.failed.is_set():
                _terminate_process_group(state)
            sleep(0.01)
        if state.interop_scope is not None:
            try:
                state.interop_scope.finish()
            except Exception:
                # Without an exact Windows join, keep the original state and
                # capacity occupied. Host close reports the physical failure.
                return
        with state.lock:
            state.ended_at = monotonic()
            status, exit_code = _freeze_final_outcome_locked(state, code=code)
        state.output.finalize(status=status.value, exit_code=exit_code)
        if state.deadline_timer is not None:
            state.deadline_timer.cancel()
        with state.lock:
            if state.physical_state is not TerminalPhysicalState.PRUNABLE:
                state.physical_state = TerminalPhysicalState.PHYSICALLY_JOINED
        # This is the sole normal physical-completion publication.  It is
        # intentionally after group exit, reader join, sanitizer finalization,
        # but before a best-effort external subscriber can
        # delay waiters or retain process capacity.
        state.physical_completion.set()
        with self._launch_condition:
            self._wake_retirement_locked()
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

    @contextmanager
    def _owned(
        self, process_id: str, owner: str, *, cleanup: bool = False
    ) -> Iterator[_ProcessState]:
        require_id(process_id, "process")
        # Lock order is Registry -> output; output callbacks run after releasing
        # the output lock. Never hold either lock over a wait or physical join.
        with self._lock:
            state = self._states.get(process_id)
            if (
                state is None
                or state.owner_host_session_id != owner
                or (not cleanup and (self._closed or owner in self._released_owners))
            ):
                raise KeyError(process_id)
            protection = state.output.reading()
            protection.__enter__()
        try:
            state.refresh()
            yield state
        finally:
            protection.__exit__(None, None, None)

    def output_owner(
        self, process_id: str, *, owner_host_session_id: str
    ) -> TerminalOutputOwner:
        with self._owned(process_id, owner_host_session_id) as state:
            return state.output

    def snapshot_and_subscribe(
        self,
        process_id: str,
        *,
        owner_host_session_id: str,
        callback: OutputSubscriber,
        maximum_chars: int = 32_000,
    ) -> tuple[TerminalOutputSnapshot, str]:
        with self._owned(process_id, owner_host_session_id) as state:
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
        with self._owned(process_id, owner_host_session_id, cleanup=True) as state:
            return state.output.unsubscribe(token)

    def poll(
        self,
        process_id: str,
        *,
        max_output_chars: int,
        owner_host_session_id: str,
        since_cursor: str | None = None,
    ) -> TerminalResult:
        with self._owned(process_id, owner_host_session_id) as state:
            return _snapshot(state, max_output_chars, since_cursor=since_cursor)

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
        with self._owned(process_id, owner_host_session_id) as state:
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
        yield_time_ms: int,
        max_output_chars: int,
        owner_host_session_id: str,
    ) -> TerminalResult:
        with self._owned(process_id, owner_host_session_id) as state:
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
            # Reuse the same physical boundary as initial yield/wait. Output or PTY
            # echo does not establish that an interactive response is complete.
            state.physical_completion.wait(yield_time_ms / 1000)
            return _snapshot(state, max_output_chars)

    def close_stdin(
        self, process_id: str, *, max_output_chars: int, owner_host_session_id: str
    ) -> TerminalResult:
        with self._owned(process_id, owner_host_session_id) as state:
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
        termination = self.terminate_if_running(
            process_id,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )
        if (
            termination.disposition
            is TerminalTerminationDisposition.PHYSICAL_SETTLEMENT_INCOMPLETE
        ):
            raise ProcessPhysicalJoinError("terminal process did not physically join")
        return termination.result

    def terminate_if_running(
        self,
        process_id: str,
        *,
        max_output_chars: int,
        owner_host_session_id: str,
        join_timeout_seconds: float = 2.0,
    ) -> TerminalTerminationResult:
        with self._owned(process_id, owner_host_session_id) as state:
            with state.lock:
                already_terminal = (
                    state.final_status is not None and state.physical_completion.is_set()
                )
            if already_terminal:
                return TerminalTerminationResult(
                    TerminalTerminationDisposition.ALREADY_TERMINAL,
                    _snapshot(state, max_output_chars),
                    state.physical_state.value,
                    False,
                )

            accepted = _accept_termination_intent(state, TerminalStatus.KILLED)
            termination_attempted = accepted or _process_is_live(state)
            if termination_attempted:
                _terminate_process_group(state)
            joined = _join_physical(state, timeout=join_timeout_seconds)
            group_alive = _process_is_live(state)
            if joined and not termination_attempted:
                # A naturally terminal process, or a process already owned by an
                # earlier terminal settlement, received neither a new intent nor
                # a signal attempt from this invocation.  Physical joining cannot
                # be used as evidence that this request performed termination.
                disposition = TerminalTerminationDisposition.ALREADY_TERMINAL
            elif joined:
                disposition = TerminalTerminationDisposition.TERMINATION_COMPLETED
            else:
                disposition = TerminalTerminationDisposition.PHYSICAL_SETTLEMENT_INCOMPLETE
            return TerminalTerminationResult(
                disposition,
                _snapshot(state, max_output_chars),
                state.physical_state.value,
                group_alive,
            )

    def get_process_info(
        self, process_id: str, *, owner_host_session_id: str
    ) -> TerminalProcessInfo:
        """Observe owned metadata through the same read-only path as inventory."""

        with self._owned(process_id, owner_host_session_id) as state:
            return _info(state)

    def list_processes(
        self,
        *,
        owner_host_session_id: str,
        include_finished: bool,
        include_running: bool,
    ) -> list[TerminalProcessInfo]:
        with ExitStack() as reads:
            with self._lock:
                if self._closed or owner_host_session_id in self._released_owners:
                    return []
                states = tuple(
                    state
                    for state in self._states.values()
                    if state.owner_host_session_id == owner_host_session_id
                )
                for state in states:
                    reads.enter_context(state.output.reading())
            result: list[TerminalProcessInfo] = []
            for state in states:
                state.refresh()
                running = _process_is_live(state)
                if running and include_running or not running and include_finished:
                    result.append(_info(state))
            return sorted(result, key=lambda item: item.started_at_monotonic)

    def list_background_processes(
        self, *, owner_host_session_id: str
    ) -> list[TerminalProcessInfo]:
        return [
            item
            for item in self.list_processes(
                owner_host_session_id=owner_host_session_id,
                include_finished=True,
                include_running=True,
            )
            if item.background_adopted
        ]

    def freeze_compaction_handoff(
        self, *, owner_host_session_id: str
    ) -> tuple[TerminalProcessInfo, ...]:
        """Freeze running public process facts without reading retained output."""

        return tuple(sorted(
            self.list_processes(
                owner_host_session_id=owner_host_session_id,
                include_finished=False,
                include_running=True,
            ),
            key=lambda item: item.process_id,
        ))

    def log(
        self,
        process_id: str,
        *,
        max_output_chars: int,
        owner_host_session_id: str,
        since_cursor: str | None = None,
    ) -> TerminalProcessLog:
        with self._owned(process_id, owner_host_session_id) as state:
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
        with self._owned(process_id, owner_host_session_id) as state:
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

    def finalize_yield_decision(self, state: _ProcessState, *, yielded: bool) -> None:
        with state.lock:
            if state.yield_decision is not None:
                raise RuntimeError("terminal yield decision is already installed")
            state.yield_decision = yielded

    def release_owner(
        self, owner: str, *, timeout_seconds: float
    ) -> list[TerminalResult]:
        if timeout_seconds <= 0:
            raise ValueError("terminal close timeout must be positive")
        deadline = monotonic() + timeout_seconds
        with self._launch_condition:
            self._released_owners.add(owner)
            self._launch_condition.notify_all()
            decision_ids = tuple(
                attempt_id
                for attempt_id, attempt in self._decision_attempts.items()
                if attempt.handle.owner_host_session_id == owner
            )
        for attempt_id in decision_ids:
            self._abort_foreground_decision(attempt_id)
        with ExitStack() as reads:
            with self._launch_condition:
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
                    reads.enter_context(state.output.reading())
            self._join_uninstalled_scopes(owner, deadline=deadline)
            for state in states:
                _accept_termination_intent(state, TerminalStatus.KILLED)
                _terminate_process_group(state)
            results: list[TerminalResult] = []
            for state in states:
                remaining = deadline - monotonic()
                if remaining <= 0 or not _join_physical(state, timeout=remaining):
                    raise TimeoutError("terminal owner physical close did not finish")
                results.append(_snapshot(state, 32_000))
        for state in states:
            self._remove_released_state(state, deadline=deadline)
        with self._launch_condition:
            for attempt_id in decision_ids:
                attempt = self._decision_attempts.get(attempt_id)
                if (
                    attempt is not None
                    and attempt.state is TerminalForegroundDecisionState.RESULT_READY
                ):
                    self._settle_foreground_decision_locked(attempt_id)
                # An ABORT_REQUESTED attempt still belongs to the physical
                # exec_with_yield caller.  That caller must observe the joined
                # process, freeze the exact killed result, and only then settle
                # the attempt.  Removing it here races _mark_*_result_ready()
                # and turns a known terminal outcome into a generic error.
        self._join_retirement(deadline=deadline)
        return results

    def release_owner_and_join(self, owner: str) -> list[TerminalResult]:
        """Force-path release that never detaches an admitted process owner."""

        with self._launch_condition:
            self._released_owners.add(owner)
            self._launch_condition.notify_all()
            decision_ids = tuple(
                attempt_id
                for attempt_id, attempt in self._decision_attempts.items()
                if attempt.handle.owner_host_session_id == owner
            )
        for attempt_id in decision_ids:
            self._abort_foreground_decision(attempt_id)
        with ExitStack() as reads:
            with self._launch_condition:
                while self._launching_by_owner.get(owner, 0):
                    self._launch_condition.wait()
                states = [
                    state
                    for state in self._states.values()
                    if state.owner_host_session_id == owner
                ]
                for state in states:
                    reads.enter_context(state.output.reading())
            self._join_uninstalled_scopes(owner, deadline=None)
            for state in states:
                _accept_termination_intent(state, TerminalStatus.KILLED)
                _terminate_process_group(state)
            results: list[TerminalResult] = []
            for state in states:
                while not _join_physical(state, timeout=1.0):
                    _terminate_process_group(state)
                results.append(_snapshot(state, 32_000))
        for state in states:
            self._remove_released_state(state, deadline=None)
        with self._launch_condition:
            for attempt_id in decision_ids:
                attempt = self._decision_attempts.get(attempt_id)
                if (
                    attempt is not None
                    and attempt.state is TerminalForegroundDecisionState.RESULT_READY
                ):
                    self._settle_foreground_decision_locked(attempt_id)
        self._join_retirement(deadline=None)
        return results

    def _join_uninstalled_scopes(self, owner: str, *, deadline: float | None) -> None:
        with self._lock:
            attempts = tuple(
                attempt
                for attempt in self._decision_attempts.values()
                if attempt.handle.owner_host_session_id == owner
                and attempt.process_id not in self._states
                and attempt.interop_scope is not None
            )
        for attempt in attempts:
            scope = attempt.interop_scope
            assert scope is not None
            timeout = None if deadline is None else max(0.0, deadline - monotonic())
            if not scope.finish(cancel=True, timeout=timeout):
                raise ProcessPhysicalJoinError("terminal launch scope did not join")
            scope.retire()
            with self._launch_condition:
                self._settle_foreground_decision_locked(attempt.handle.attempt_id)

    def shutdown(self) -> None:
        with self._lock:
            owners = sorted(
                {
                    *(state.owner_host_session_id for state in self._states.values()),
                    *self._launching_by_owner,
                    *(
                        attempt.handle.owner_host_session_id
                        for attempt in self._decision_attempts.values()
                    ),
                }
            )
            self._closed = True
            self._launch_condition.notify_all()
        try:
            for owner in owners:
                try:
                    self.release_owner(owner, timeout_seconds=5.0)
                except TimeoutError:
                    self.release_owner_and_join(owner)
        finally:
            self._join_retirement(deadline=None)

    def _expire(self, process_id: str) -> None:
        with self._lock:
            state = self._states.get(process_id)
        if state is None or not _process_is_live(state):
            return
        _accept_termination_intent(state, TerminalStatus.TIMEOUT)
        _terminate_process_group(state)

    def _wake_retirement_locked(self) -> None:
        self._launch_condition.notify_all()

    def _ensure_retirement(self) -> None:
        while True:
            with self._launch_condition:
                if self._closed or not (self._states or self._launching_by_owner):
                    return
                thread = self._retirement_thread
                if thread is None:
                    thread = Thread(
                        target=self._retire_finished,
                        name=f"terminal-retirement:{self._owner_identity}",
                        daemon=False,
                    )
                    self._retirement_thread = thread
                    self._retirement_stopping = False
                    try:
                        thread.start()
                    except BaseException:
                        self._retirement_thread = None
                        raise
                    return
                if not self._retirement_stopping and thread.is_alive():
                    self._launch_condition.notify_all()
                    return
            # Reap the exact retiring worker outside Registry locks before a
            # replacement is created. Admission racing an empty-worker exit
            # cannot strand new state or create two physical workers.
            thread.join()
            with self._launch_condition:
                if self._retirement_thread is thread:
                    self._retirement_thread = None
                    self._retirement_stopping = False

    def _retire_finished(self) -> None:
        with self._launch_condition:
            while (
                not self._closed
                and not self._retirement_stopping
                and (self._states or self._launching_by_owner)
            ):
                deadlines = [
                    state.ended_at + self.finished_ttl_seconds
                    for state in self._states.values()
                    if state.ended_at is not None
                ]
                delay = min(deadlines) - monotonic() if deadlines else None
                if delay is None or delay > 0:
                    self._launch_condition.wait(delay)
                    continue
                # Join outside Registry/output locks: watcher completion and
                # admitted readers need both owners to make forward progress.
                self._launch_condition.release()
                try:
                    self._prune()
                finally:
                    self._launch_condition.acquire()
                if not self._closed and not self._retirement_stopping and any(
                    state.ended_at is not None
                    and state.ended_at + self.finished_ttl_seconds <= monotonic()
                    for state in self._states.values()
                ):
                    # An output lease/read or incomplete physical join blocked
                    # expiry. This is maintenance frequency, never a lifetime cap.
                    self._launch_condition.wait(1.0)
            self._retirement_stopping = True
            self._launch_condition.notify_all()

    def _prune(self) -> None:
        now = monotonic()
        with self._lock:
            finished = sorted(
                (state.ended_at, process_id, state)
                for process_id, state in self._states.items()
                if state.ended_at is not None
            )
        for ended, process_id, state in finished:
            if now >= ended + self.finished_ttl_seconds:
                self._try_prune(process_id, state)
        with self._lock:
            remaining = sorted(
                (state.ended_at or now, process_id, state)
                for process_id, state in self._states.items()
                if not _process_is_live(state)
            )
            excess = max(0, len(remaining) - self.max_finished_processes)
        # Preserve the original pressure policy: only the oldest excess slots
        # are candidates; a blocked oldest slot does not widen that selection.
        for _ended, process_id, state in remaining[:excess]:
            self._try_prune(process_id, state)

    def _try_prune(self, process_id: str, state: _ProcessState) -> bool:
        with self._lock:
            if (
                self._states.get(process_id) is not state
                or state.owner_host_session_id in self._released_owners
            ) or any(
                attempt.process_id == process_id
                for attempt in self._decision_attempts.values()
            ):
                return False
        if _process_is_live(state) or not _join_physical(state, timeout=0.1):
            return False
        with self._launch_condition:
            if (
                self._states.get(process_id) is not state
                or state.owner_host_session_id in self._released_owners
            ) or any(
                attempt.process_id == process_id
                for attempt in self._decision_attempts.values()
            ):
                return False
            if not _mark_prunable(state):
                return False
            self._states.pop(process_id)
            self._launch_condition.notify_all()
            return True

    def _join_retirement(self, *, deadline: float | None) -> None:
        with self._launch_condition:
            self._launch_condition.notify_all()
            thread = self._retirement_thread
            # A shared Registry still needs its one worker for other owners.
            if not self._closed and (self._states or self._launching_by_owner):
                return
            self._retirement_stopping = True
            self._launch_condition.notify_all()
        if thread is not None and thread is not current_thread():
            timeout = None if deadline is None else max(0.0, deadline - monotonic())
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("terminal retirement worker did not finish")
            with self._launch_condition:
                if self._retirement_thread is thread:
                    self._retirement_thread = None
                    self._retirement_stopping = False

    def _remove_released_state(
        self, state: _ProcessState, *, deadline: float | None
    ) -> None:
        while True:
            with self._launch_condition:
                if self._states.get(state.process_id) is not state:
                    return
                if state.output.close_reads_if_idle():
                    self._states.pop(state.process_id)
                    self._launch_condition.notify_all()
                    return
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("terminal owner output readers did not finish")
            state.output.wait_for_readers(remaining)

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
        self._wake_retirement_locked()


@dataclass(slots=True)
class TerminalManager:
    workspace_root: Path
    max_live_processes: int = 8
    max_finished_processes: int = 32
    finished_ttl_seconds: float = 3600.0
    completion_subscriber: TerminalCompletionSubscriber | None = None
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
        self.process_registry.activate_owner(owner_host_session_id)

    def execute(
        self,
        request: TerminalRequest,
        *,
        owner_host_session_id: str,
        output_subscriber: OutputSubscriber | None = None,
        origin: TerminalProcessOrigin | None = None,
        decision_attempt_id: str | None = None,
        decision_deadline_monotonic: float | None = None,
        cwd_scope: TerminalCwdScope = TerminalCwdScope.WORKSPACE,
    ) -> TerminalResult:
        # A command's cwd is an input, never process-local session memory.
        try:
            cwd = _resolve_workdir(
                request.workdir, workspace=self.workspace_root, scope=cwd_scope
            )
            environment = self.environment_owner.build(cwd=cwd)
        except Exception as exc:
            return TerminalResult(
                status=TerminalStatus.ERROR,
                output="",
                exit_code=-1,
                cwd=str(self.workspace_root),
                error=f"terminal preflight failed ({type(exc).__name__}): {exc}",
            )
        initial_result: TerminalResult | None = None

        def read_initial_result(state: _ProcessState) -> None:
            nonlocal initial_result
            initial_result = _snapshot(state, request.max_output_chars)

        while True:
            attempt_id = decision_attempt_id or random_id("terminal-decision")
            try:
                self.process_registry.exec_with_yield(
                    command=request.command,
                    cwd=cwd,
                    yield_time_ms=request.yield_time_ms,
                    tty=request.tty,
                    max_lifetime_seconds=request.max_lifetime_seconds,
                    owner_host_session_id=owner_host_session_id,
                    shell_argv=environment.shell.command_argv(request.command),
                    env=environment.values,
                    output_subscriber=output_subscriber,
                    origin=origin,
                    decision_attempt_id=attempt_id,
                    decision_deadline_monotonic=decision_deadline_monotonic,
                    initial_result_reader=read_initial_result,
                )
            except ProcessDecisionIdentityConflict:
                # Only this unpublished local allocation may change. Admission
                # did not acquire the occupied slot, so never settle its owner.
                if decision_attempt_id is not None:
                    raise
                if (
                    decision_deadline_monotonic is not None
                    and monotonic() >= decision_deadline_monotonic
                ):
                    raise TimeoutError("terminal decision allocation deadline expired")
                continue
            except ProcessLimitError as exc:
                return TerminalResult(
                    status=TerminalStatus.BLOCKED,
                    output="",
                    exit_code=-1,
                    cwd=str(cwd),
                    error=str(exc),
                    reason=TerminalFailureReason.PROCESS_CAPACITY_EXHAUSTED,
                    shell_diagnostic=environment.diagnostic,
                )
            # Failed launches are settled (or retained for physical joining)
            # by the registry. Only a returned result transfers settlement here.
            try:
                if initial_result is None:
                    raise RuntimeError("terminal initial result was not frozen")
                return replace(
                    initial_result,
                    shell_diagnostic=environment.diagnostic,
                )
            finally:
                self.process_registry.settle_foreground_decision(attempt_id)

    def list_processes(self, **kwargs):
        return self.process_registry.list_processes(**kwargs)

    def get_process_info(
        self, process_id: str, *, owner_host_session_id: str
    ) -> TerminalProcessInfo:
        return self.process_registry.get_process_info(
            process_id, owner_host_session_id=owner_host_session_id
        )

    def freeze_compaction_handoff(self, *, owner_host_session_id: str):
        return self.process_registry.freeze_compaction_handoff(
            owner_host_session_id=owner_host_session_id
        )

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

    def terminate_process_if_running(self, process_id: str, **kwargs):
        return self.process_registry.terminate_if_running(process_id, **kwargs)

    def list_background_processes(self, *, owner_host_session_id: str):
        return self.process_registry.list_background_processes(
            owner_host_session_id=owner_host_session_id
        )

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
        return results

    def release_owner_and_join(
        self, owner_host_session_id: str
    ) -> list[TerminalResult]:
        results = self.process_registry.release_owner_and_join(owner_host_session_id)
        self.environment_owner.close_and_join()
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
    if state.interop_scope is not None:
        state.interop_scope.request_cancel()
    with state.lock:
        if state.physical_state is TerminalPhysicalState.RUNNING:
            state.physical_state = TerminalPhysicalState.TERMINALIZING
    _terminate_spawned_process_group(state.process)


def _terminate_spawned_process_group(process: subprocess.Popen[bytes]) -> None:
    # The session leader may already have exited while a descendant remains in
    # its process group (and may still own stdout).  Always target the group;
    # checking only Popen.poll() would strand that descendant and its reader.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    group_deadline = monotonic() + 0.5
    while _process_group_exists(process.pid) and monotonic() < group_deadline:
        sleep(0.01)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _join_physical(state: _ProcessState, *, timeout: float) -> bool:
    deadline = monotonic() + max(0.0, timeout)
    try:
        state.process.wait(timeout=max(0.0, deadline - monotonic()))
    except subprocess.TimeoutExpired:
        return False
    if state.reader_started:
        state.reader.join(timeout=max(0.0, deadline - monotonic()))
        if state.reader.is_alive():
            return False
    else:
        # A Thread.start() fault still leaves the spawned pipe/PTY source owned
        # by this invisible state.  Close it here so rollback does not strand a
        # descriptor after the child group has been terminated.
        try:
            if state.io_mode is TerminalIOMode.PTY and state.master_fd is not None:
                os.close(state.master_fd)
                state.master_fd = None
            elif state.process.stdout is not None:
                state.process.stdout.close()
        except OSError:
            pass
    watcher = state.watcher
    if (
        watcher is not None
        and state.watcher_started
        and watcher is not current_thread()
    ):
        watcher.join(timeout=max(0.0, deadline - monotonic()))
        if watcher.is_alive():
            return False
    if state.deadline_timer is not None:
        state.deadline_timer.cancel()
        if (
            state.deadline_timer_started
            and state.deadline_timer is not current_thread()
        ):
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
    if state.interop_scope is not None:
        if not state.interop_scope.finish(timeout=max(0.0, deadline - monotonic())):
            return False
        state.interop_scope.retire()
    with state.lock:
        if state.physical_state is not TerminalPhysicalState.PRUNABLE:
            state.physical_state = TerminalPhysicalState.PHYSICALLY_JOINED
        if state.ended_at is None:
            state.ended_at = monotonic()
    if not state.watcher_started:
        state.physical_completion.set()
    return True


def _mark_prunable(state: _ProcessState) -> bool:
    # Physical join has completed outside Registry locks. Registry holds its
    # lock here; close_reads_if_idle atomically fences even old output handles.
    if not state.output.close_reads_if_idle():
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

    return (
        state.process.poll() is None
        or _process_group_exists(state.process.pid)
        or (
            state.interop_scope is not None
            and not state.interop_scope.physically_joined.is_set()
        )
    )


def _accept_termination_intent(state: _ProcessState, reason: TerminalStatus) -> bool:
    """Linearize one terminal intent without overwriting a frozen outcome."""

    if reason not in {TerminalStatus.KILLED, TerminalStatus.TIMEOUT}:
        raise ValueError("terminal termination intent is invalid")
    with state.lock:
        if state.final_status is not None or not _process_is_live(state):
            if state.final_status is None:
                _freeze_final_outcome_locked(state, code=state.process.poll())
            return False
        if state.termination_intent is None:
            state.termination_intent = reason
            return True
        return False


def _freeze_final_outcome_locked(
    state: _ProcessState, *, code: int | None
) -> tuple[TerminalStatus, int]:
    """Freeze the sole status/exit pair while ``state.lock`` is held."""

    if state.final_status is None:
        if state.termination_intent is TerminalStatus.TIMEOUT:
            state.final_status = TerminalStatus.TIMEOUT
            state.final_exit_code = _TIMEOUT_EXIT_CODE
        elif state.termination_intent is TerminalStatus.KILLED:
            state.final_status = TerminalStatus.KILLED
            state.final_exit_code = -1 if code is None else code
        else:
            state.final_status = (
                TerminalStatus.SUCCESS
                if code == 0
                and not (
                    state.interop_scope is not None and state.interop_scope.failure
                )
                else TerminalStatus.ERROR
            )
            state.final_exit_code = (
                -1
                if code is None
                or (state.interop_scope is not None and state.interop_scope.failure)
                else code
            )
    assert state.final_exit_code is not None
    return state.final_status, state.final_exit_code


def _occupies_live_capacity(state: _ProcessState) -> bool:
    """Retain capacity until the complete process-local physical boundary."""

    return not state.physical_completion.is_set()


def _status(state: _ProcessState) -> TerminalStatus:
    if _process_is_live(state):
        return TerminalStatus.RUNNING
    with state.lock:
        status, _exit_code = _freeze_final_outcome_locked(
            state, code=state.process.poll()
        )
        return status


def _info(state: _ProcessState) -> TerminalProcessInfo:
    state.refresh()
    output = state.output.snapshot(maximum_chars=1)
    ended = state.ended_at
    status = _status(state)
    exit_code: int | None = None
    if status is not TerminalStatus.RUNNING:
        with state.lock:
            _frozen_status, exit_code = _freeze_final_outcome_locked(
                state, code=state.process.poll()
            )
    return TerminalProcessInfo(
        process_id=state.process_id,
        command=state.command,
        cwd=str(state.cwd),
        backend_type="local",
        io_mode=state.io_mode.value,
        status=status.value,
        exit_code=exit_code,
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
        background_adopted=state.background_adopted,
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
    if status is not TerminalStatus.RUNNING:
        with state.lock:
            _frozen_status, code = _freeze_final_outcome_locked(state, code=code)
    snapshot, artifact_candidate = state.output.snapshot_with_artifact_candidate(
        maximum_chars=maximum_chars,
        since_cursor=since_cursor,
    )
    return TerminalResult(
        status=status,
        output=snapshot.text,
        exit_code=(
            code if status is not TerminalStatus.RUNNING and code is not None else -1
        ),
        cwd=str(state.cwd),
        timed_out=state.timed_out,
        truncated=snapshot.gap_before_output or snapshot.truncated_by_response_bound,
        error=(
            state.interop_scope.failure
            if state.interop_scope is not None and state.interop_scope.failure
            else None
            if status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
            else status.value
        ),
        process_id=state.process_id,
        output_artifact_candidate=artifact_candidate,
        output_disposition=snapshot.disposition,
        output_cursor=snapshot.output_cursor,
        retained_from_cursor=snapshot.retained_from_cursor,
        gap_before_output=snapshot.gap_before_output,
        truncated_by_response_bound=snapshot.truncated_by_response_bound,
        source_coverage=snapshot.source_coverage,
        trusted_process_duration_microseconds=max(
            0, int(((state.ended_at or monotonic()) - state.started_at) * 1_000_000)
        ),
    )


def _resolve_workdir(
    raw: str | None,
    *,
    workspace: Path,
    scope: TerminalCwdScope,
) -> Path:
    candidate = workspace if not raw else Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if scope is TerminalCwdScope.WORKSPACE and (
        resolved != workspace and workspace not in resolved.parents
    ):
        raise ValueError("terminal workdir must remain inside workspace")
    if scope not in {TerminalCwdScope.WORKSPACE, TerminalCwdScope.HOST_LOCAL}:
        raise ValueError("terminal workdir scope is invalid")
    if not resolved.is_dir():
        raise ValueError("terminal workdir does not exist")
    return resolved


__all__ = [
    "ProcessInputError",
    "ProcessLimitError",
    "ProcessPhysicalJoinError",
    "ProcessRegistry",
    "TerminalManager",
]
