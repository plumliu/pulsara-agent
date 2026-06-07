import os
import subprocess
import time

from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus


def make_session(tmp_path):
    return TerminalSessionManager(tmp_path).get_or_create()


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


def test_terminal_runtime_does_not_update_cwd_when_command_ends_outside_workspace(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "cd /tmp && pwd")
    after = run(session, "pwd")

    assert result.status is TerminalStatus.BLOCKED
    assert result.cwd == str(tmp_path)
    assert result.output == "/tmp"
    assert after.output == str(tmp_path)


def test_terminal_runtime_rejects_empty_command(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "   ")

    assert result.status is TerminalStatus.BLOCKED
    assert result.error == "command must not be empty"


def test_terminal_runtime_timeout_keeps_partial_output(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(
        session,
        "python -c 'import time; print(\"before\", flush=True); time.sleep(5)'",
        timeout_seconds=1,
    )

    assert result.status is TerminalStatus.TIMEOUT
    assert result.exit_code == 124
    assert result.timed_out is True
    assert "before" in result.output


def test_terminal_runtime_timeout_does_not_reuse_previous_cwd_file(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    session = make_session(tmp_path)

    session.backend._cwd_file.write_text(str(tmp_path / "src"), encoding="utf-8")
    result = run(session, "sleep 5", timeout_seconds=1)

    assert result.status is TerminalStatus.TIMEOUT
    assert result.cwd == str(tmp_path)


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


def test_terminal_runtime_kills_process_group_on_timeout(tmp_path) -> None:
    marker = f"pulsara_terminal_test_{os.getpid()}_{int(time.time())}"
    session = make_session(tmp_path)

    result = run(
        session,
        (
            "python -c 'import subprocess, time; "
            f"subprocess.Popen([\"sh\", \"-c\", \"sleep 2; touch /tmp/{marker}\"]); "
            "time.sleep(10)'"
        ),
        timeout_seconds=1,
    )
    time.sleep(2.5)

    assert result.status is TerminalStatus.TIMEOUT
    assert not os.path.exists(f"/tmp/{marker}")
    subprocess.run(["rm", "-f", f"/tmp/{marker}"], check=False)
