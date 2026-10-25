from __future__ import annotations

import errno
import os
from pathlib import Path
import signal
import sys
from time import monotonic

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_process import manager as M


def launch(registry: M.ProcessRegistry, owner: str, cwd: Path, *, tty=False, argv=None):
    return registry.exec_with_yield(
        command="startup cleanup fixture",
        cwd=cwd,
        yield_time_ms=0,
        tty=tty,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=argv or (sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
        decision_deadline_monotonic=monotonic() + 5,
    )


def assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


def test_foreground_timer_start_failure_releases_exact_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = M.ProcessRegistry(max_live_processes=1)
    owner = derive_id("host", "test-fixture", "foreground-timer-fault")
    registry.activate_owner(owner)
    failure = RuntimeError("foreground watchdog could not start")
    original_start = M.Timer.start

    def fail_foreground(timer):
        if getattr(timer.function, "__name__", "") == "_abort_foreground_decision":
            raise failure
        return original_start(timer)

    with monkeypatch.context() as patch:
        patch.setattr(M.Timer, "start", fail_foreground)
        with pytest.raises(RuntimeError) as caught:
            launch(registry, owner, tmp_path)
        assert caught.value is failure
    assert registry._launching_by_owner == {}
    assert registry._decision_attempts == {}
    assert registry._states == {}
    # The failed reservation must not consume the only physical slot.
    state, _ = launch(registry, owner, tmp_path, argv=(sys.executable, "-c", "pass"))
    assert state.physical_completion.wait(5)
    registry.release_owner(owner, timeout_seconds=2)


@pytest.mark.parametrize(
    "stage,tty",
    [
        ("popen", True),
        ("fdopen", True),
        ("reader", False),
        ("reader", True),
        ("state", False),
        ("state", True),
    ],
)
def test_pre_state_failure_settles_owned_child_and_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, tty: bool
) -> None:
    registry = M.ProcessRegistry()
    owner = derive_id("host", "test-fixture", f"pre-state-{stage}-{tty}")
    registry.activate_owner(owner)
    pairs = []
    children = []
    duplicated = []
    original_openpty = M.pty.openpty
    original_popen = M.subprocess.Popen

    def capture_openpty():
        pair = original_openpty()
        pairs.append(pair)
        return pair

    def capture_popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    failure = RuntimeError(f"injected {stage} construction failure")

    def fail(*args, **kwargs):
        raise failure

    def fail_fdopen(fd, *args, **kwargs):
        duplicated.append(fd)
        raise failure

    try:
        with monkeypatch.context() as patch:
            patch.setattr(M.pty, "openpty", capture_openpty)
            patch.setattr(M.subprocess, "Popen", capture_popen)
            if stage == "fdopen":
                patch.setattr(M.os, "fdopen", fail_fdopen)
            elif stage == "reader":
                patch.setattr(M, "Thread", fail)
            elif stage == "state":
                patch.setattr(M, "_ProcessState", fail)
            expected = FileNotFoundError if stage == "popen" else RuntimeError
            with pytest.raises(expected) as caught:
                launch(
                    registry,
                    owner,
                    tmp_path,
                    tty=tty,
                    argv=("/nonexistent/pulsara-test-shell",)
                    if stage == "popen"
                    else None,
                )
            if stage != "popen":
                assert caught.value is failure
        assert len(children) == (0 if stage == "popen" else 1)
        for child in children:
            assert child.poll() is not None
            assert not M._process_group_exists(child.pid)
            for stream in (child.stdin, child.stdout, child.stderr):
                assert stream is None or stream.closed
        for pair in pairs:
            for fd in pair:
                assert_closed(fd)
        for fd in duplicated:
            assert_closed(fd)
        assert registry._launching_by_owner == {}
        assert registry._decision_attempts == {}
        assert registry._states == {}
        registry.release_owner(owner, timeout_seconds=1)
    finally:
        # Keep a regression failure from leaving this test's exact child alive.
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
