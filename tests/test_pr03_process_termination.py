from __future__ import annotations

import os
from pathlib import Path
import sys
from threading import Event, Thread
from time import sleep
from time import monotonic

import pytest

from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_process.models import (
    TerminalPhysicalState,
    TerminalTerminationDisposition,
)


def _launch(
    registry: ProcessRegistry,
    tmp_path: Path,
    *,
    owner: str,
    program: str,
    yield_time_ms: int = 2_000,
):
    registry.activate_owner(owner)
    return registry.exec_with_yield(
        terminal_session_id="default",
        command=program,
        cwd=tmp_path,
        yield_time_ms=yield_time_ms,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", program),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )


@pytest.mark.parametrize(
    ("program", "expected_status", "expected_exit_code"),
    (("raise SystemExit(0)", "success", 0), ("raise SystemExit(7)", "error", 7)),
)
def test_pr03_late_termination_preserves_confirmed_natural_terminal_state(
    tmp_path: Path,
    program: str,
    expected_status: str,
    expected_exit_code: int,
) -> None:
    registry = ProcessRegistry()
    owner = f"host:late-terminal:{expected_status}"
    completions: list[tuple[str, int | None]] = []
    registry._completion_subscriber = (  # noqa: SLF001
        lambda info, _snapshot: completions.append((info.status, info.exit_code))
    )
    state, yielded, _cwd = _launch(
        registry, tmp_path, owner=owner, program=program
    )

    assert yielded is False
    assert state.physical_completion.is_set()
    assert completions == [(expected_status, expected_exit_code)]

    termination = registry.terminate_if_running(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )

    assert termination.disposition is TerminalTerminationDisposition.ALREADY_TERMINAL
    assert termination.result.status.value == expected_status
    assert termination.result.exit_code == expected_exit_code
    assert registry.poll(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    ).status.value == expected_status
    assert registry.log(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    ).process.status == expected_status
    assert completions == [(expected_status, expected_exit_code)]
    registry.release_owner(owner, timeout_seconds=2)


def test_pr03_background_adopted_only_after_successful_yield_publication(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry()
    owner = "host:background-adopted"
    state, yielded, _cwd = _launch(
        registry,
        tmp_path,
        owner=owner,
        program="import time; time.sleep(30)",
        yield_time_ms=0,
    )

    assert yielded is True
    info = registry.list_processes(
        owner_host_session_id=owner,
        include_finished=True,
        include_running=True,
    )[0]
    assert info.process_id == state.process_id
    assert info.background_adopted is True

    termination = registry.terminate_if_running(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )
    assert termination.disposition is TerminalTerminationDisposition.TERMINATION_COMPLETED
    assert termination.result.status.value == "killed"
    assert termination.physical_state == TerminalPhysicalState.PHYSICALLY_JOINED.value
    retained = registry.list_background_processes(owner_host_session_id=owner)
    assert [item.process_id for item in retained] == [state.process_id]
    assert retained[0].background_adopted is True
    registry.release_owner(owner, timeout_seconds=2)


def test_pr03_shell_exit_with_live_group_is_still_terminable(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    owner = "host:live-descendant"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="background descendant",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=("/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & exit 0"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    state.process.wait(timeout=1)

    termination = registry.terminate_if_running(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )

    assert termination.disposition is TerminalTerminationDisposition.TERMINATION_COMPLETED
    assert termination.result.status.value == "killed"
    assert termination.group_alive is False
    assert state.physical_completion.is_set()
    registry.release_owner(owner, timeout_seconds=2)


def test_pr03_natural_exit_during_physical_settlement_is_not_reported_as_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _PausedWatcherRegistry(ProcessRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.shell_exited = Event()
            self.finish_watcher = Event()

        def _watch_process(self, state) -> None:
            state.process.wait()
            self.shell_exited.set()
            assert self.finish_watcher.wait(timeout=2)
            super()._watch_process(state)

    registry = _PausedWatcherRegistry()
    owner = "host:natural-settlement-race"
    state, _yielded, _cwd = _launch(
        registry,
        tmp_path,
        owner=owner,
        program="raise SystemExit(7)",
        yield_time_ms=0,
    )
    assert registry.shell_exited.wait(timeout=1)

    signals: list[str] = []

    def record_signal(_state) -> None:
        signals.append("sent")

    monkeypatch.setattr(
        "pulsara_agent.terminal_process.manager._terminate_process_group",
        record_signal,
    )
    releaser = Thread(
        target=lambda: (sleep(0.05), registry.finish_watcher.set()),
        daemon=True,
    )
    releaser.start()
    result = registry.terminate_if_running(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
        join_timeout_seconds=1,
    )
    releaser.join(timeout=1)

    assert result.disposition is TerminalTerminationDisposition.ALREADY_TERMINAL
    assert result.result.status.value == "error"
    assert result.result.exit_code == 7
    assert state.termination_intent is None
    assert signals == []
    registry.release_owner(owner, timeout_seconds=2)
