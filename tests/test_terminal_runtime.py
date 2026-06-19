import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus
from pulsara_agent.runtime.terminal.output import OutputAccumulator
from pulsara_agent.runtime.terminal.process import (
    ProcessInputError,
    read_captured_cwd,
    spawn_local_process,
    wait_for_process,
)
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


def make_session(tmp_path):
    return TerminalSessionManager(tmp_path).get_or_create()


def make_manager(tmp_path, **kwargs):
    return TerminalSessionManager(tmp_path, **kwargs)


def run(session, command: str, **kwargs):
    return session.execute(TerminalRequest(command=command, **kwargs))


def test_terminal_runtime_runs_in_workspace_root_by_default(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "pwd")

    assert result.status is TerminalStatus.SUCCESS
    assert result.cwd == str(tmp_path)
    assert result.output == str(tmp_path)


def test_terminal_runtime_persists_current_cwd_after_cd(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    session = make_session(tmp_path)

    first = run(session, "cd src && pwd")
    second = run(session, "pwd")

    assert first.status is TerminalStatus.SUCCESS
    assert first.cwd == str(tmp_path / "src")
    assert second.output == str(tmp_path / "src")


def test_terminal_runtime_materialized_dot_workdir_honors_current_cwd(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    session = make_session(tmp_path)

    first = run(session, "cd src && pwd")
    dot = run(session, "pwd", workdir=".")
    dot_slash = run(session, "pwd", workdir="./")

    assert first.status is TerminalStatus.SUCCESS
    assert dot.output == str(tmp_path / "src")
    assert dot_slash.output == str(tmp_path / "src")


def test_terminal_runtime_resolves_relative_and_absolute_workdir(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    session = make_session(tmp_path)

    relative = run(session, "pwd", workdir="src")
    absolute = run(session, "pwd", workdir=str(tmp_path))
    outside = run(session, "pwd", workdir=str(tmp_path.parent))

    assert relative.status is TerminalStatus.SUCCESS
    assert relative.output == str(tmp_path / "src")
    assert absolute.status is TerminalStatus.SUCCESS
    assert absolute.output == str(tmp_path)
    assert outside.status is TerminalStatus.BLOCKED
    assert "escapes workspace root" in (outside.error or "")


def test_terminal_runtime_uses_configured_shell_and_records_metadata(tmp_path) -> None:
    shell_path = "/bin/sh"
    manager = TerminalSessionManager(
        tmp_path,
        shell=TerminalShellConfig(path=Path(shell_path), login=False),
    )
    session = manager.get_or_create()

    result = run(session, "printf shell-ok")

    assert result.status is TerminalStatus.SUCCESS
    assert result.output == "shell-ok"
    assert result.metadata["shell"]["path"] == shell_path
    assert result.metadata["shell"]["login"] is False
    assert session.state.backend_metadata["shell"]["path"] == shell_path


def test_terminal_runtime_default_shell_is_non_login(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "printf shell-ok")

    assert result.status is TerminalStatus.SUCCESS
    assert result.metadata["shell"]["login"] is False
    assert session.state.backend_metadata["shell"]["login"] is False


def test_terminal_runtime_does_not_update_cwd_when_command_ends_outside_workspace(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "cd /tmp && pwd")
    after = run(session, "pwd")

    assert result.status is TerminalStatus.BLOCKED
    assert result.cwd == str(tmp_path)
    assert result.output == "/tmp"
    assert after.output == str(tmp_path)


def test_terminal_runtime_recovers_deleted_current_cwd_to_existing_workspace_ancestor(tmp_path) -> None:
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)
    session = make_session(tmp_path)

    first = run(session, "cd src/nested && pwd")
    shutil.rmtree(nested)
    second = run(session, "pwd")

    assert first.status is TerminalStatus.SUCCESS
    assert first.cwd == str(nested)
    assert second.status is TerminalStatus.SUCCESS
    assert second.output == str(tmp_path / "src")
    assert session.current_cwd == tmp_path / "src"


def test_terminal_runtime_rejects_empty_command(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "   ")

    assert result.status is TerminalStatus.BLOCKED
    assert result.error == "command must not be empty"


def test_terminal_runtime_yield_keeps_partial_output_and_does_not_kill(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'import time; print(\"before\", flush=True); time.sleep(5)'",
        yield_time_ms=200,
    )

    assert result.status is TerminalStatus.RUNNING
    assert result.process_id is not None
    assert result.timed_out is False
    assert "before" in result.output
    assert manager.kill_process(result.process_id).status is TerminalStatus.KILLED


def test_terminal_runtime_cleans_per_process_cwd_file_after_readback(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    process = spawn_local_process(
        terminal_session_id="default",
        command="cd src && pwd >/dev/null",
        cwd=tmp_path,
        max_output_chars=1000,
        stdin_pipe=True,
        capture_cwd=True,
    )
    assert process.capture_cwd_file is not None
    cwd_file = process.capture_cwd_file

    wait_for_process(process, timeout_seconds=2, kill_on_timeout=True)
    observed_cwd = read_captured_cwd(process)

    assert observed_cwd == tmp_path / "src"
    assert not cwd_file.exists()


def test_terminal_runtime_truncates_with_head_and_tail(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(
        session,
        "python -c 'print(\"HEAD\" + \"x\" * 200 + \"TAIL\")'",
        max_output_chars=60,
    )

    assert result.truncated is True
    assert "HEAD" in result.output
    assert "TAIL" in result.output
    assert "OUTPUT TRUNCATED" in result.output


def test_terminal_runtime_strips_ansi_and_redacts_secrets(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "printf '\\033[31mred\\033[0m API_KEY=secret-token'")

    assert result.output == "red API_KEY=[REDACTED]"


def test_terminal_runtime_lifetime_watchdog_kills_process_group(tmp_path) -> None:
    marker = f"pulsara_terminal_test_{os.getpid()}_{int(time.time())}"
    session = make_session(tmp_path)

    result = run(
        session,
        (
            "python -c 'import subprocess, time; "
            f"subprocess.Popen([\"sh\", \"-c\", \"sleep 2; touch /tmp/{marker}\"]); "
            "time.sleep(10)'"
        ),
        yield_time_ms=5000,
        max_lifetime_seconds=1,
    )
    time.sleep(2.5)

    assert result.status is TerminalStatus.KILLED
    assert not os.path.exists(f"/tmp/{marker}")
    subprocess.run(["rm", "-f", f"/tmp/{marker}"], check=False)


def test_terminal_runtime_in_window_completion_is_not_registered(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "printf quick", yield_time_ms=10_000)

    assert result.status is TerminalStatus.SUCCESS
    assert result.process_id is None
    assert manager.process_registry._processes == {}


def test_terminal_runtime_yielded_process_poll_wait_and_kill(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "sleep 5", yield_time_ms=0)

    assert result.status is TerminalStatus.RUNNING
    assert result.process_id is not None
    assert manager.poll_process(result.process_id).status is TerminalStatus.RUNNING

    killed = manager.kill_process(result.process_id)

    assert killed.status is TerminalStatus.KILLED


def test_terminal_runtime_yielded_process_without_lifetime_survives_past_default_window(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "sleep 0.7 && printf survived", yield_time_ms=0)
    assert result.status is TerminalStatus.RUNNING
    assert result.process_id is not None

    still_running = manager.wait_process(result.process_id, timeout_seconds=0.2)
    final = manager.wait_process(result.process_id, timeout_seconds=2)

    assert still_running.status is TerminalStatus.RUNNING
    assert final.status is TerminalStatus.SUCCESS
    assert final.output == "survived"


def test_terminal_runtime_wait_timeout_does_not_kill_yielded_process(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "sleep 0.4 && printf done", yield_time_ms=0)
    assert result.process_id is not None

    first_wait = manager.wait_process(result.process_id, timeout_seconds=0.05)
    final_wait = manager.wait_process(result.process_id, timeout_seconds=2)

    assert first_wait.status is TerminalStatus.RUNNING
    assert final_wait.status is TerminalStatus.SUCCESS
    assert final_wait.output == "done"


def test_terminal_runtime_yielded_high_output_does_not_deadlock(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'for i in range(30000): print(i)'",
        yield_time_ms=0,
        max_output_chars=200,
    )
    assert result.process_id is not None

    final = manager.wait_process(result.process_id, timeout_seconds=10, max_output_chars=200)

    assert final.status is TerminalStatus.SUCCESS
    assert final.truncated is True
    assert "OUTPUT TRUNCATED" in final.output


def test_terminal_runtime_yielded_process_does_not_update_session_cwd(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "cd src && sleep 0.2 && pwd", yield_time_ms=0)
    assert result.process_id is not None
    final = manager.wait_process(result.process_id, timeout_seconds=2)
    after = run(session, "pwd")

    assert final.status is TerminalStatus.SUCCESS
    assert final.output == str(tmp_path / "src")
    assert after.output == str(tmp_path)


def test_terminal_runtime_long_running_command_yields_to_managed_process(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    yielded = run(session, "tail -f /dev/null", yield_time_ms=100)

    assert yielded.status is TerminalStatus.RUNNING
    assert yielded.process_id is not None
    assert manager.kill_process(yielded.process_id).status is TerminalStatus.KILLED


def test_terminal_runtime_shell_background_wrapper_suggests_terminal_yield(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "sleep 5 &")

    assert result.status is TerminalStatus.BLOCKED
    assert result.error == "shell-level background wrappers should use terminal yield semantics instead"
    assert result.metadata["policy_code"] == "use_terminal_yield"
    assert result.metadata["suggested_args"] == {"yield_time_ms": 0}


def test_terminal_runtime_dangerous_command_requires_confirmation_when_called_directly(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "rm -rf build")

    assert result.status is TerminalStatus.BLOCKED
    assert result.error == "terminal command requires user confirmation before execution"
    assert result.metadata["policy_code"] == "requires_confirmation"


def test_terminal_runtime_yield_limit_does_not_count_in_window_completion(tmp_path) -> None:
    manager = make_manager(tmp_path, max_live_processes=1)
    session = manager.get_or_create()

    first = run(session, "sleep 5", yield_time_ms=0)
    foreground = run(session, "pwd")
    second = run(session, "sleep 5", yield_time_ms=0)

    assert first.status is TerminalStatus.RUNNING
    assert foreground.status is TerminalStatus.SUCCESS
    assert foreground.process_id is None
    assert second.status is TerminalStatus.BLOCKED
    assert "max live terminal processes" in (second.error or "")
    assert first.process_id is not None
    manager.kill_process(first.process_id)


def test_terminal_runtime_finished_process_retention_is_lazy_and_bounded(tmp_path) -> None:
    manager = make_manager(tmp_path, max_finished_processes=1)
    session = manager.get_or_create()

    first = run(session, "sleep 0.05 && printf first", yield_time_ms=0)
    assert first.process_id is not None
    assert manager.wait_process(first.process_id, timeout_seconds=2).status is TerminalStatus.SUCCESS

    second = run(session, "sleep 0.05 && printf second", yield_time_ms=0)
    assert second.process_id is not None
    assert manager.wait_process(second.process_id, timeout_seconds=2).status is TerminalStatus.SUCCESS
    manager.poll_process(second.process_id)

    try:
        manager.poll_process(first.process_id)
    except KeyError as exc:
        assert "not found or expired" in str(exc)
    else:
        raise AssertionError("old finished process should be evicted")


def test_terminal_runtime_shutdown_kills_tracked_yielded_process(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "sleep 10", yield_time_ms=0)
    assert result.process_id is not None

    manager.shutdown()
    killed = manager.poll_process(result.process_id)

    assert killed.status is TerminalStatus.KILLED


def test_output_accumulator_redacts_secret_split_across_chunks() -> None:
    accumulator = OutputAccumulator()

    accumulator.append(b"API_KEY=sec")
    assert accumulator.snapshot(max_chars=100).text == ""

    accumulator.append(b"ret\n")
    snapshot = accumulator.snapshot(max_chars=100)

    assert snapshot.text == "API_KEY=[REDACTED]"
    assert "secret" not in snapshot.text


def test_terminal_runtime_write_does_not_append_newline(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'import sys; data=sys.stdin.read(4); print(\"NL\" if data.endswith(\"\\n\") else \"NO_NL\")'",
        yield_time_ms=0,
    )
    assert result.process_id is not None

    manager.write_process(result.process_id, "ping")
    final = manager.wait_process(result.process_id, timeout_seconds=2)

    assert final.status is TerminalStatus.SUCCESS
    assert final.output == "NO_NL"


def test_terminal_runtime_submit_appends_newline(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'import sys; line=sys.stdin.readline(); print(\"NL\" if line.endswith(\"\\n\") else \"NO_NL\")'",
        yield_time_ms=0,
    )
    assert result.process_id is not None

    manager.write_process(result.process_id, "ping", append_newline=True)
    final = manager.wait_process(result.process_id, timeout_seconds=2)

    assert final.status is TerminalStatus.SUCCESS
    assert final.output == "NL"


def test_terminal_runtime_close_stdin_sends_eof(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'import sys; data=sys.stdin.read(); print(\"EOF:\" + data)'",
        yield_time_ms=0,
    )
    assert result.process_id is not None

    manager.write_process(result.process_id, "payload")
    manager.close_process_stdin(result.process_id)
    final = manager.wait_process(result.process_id, timeout_seconds=2)

    assert final.status is TerminalStatus.SUCCESS
    assert final.output == "EOF:payload"


def test_terminal_runtime_rejects_write_after_process_finished(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "sleep 0.05 && printf done", yield_time_ms=0)
    assert result.process_id is not None
    assert manager.wait_process(result.process_id, timeout_seconds=2).status is TerminalStatus.SUCCESS

    with pytest.raises(ProcessInputError, match="finished"):
        manager.write_process(result.process_id, "late")


def test_terminal_runtime_large_output_spills_redacted_full_output_ref(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(
        session,
        "python -c 'print(\"HEAD\"); print(\"API_KEY=secret-token\"); print(\"x\" * 1100000); print(\"TAIL\")'",
        max_output_chars=120,
    )

    assert result.status is TerminalStatus.SUCCESS
    assert result.truncated is True
    assert result.full_output_ref is not None
    assert "OUTPUT TRUNCATED" in result.output
    assert "HEAD" in result.output
    assert "TAIL" in result.output
    artifact_path = tmp_path / result.full_output_ref
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert "HEAD" in artifact_text
    assert "TAIL" in artifact_text
    assert "API_KEY=[REDACTED]" in artifact_text
    assert "secret-token" not in artifact_text


def test_terminal_runtime_yielded_large_output_full_output_ref_is_readable(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'print(\"START\"); print(\"y\" * 100000); print(\"END\")'",
        yield_time_ms=0,
        max_output_chars=100,
    )
    assert result.process_id is not None
    final = manager.wait_process(result.process_id, timeout_seconds=5, max_output_chars=100)

    assert final.status is TerminalStatus.SUCCESS
    assert final.truncated is True
    assert final.full_output_ref is not None
    artifact_text = (tmp_path / final.full_output_ref).read_text(encoding="utf-8")
    assert "START" in artifact_text
    assert "END" in artifact_text


def test_terminal_runtime_pty_reports_tty(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(
        session,
        "python -c 'import sys; print(sys.stdin.isatty())'",
        yield_time_ms=0,
        tty=True,
    )
    assert result.process_id is not None

    final = manager.wait_process(result.process_id, timeout_seconds=2)

    assert final.status is TerminalStatus.SUCCESS
    assert "True" in final.output
    assert final.metadata["io_mode"] == "pty"


def test_terminal_runtime_pty_python_repl_submit_and_close(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "python", yield_time_ms=0, tty=True, max_output_chars=5000)
    assert result.process_id is not None

    manager.write_process(result.process_id, 'print("PULSARA_PTY_OK")', append_newline=True)
    poll = manager.poll_process(result.process_id, max_output_chars=5000)
    deadline = time.monotonic() + 3
    while "PULSARA_PTY_OK" not in poll.output and time.monotonic() < deadline:
        time.sleep(0.05)
        poll = manager.poll_process(result.process_id, max_output_chars=5000)
    manager.close_process_stdin(result.process_id)
    final = manager.wait_process(result.process_id, timeout_seconds=3, max_output_chars=5000)

    assert "PULSARA_PTY_OK" in poll.output
    assert final.status is TerminalStatus.SUCCESS


def test_terminal_runtime_pty_kill_does_not_leave_process_running(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "python", yield_time_ms=0, tty=True)
    assert result.process_id is not None
    killed = manager.kill_process(result.process_id)

    assert killed.status is TerminalStatus.KILLED
    assert manager.poll_process(result.process_id).status is TerminalStatus.KILLED


def test_terminal_runtime_pty_blocks_known_pipe_stdin_command(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create()

    result = run(session, "gh auth login --with-token", yield_time_ms=0, tty=True)

    assert result.status is TerminalStatus.BLOCKED
    assert "pipe stdin" in (result.error or "")
