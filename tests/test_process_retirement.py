"""Process retention is an owner-local deadline, never an execution timeout."""
from __future__ import annotations

from contextlib import contextmanager
import os
from queue import Queue
import sys
from threading import Event, Thread
from time import monotonic

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_process import manager as M
from pulsara_agent.terminal_process.models import TerminalPhysicalState, TerminalTerminationDisposition


OWNER = derive_id("host", "process-retirement-tests", "owner")


def launch(registry, path, *, owner=OWNER, program="print('retained result')"):
    registry.activate_owner(owner)
    attempt = derive_id("terminal-decision", "process-retirement-tests", str(monotonic()))
    state, _ = registry.exec_with_yield(
        command=program, cwd=path, yield_time_ms=2_000,
        tty=False, max_lifetime_seconds=None, owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", program), env=dict(os.environ),
        decision_attempt_id=attempt, decision_deadline_monotonic=M.monotonic() + 5,
    )
    registry.settle_foreground_decision(attempt)
    return state


def set_clock(monkeypatch, registry, value):
    now = [value]
    monkeypatch.setattr(M, "monotonic", lambda: now[0])
    return now


def wake(registry):
    with registry._launch_condition:
        registry._launch_condition.notify_all()


def await_removed(registry, state):
    with registry._launch_condition:
        assert registry._launch_condition.wait_for(
            lambda: state.process_id not in registry._states, timeout=5
        )


@contextmanager
def owned_registry(**kwargs):
    registry = M.ProcessRegistry(**kwargs)
    try:
        yield registry
    finally:
        registry.shutdown()


def observe_waits(monkeypatch, registry):
    waits = Queue()
    original = registry._launch_condition.wait

    def wait(timeout=None):
        waits.put(timeout)
        return original(timeout)

    monkeypatch.setattr(registry._launch_condition, "wait", wait)
    return waits


def next_wait(waits, expected):
    while True:
        value = waits.get(timeout=5)
        if value == expected:
            return


def test_expiry_uses_ended_at_exact_deadline_without_another_exec(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        now = set_clock(monkeypatch, registry, state.ended_at + 3599)
        waits = observe_waits(monkeypatch, registry)
        owner = registry.output_owner(state.process_id, owner_host_session_id=OWNER)
        original = registry.poll(state.process_id, max_output_chars=512, owner_host_session_id=OWNER)
        wake(registry)
        next_wait(waits, 1.0)
        assert registry.get_process_info(state.process_id, owner_host_session_id=OWNER).status == "success"
        assert original.output == "retained result\n"
        assert original.source_coverage is not None
        now[0] += 1
        wake(registry)
        await_removed(registry, state)
        assert state.physical_state is TerminalPhysicalState.PRUNABLE
        with pytest.raises(KeyError):
            registry.poll(state.process_id, max_output_chars=512, owner_host_session_id=OWNER)
        with pytest.raises(KeyError):
            owner.snapshot(maximum_chars=512)
        with pytest.raises(KeyError):
            owner.subscribe(lambda *_: None)


def test_lease_blocks_expiry_then_automatic_one_second_retry(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        _snapshot, token = registry.snapshot_and_subscribe(
            state.process_id, owner_host_session_id=OWNER, callback=lambda *_: None,
        )
        waits = observe_waits(monkeypatch, registry)
        set_clock(monkeypatch, registry, state.ended_at + 3600)
        wake(registry)
        next_wait(waits, 1.0)
        assert state.process_id in registry._states
        assert registry.unsubscribe_output(state.process_id, token, owner_host_session_id=OWNER)
        # No Registry notification and no new command: the bounded retry itself
        # must notice the output owner releasing its existing lease.
        await_removed(registry, state)


@pytest.mark.parametrize("operation", ["metadata", "list", "poll", "wait", "log", "observation", "subscribe", "handoff"])
def test_admitted_reader_blocks_prune_until_whole_operation_finishes(tmp_path, monkeypatch, operation):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        admitted, resume = Event(), Event()
        original_reading = state.output.reading

        @contextmanager
        def reading():
            with original_reading():
                yield
                # This pause is beyond Registry lookup, while the original
                # inflight count still protects this operation's entire result.
                if not admitted.is_set():
                    admitted.set()
                    assert resume.wait(5)

        monkeypatch.setattr(state.output, "reading", reading)
        values, failures = [], []
        subscription = []

        def read():
            try:
                args = dict(owner_host_session_id=OWNER)
                if operation == "metadata":
                    value = registry.get_process_info(state.process_id, **args)
                elif operation == "list":
                    value = registry.list_processes(include_running=True, include_finished=True, **args)
                elif operation == "poll":
                    value = registry.poll(state.process_id, max_output_chars=512, **args)
                elif operation == "wait":
                    value = registry.wait(state.process_id, timeout_seconds=0, max_output_chars=512, **args)
                elif operation == "log":
                    value = registry.log(state.process_id, max_output_chars=512, **args)
                elif operation == "observation":
                    value = registry.observation_slice(state.process_id, maximum_chars=512, maximum_bytes=2048, since_cursor=None, **args)
                elif operation == "subscribe":
                    value, token = registry.snapshot_and_subscribe(state.process_id, callback=lambda *_: None, **args)
                    subscription.append(token)
                else:
                    value = registry.freeze_compaction_handoff(**args)
                values.append(value)
            except BaseException as error:
                failures.append(error)

        reader = Thread(target=read)
        reader.start()
        try:
            assert admitted.wait(5)
            assert state.output.inflight_read_count == 1
            waits = observe_waits(monkeypatch, registry)
            set_clock(monkeypatch, registry, state.ended_at + 3600)
            wake(registry)
            next_wait(waits, 1.0)
            assert state.process_id in registry._states
        finally:
            resume.set()
            reader.join(5)
        assert not reader.is_alive()
        assert failures == []
        assert len(values) == 1
        for token in subscription:
            registry.unsubscribe_output(state.process_id, token, owner_host_session_id=OWNER)
        wake(registry)
        await_removed(registry, state)


def test_join_outside_registry_lock_and_exact_state_revalidated(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        entered, resume = Event(), Event()
        original = M._join_physical

        def join(candidate, *, timeout):
            if timeout == 0.1:
                entered.set()
                assert resume.wait(5)
            return original(candidate, timeout=timeout)

        monkeypatch.setattr(M, "_join_physical", join)
        set_clock(monkeypatch, registry, state.ended_at + 3600)
        wake(registry)
        assert entered.wait(5)
        # Admission succeeds while physical joining is paused; final count and
        # exact identity must be checked again after that lock-free work.
        with registry._owned(state.process_id, OWNER):
            resume.set()
            with registry._launch_condition:
                assert registry._launch_condition.wait_for(
                    lambda: state.output.inflight_read_count == 1, timeout=5
                )
            assert state.process_id in registry._states
        wake(registry)
        await_removed(registry, state)


def test_physical_join_failure_is_retried_without_spinning(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        original = M._join_physical
        attempts = []
        allow = Event()

        def join(candidate, *, timeout):
            if timeout == 0.1:
                attempts.append(candidate)
                if not allow.is_set():
                    return False
            return original(candidate, timeout=timeout)

        monkeypatch.setattr(M, "_join_physical", join)
        waits = observe_waits(monkeypatch, registry)
        set_clock(monkeypatch, registry, state.ended_at + 3600)
        wake(registry)
        next_wait(waits, 1.0)
        assert len(attempts) == 1
        assert state.process_id in registry._states
        allow.set()
        await_removed(registry, state)
        assert len(attempts) == 2


@pytest.mark.parametrize("release", ["release_owner", "release_owner_and_join", "shutdown"])
def test_all_close_paths_join_worker_and_wait_for_admitted_reads(tmp_path, monkeypatch, release):
    registry = M.ProcessRegistry()
    try:
        state = launch(registry, tmp_path)
        worker = registry._retirement_thread
        assert worker is not None and worker.is_alive()
        assert worker.daemon is False
        admitted = registry._owned(state.process_id, OWNER)
        admitted.__enter__()
        started, done = Event(), Event()
        failures = []
        original = registry._remove_released_state

        def remove(candidate, *, deadline):
            started.set()
            return original(candidate, deadline=deadline)

        monkeypatch.setattr(registry, "_remove_released_state", remove)

        def close():
            try:
                if release == "release_owner":
                    registry.release_owner(OWNER, timeout_seconds=5)
                elif release == "release_owner_and_join":
                    registry.release_owner_and_join(OWNER)
                else:
                    registry.shutdown()
            except BaseException as error:
                failures.append(error)
            finally:
                done.set()

        closer = Thread(target=close)
        closer.start()
        try:
            assert started.wait(5)
            assert not done.is_set()
            assert state.process_id in registry._states
            with pytest.raises(KeyError):
                registry.get_process_info(state.process_id, owner_host_session_id=OWNER)
        finally:
            admitted.__exit__(None, None, None)
        closer.join(5)
        assert done.is_set()
        assert failures == []
        assert not worker.is_alive()
        assert not registry._states
        if release == "shutdown":
            with pytest.raises(RuntimeError):
                registry.activate_owner(OWNER)
        else:
            # Ordinary release is reusable, unlike explicit Registry shutdown.
            state2 = launch(registry, tmp_path)
            assert registry._retirement_thread is not worker
            assert registry.get_process_info(state2.process_id, owner_host_session_id=OWNER).status == "success"
    finally:
        registry.shutdown()


def test_shared_registry_owner_release_keeps_other_owner_worker(tmp_path):
    with owned_registry() as registry:
        first = launch(registry, tmp_path)
        other_owner = derive_id("host", "process-retirement-tests", "other")
        second = launch(registry, tmp_path, owner=other_owner)
        worker = registry._retirement_thread
        registry.release_owner(OWNER, timeout_seconds=5)
        assert first.process_id not in registry._states
        assert registry._retirement_thread is worker
        assert worker.is_alive()
        assert registry.get_process_info(second.process_id, owner_host_session_id=other_owner).status == "success"


def test_pressure_preserves_oldest_excess_selection_and_coverage(tmp_path):
    with owned_registry(max_finished_processes=32) as registry:
        states = [launch(registry, tmp_path) for _ in range(33)]
        # Launch opportunism applies before the new process is added; 33
        # retained records are therefore permitted until the next attempt.
        assert len(registry._states) == 33
        token, _ = states[0].output.subscribe(lambda *_: None)
        try:
            registry._prune()
            assert len(registry._states) == 33
            assert states[0].process_id in registry._states
            assert states[1].process_id in registry._states
        finally:
            states[0].output.unsubscribe(token)
        before = states[1].output.snapshot(maximum_chars=512)
        registry._prune()
        assert len(registry._states) == 32
        assert states[0].process_id not in registry._states
        after = states[1].output.snapshot(maximum_chars=512)
        assert after == before


def test_reaccept_during_worker_exit_joins_exact_old_thread_before_restarting(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        exiting, finish_exit, installed = Event(), Event(), Event()
        original_worker = registry._retire_finished
        original_install = registry._install_spawned_process
        workers = []

        def retire():
            from threading import current_thread
            workers.append(current_thread())
            original_worker()
            if len(workers) == 1:
                exiting.set()
                assert finish_exit.wait(5)

        def install(*args, **kwargs):
            value = original_install(*args, **kwargs)
            installed.set()
            return value

        monkeypatch.setattr(registry, "_retire_finished", retire)
        first = launch(registry, tmp_path)
        old_thread = registry._retirement_thread
        now = set_clock(monkeypatch, registry, first.ended_at + 3600)
        wake(registry)
        assert exiting.wait(5)
        assert old_thread.is_alive()
        # Existing owner may legally reaccept after its record expired. The
        # next launch reaches physical publication before trying to reap the
        # exiting worker; that join must release Registry locks.
        monkeypatch.setattr(registry, "_install_spawned_process", install)
        values, failures = [], []

        def accept():
            try:
                values.append(launch(registry, tmp_path))
            except BaseException as error:
                failures.append(error)

        admission = Thread(target=accept)
        admission.start()
        try:
            assert installed.wait(5)
            with registry._lock:
                assert registry._retirement_thread is old_thread
                assert len(registry._states) == 1
            assert len(workers) == 1
        finally:
            finish_exit.set()
            admission.join(5)
        assert not admission.is_alive()
        assert failures == []
        assert len(values) == 1
        assert not old_thread.is_alive()
        assert registry._retirement_thread is not old_thread
        assert len(workers) == 2
        now[0] += 3600
        wake(registry)
        await_removed(registry, values[0])


def test_join_rechecks_exact_registry_object_before_closing_old_output(tmp_path, monkeypatch):
    from dataclasses import replace
    from pulsara_agent.terminal_process.output import TerminalOutputOwner

    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path)
        original = M._join_physical
        entered, resume = Event(), Event()

        def join(candidate, *, timeout):
            if candidate is state and timeout == 0.1:
                entered.set()
                assert resume.wait(5)
            return original(candidate, timeout=timeout)

        monkeypatch.setattr(M, "_join_physical", join)
        now = set_clock(monkeypatch, registry, state.ended_at + 3600)
        wake(registry)
        assert entered.wait(5)
        replacement = replace(
            state,
            output=TerminalOutputOwner(owner_epoch=OWNER, process_id=state.process_id),
            ended_at=now[0],
        )
        replacement.output.append_raw(b"exact replacement")
        replacement.output.finalize(status="success", exit_code=0)
        with registry._lock:
            registry._states[state.process_id] = replacement
        resume.set()
        # Synchronize with the worker's next future-deadline wait, after its
        # old-state join returned and exact identity recheck rejected removal.
        waits = observe_waits(monkeypatch, registry)
        wake(registry)
        next_wait(waits, 3600)
        assert registry._states[state.process_id] is replacement
        assert state.output.snapshot(maximum_chars=512).text == "retained result\n"
        assert registry.poll(state.process_id, max_output_chars=512, owner_host_session_id=OWNER).output == "exact replacement"


def test_expired_record_never_signals_a_still_physical_process(tmp_path, monkeypatch):
    with owned_registry(finished_ttl_seconds=3600) as registry:
        state = launch(registry, tmp_path, program="import time; time.sleep(30)")
        # A stale terminal timestamp cannot override the physical owner: both
        # expiry and pressure candidates still use the real group/interop test.
        state.ended_at = monotonic() - 3600
        signalled = []
        with monkeypatch.context() as patch:
            patch.setattr(M, "_terminate_process_group", lambda candidate: signalled.append(candidate))
            registry._prune()
            assert state.process.poll() is None
            assert state.process_id in registry._states
            assert signalled == []


def test_normal_close_timeout_can_be_force_joined_after_reader_settles(tmp_path, monkeypatch):
    with owned_registry() as registry:
        state = launch(registry, tmp_path)
        worker = registry._retirement_thread
        now = set_clock(monkeypatch, registry, monotonic())
        read = registry._owned(state.process_id, OWNER)
        read.__enter__()
        original = registry._remove_released_state

        def expired(candidate, *, deadline):
            now[0] = deadline + 1
            return original(candidate, deadline=deadline)

        with monkeypatch.context() as patch:
            patch.setattr(registry, "_remove_released_state", expired)
            with pytest.raises(TimeoutError, match="output readers"):
                registry.release_owner(OWNER, timeout_seconds=5)
        assert worker.is_alive()
        assert state.process_id in registry._states
        read.__exit__(None, None, None)
        registry.release_owner_and_join(OWNER)
        assert not worker.is_alive()
        assert not registry._states


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("ordering", ["initial_first", "release_first"])
def test_initial_and_release_results_keep_admitted_snapshot_readable(tmp_path, monkeypatch, force, ordering):
    from threading import current_thread
    from pulsara_agent.terminal_process.models import TerminalRequest

    manager = M.TerminalManager(tmp_path)
    registry = manager.process_registry
    manager.activate_owner(OWNER)
    initial_entered, resume_initial = Event(), Event()
    release_entered, resume_release = Event(), Event()
    removal_entered, initial_done, release_done = Event(), Event(), Event()
    results, closes, initial_errors, close_errors = [], [], [], []
    snapshot = M._snapshot
    mark_ready = registry._mark_foreground_decision_result_ready
    remove = registry._remove_released_state

    def observe_snapshot(state, *args, **kwargs):
        name = current_thread().name
        if ordering == "initial_first" and name == "initial-result":
            initial_entered.set()
            assert resume_initial.wait(5)
        elif ordering == "release_first" and name == "release-result":
            release_entered.set()
            assert resume_release.wait(5)
        return snapshot(state, *args, **kwargs)

    def observe_ready(*args, **kwargs):
        if ordering == "release_first" and current_thread().name == "initial-result":
            initial_entered.set()
            assert resume_initial.wait(5)
        return mark_ready(*args, **kwargs)

    def observe_remove(*args, **kwargs):
        removal_entered.set()
        return remove(*args, **kwargs)

    monkeypatch.setattr(M, "_snapshot", observe_snapshot)
    monkeypatch.setattr(registry, "_mark_foreground_decision_result_ready", observe_ready)
    monkeypatch.setattr(registry, "_remove_released_state", observe_remove)

    def execute():
        try:
            results.append(manager.execute(
                TerminalRequest(command="printf 'initial result'", yield_time_ms=2_000),
                owner_host_session_id=OWNER,
                decision_deadline_monotonic=monotonic() + 10,
            ))
        except BaseException as error:
            initial_errors.append(error)
        finally:
            initial_done.set()

    def release():
        try:
            if force:
                closes.extend(registry.release_owner_and_join(OWNER))
            else:
                closes.extend(registry.release_owner(OWNER, timeout_seconds=5))
        except BaseException as error:
            close_errors.append(error)
        finally:
            release_done.set()

    initial = Thread(target=execute, name="initial-result")
    closer = Thread(target=release, name="release-result")
    initial.start()
    try:
        assert initial_entered.wait(5)
        closer.start()
        if ordering == "initial_first":
            assert removal_entered.wait(5)
            # Closing may freeze its own result, but cannot retire a Process
            # whose already-admitted first result has not finished reading.
            assert not release_done.is_set()
            resume_initial.set()
        else:
            assert release_entered.wait(5)
            resume_initial.set()
            assert initial_done.wait(5)
            # The first result's finally settles the aborted decision while
            # release still owns an admitted read on the same exact state.
            resume_release.set()
    finally:
        resume_initial.set()
        resume_release.set()
        initial.join(5)
        if closer.ident is not None:
            closer.join(5)
        registry.shutdown()
        manager.environment_owner.close_and_join()
    assert not initial.is_alive() and not closer.is_alive()
    assert initial_errors == []
    assert close_errors == []
    assert len(results) == len(closes) == 1
    assert results[0].output == closes[0].output == "initial result"
    assert results[0].source_coverage == closes[0].source_coverage
    assert registry._states == {}
    assert registry._decision_attempts == {}
    assert registry._retirement_thread is None


def test_failed_initial_result_reader_preserves_finished_record_and_releases_its_users(tmp_path):
    with owned_registry() as registry:
        registry.activate_owner(OWNER)
        observed = []
        failure = RuntimeError("initial snapshot could not be frozen")

        def fail(state):
            observed.append(state)
            raise failure

        with pytest.raises(RuntimeError) as caught:
            registry.exec_with_yield(
                command="printf result", cwd=tmp_path, yield_time_ms=2_000,
                tty=False, max_lifetime_seconds=None, owner_host_session_id=OWNER,
                shell_argv=(sys.executable, "-c", "print('result')"),
                env=dict(os.environ), decision_deadline_monotonic=monotonic() + 5,
                output_subscriber=lambda *_: None, initial_result_reader=fail,
            )
        assert caught.value is failure
        assert len(observed) == 1
        assert observed[0].output.inflight_read_count == 0
        assert observed[0].output.observation_lease_count == 0
        assert observed[0].process.poll() == 0
        assert registry._states == {observed[0].process_id: observed[0]}
        assert registry.get_process_info(
            observed[0].process_id, owner_host_session_id=OWNER,
        ).status == "success"
        assert registry._decision_attempts == {}
        assert registry._launching_by_owner == {}


def test_failed_initial_result_reader_preserves_adopted_running_process(tmp_path):
    with owned_registry() as registry:
        registry.activate_owner(OWNER)
        observed = []
        failure = RuntimeError("initial output read failed after background adoption")

        def fail(state):
            assert state.background_adopted is True
            observed.append(state)
            raise failure

        with pytest.raises(RuntimeError) as caught:
            registry.exec_with_yield(
                command="retained background execution", cwd=tmp_path, yield_time_ms=0,
                tty=False, max_lifetime_seconds=None, owner_host_session_id=OWNER,
                shell_argv=(sys.executable, "-c", "import time; print('running', flush=True); time.sleep(30)"),
                env=dict(os.environ), decision_deadline_monotonic=monotonic() + 5,
                output_subscriber=lambda *_: None, initial_result_reader=fail,
            )
        assert caught.value is failure
        assert len(observed) == 1
        state = observed[0]
        assert state.process.poll() is None
        assert state.output.inflight_read_count == 0
        assert state.output.observation_lease_count == 0
        assert registry._decision_attempts == {}
        assert registry._launching_by_owner == {}
        inventory = registry.list_processes(
            owner_host_session_id=OWNER, include_running=True, include_finished=True,
        )
        assert [item.process_id for item in inventory] == [state.process_id]
        assert inventory[0].status == "running"
        assert inventory[0].background_adopted is True
        assert registry.get_process_info(state.process_id, owner_host_session_id=OWNER).status == "running"
        stopped = registry.terminate_if_running(
            state.process_id, max_output_chars=512, owner_host_session_id=OWNER,
        )
        assert stopped.disposition is TerminalTerminationDisposition.TERMINATION_COMPLETED
        assert stopped.result.status.value == "killed"
        assert state.process.poll() is not None
