import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import pulsara_agent.runtime.terminal.process as process_mod

from pulsara_agent.event import EventContext, TerminalProcessCompletedEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus
from pulsara_agent.runtime.terminal.env import TerminalEnvBuilder, TerminalEnvConfig
from pulsara_agent.runtime.terminal.output import OutputAccumulator
from pulsara_agent.runtime.terminal.process import (
    PendingTerminalCompletionError,
    ProcessInputError,
    TerminalCompletionRecordState,
    TerminalKillReason,
    _maybe_record_completion_event,
    kill_process,
    read_captured_cwd,
    spawn_local_process,
    snapshot_process,
    wait_for_process,
)
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


def make_session(tmp_path):
    return TerminalSessionManager(tmp_path).get_or_create()


def make_manager(tmp_path, **kwargs):
    return TerminalSessionManager(tmp_path, **kwargs)


def run(session, command: str, **kwargs):
    return session.execute(TerminalRequest(command=command, **kwargs))


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


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


def test_spawn_local_process_starts_isolated_session_without_preexec_hook(tmp_path) -> None:
    process = spawn_local_process(
        terminal_session_id="default",
        command="sleep 10",
        cwd=tmp_path,
        max_output_chars=1000,
        stdin_pipe=True,
        capture_cwd=False,
    )
    try:
        assert os.getsid(process.process.pid) == process.process.pid
        assert os.getpgid(process.process.pid) == process.process.pid
    finally:
        wait_for_process(process, timeout_seconds=0.01, kill_on_timeout=True)


def test_spawn_local_process_default_env_does_not_inherit_provider_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSARA_API_KEY", "pulsara-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    process = spawn_local_process(
        terminal_session_id="default",
        command=(
            "python - <<'PY'\n"
            "import os\n"
            "print('missing_0=' + str('PULSARA_API_KEY' not in os.environ))\n"
            "print('missing_1=' + str('OPENAI_API_KEY' not in os.environ))\n"
            "PY"
        ),
        cwd=tmp_path,
        max_output_chars=1000,
        stdin_pipe=True,
        capture_cwd=True,
    )

    wait_for_process(process, timeout_seconds=2, kill_on_timeout=True)
    result = snapshot_process(process)

    assert result.status is TerminalStatus.SUCCESS
    assert "missing_0=True" in result.output
    assert "missing_1=True" in result.output
    assert "pulsara-secret" not in result.output


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


def test_terminal_runtime_sanitizes_pipe_child_environment(tmp_path) -> None:
    env_builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=False),
        parent_env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "LANG": "en_US.UTF-8",
            "PULSARA_API_KEY": "pulsara-secret",
            "OPENAI_API_KEY": "openai-secret",
            "FOO_TOKEN": "token-secret",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
            "NVM_DIR": "/tmp/nvm",
            "PYTHONPATH": "/tmp/evil",
            "NODE_OPTIONS": "--require /tmp/hook.js",
        },
    )
    manager = make_manager(tmp_path, env_builder=env_builder)
    session = manager.get_or_create()

    result = run(
        session,
        (
            "python - <<'PY'\n"
            "import os\n"
            "names = ['PULSARA_API_KEY','OPENAI_API_KEY','FOO_TOKEN','PYTHONPATH','NODE_OPTIONS']\n"
            "for index, name in enumerate(names):\n"
            "    print(f'missing_{index}={name not in os.environ}')\n"
            "print('SSH_AUTH_SOCK=' + os.environ.get('SSH_AUTH_SOCK', '<missing>'))\n"
            "print('NVM_DIR=' + os.environ.get('NVM_DIR', '<missing>'))\n"
            "PY"
        ),
    )

    assert result.status is TerminalStatus.SUCCESS
    assert "missing_0=True" in result.output
    assert "missing_1=True" in result.output
    assert "missing_2=True" in result.output
    assert "missing_3=True" in result.output
    assert "missing_4=True" in result.output
    assert "SSH_AUTH_SOCK=/tmp/ssh-agent.sock" in result.output
    assert "NVM_DIR=/tmp/nvm" in result.output
    assert "pulsara-secret" not in result.output
    assert "openai-secret" not in result.output
    assert "env" in result.metadata
    assert "PATH" not in result.metadata["env"]


def test_terminal_runtime_sanitizes_pty_child_environment(tmp_path) -> None:
    env_builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=False),
        parent_env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PULSARA_API_KEY": "pulsara-secret",
        },
    )
    manager = make_manager(tmp_path, env_builder=env_builder)
    session = manager.get_or_create()

    result = run(session, "python -c 'import os; print(\"present\", \"PULSARA_API_KEY\" in os.environ)'", tty=True)

    assert result.status is TerminalStatus.SUCCESS
    assert "present False" in result.output
    assert "pulsara-secret" not in result.output


def test_terminal_runtime_shell_snapshot_path_is_used_without_login_shell_default(tmp_path) -> None:
    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    tool = bin_dir / "snapshot-tool"
    write_executable(tool, "#!/bin/sh\nprintf snapshot-ok\n")
    fake_shell = tmp_path / "zsh"
    write_executable(
        fake_shell,
        "#!/bin/sh\n"
        "command=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -c) shift; command=\"${1:-}\"; break ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "if printf '%s' \"$command\" | grep -q 'env -0'; then\n"
        "  printf '__PULSARA_ENV_START__\\0'\n"
        f"  printf 'PATH={bin_dir}:/usr/bin:/bin\\0'\n"
        "  printf 'OPENAI_API_KEY=secret\\0'\n"
        "  exit 0\n"
        "fi\n"
        "exec /bin/sh -c \"$command\"\n",
    )
    env_builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=True),
        parent_env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    manager = TerminalSessionManager(
        tmp_path,
        shell=TerminalShellConfig(path=fake_shell, login=False),
        env_builder=env_builder,
    )
    session = manager.get_or_create()

    result = run(session, "snapshot-tool")

    assert result.status is TerminalStatus.SUCCESS
    assert result.output == "snapshot-ok"
    assert result.metadata["shell"]["login"] is False
    assert result.metadata["env"]["shell_snapshot_used"] is True


def test_terminal_runtime_nearest_venv_overlay_uses_session_cwd(tmp_path) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "packages" / "foo" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "packages" / "foo" / "src").mkdir(parents=True)
    root_marker = tmp_path / ".venv" / "bin" / "which-venv"
    package_marker = tmp_path / "packages" / "foo" / ".venv" / "bin" / "which-venv"
    write_executable(root_marker, "#!/bin/sh\nprintf root-venv\n")
    write_executable(package_marker, "#!/bin/sh\nprintf package-venv\n")
    env_builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=False),
        parent_env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    manager = make_manager(tmp_path, env_builder=env_builder)
    session = manager.get_or_create()

    root = run(session, "which-venv")
    cd = run(session, "cd packages/foo/src && pwd")
    package = run(session, "which-venv", workdir=".")

    assert root.output == "root-venv"
    assert cd.status is TerminalStatus.SUCCESS
    assert package.output == "package-venv"
    assert package.metadata["env"]["venv_overlay"] == str(tmp_path / "packages" / "foo" / ".venv" / "bin")


def test_terminal_runtime_venv_overlay_falls_back_to_root_and_ignores_outside(tmp_path) -> None:
    outside = tmp_path.parent / f"outside-venv-{os.getpid()}"
    try:
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / "packages" / "bar").mkdir(parents=True)
        (outside / ".venv" / "bin").mkdir(parents=True)
        root_marker = tmp_path / ".venv" / "bin" / "which-venv"
        outside_marker = outside / ".venv" / "bin" / "which-venv"
        write_executable(root_marker, "#!/bin/sh\nprintf root-venv\n")
        write_executable(outside_marker, "#!/bin/sh\nprintf outside-venv\n")
        env_builder = TerminalEnvBuilder(
            config=TerminalEnvConfig(enable_shell_snapshot=False),
            parent_env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        manager = make_manager(tmp_path, env_builder=env_builder)
        session = manager.get_or_create()

        package = run(session, "which-venv", workdir="packages/bar")
        root = run(session, "which-venv", workdir=".")

        assert package.output == "root-venv"
        assert root.output == "root-venv"
        assert "outside-venv" not in package.output
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_terminal_runtime_venv_overlay_switches_python_resolution(tmp_path) -> None:
    parent_bin = tmp_path / "parent-bin"
    workspace_venv_bin = tmp_path / ".venv" / "bin"
    parent_python = parent_bin / "python"
    workspace_python = workspace_venv_bin / "python"
    parent_bin.mkdir(parents=True)
    workspace_venv_bin.mkdir(parents=True)
    write_executable(parent_python, "#!/bin/sh\nprintf 'parent-path-python\n'")
    write_executable(workspace_python, "#!/bin/sh\nprintf 'workspace-venv-python\n'")
    command = "command -v python && python -c 'print(\"ignored\")'"

    disabled = run(
        make_manager(
            tmp_path,
            env_builder=TerminalEnvBuilder(
                config=TerminalEnvConfig(enable_shell_snapshot=False, enable_venv_overlay=False),
                parent_env={"PATH": f"{parent_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
            ),
        ).get_or_create(),
        command,
    )
    enabled = run(
        make_manager(
            tmp_path,
            env_builder=TerminalEnvBuilder(
                config=TerminalEnvConfig(enable_shell_snapshot=False, enable_venv_overlay=True),
                parent_env={"PATH": f"{parent_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
            ),
        ).get_or_create(),
        command,
    )

    assert disabled.status is TerminalStatus.SUCCESS
    assert disabled.output.splitlines() == [str(parent_python), "parent-path-python"]
    assert disabled.metadata["env"]["venv_overlay"] is None

    assert enabled.status is TerminalStatus.SUCCESS
    assert enabled.output.splitlines() == [str(workspace_python), "workspace-venv-python"]
    assert enabled.metadata["env"]["venv_overlay"] == str(workspace_venv_bin)


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


def test_terminal_runtime_non_hardline_risky_command_is_not_blocked_by_runtime_floor(tmp_path) -> None:
    session = make_session(tmp_path)
    (tmp_path / "build").mkdir()

    result = run(session, "rm -rf build")

    assert result.status is TerminalStatus.SUCCESS
    assert not (tmp_path / "build").exists()


def test_terminal_runtime_hardline_command_is_blocked_when_called_directly(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(session, "rm -rf /")

    assert result.status is TerminalStatus.BLOCKED
    assert result.error == "terminal command blocked by hardline permission policy"
    assert result.metadata["policy_code"] == "hardline_terminal_command"


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


def test_terminal_runtime_owner_scoped_process_access(tmp_path) -> None:
    manager = make_manager(tmp_path)
    first = manager.get_or_create(owner_host_session_id="host:a")
    second = manager.get_or_create(owner_host_session_id="host:b")

    result = first.execute(TerminalRequest(command="sleep 10", yield_time_ms=0))
    assert result.process_id is not None

    with pytest.raises(KeyError):
        manager.poll_process(result.process_id, owner_host_session_id="host:b")
    assert manager.poll_process(result.process_id, owner_host_session_id="host:a").status is TerminalStatus.RUNNING
    assert second.current_cwd == tmp_path
    assert manager.kill_owned("host:a")[0].status is TerminalStatus.KILLED


def test_terminal_runtime_list_processes_returns_running_and_finished_tasks(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    running = session.execute(TerminalRequest(command="sleep 5", yield_time_ms=0))
    finished = session.execute(TerminalRequest(command="sleep 0.05 && printf done", yield_time_ms=0))
    assert running.process_id is not None
    assert finished.process_id is not None
    manager.wait_process(finished.process_id, timeout_seconds=2)

    processes = manager.list_processes(owner_host_session_id="host:a")
    running_only = manager.list_processes(owner_host_session_id="host:a", include_finished=False)

    assert [process.status for process in running_only] == ["running"]
    assert {process.process_id for process in processes} == {running.process_id, finished.process_id}
    assert all(process.duration_seconds >= 0 for process in processes)
    assert all(process.started_at_monotonic > 0 for process in processes)
    assert manager.kill_process(running.process_id).status is TerminalStatus.KILLED


def test_terminal_runtime_list_processes_is_owner_scoped(tmp_path) -> None:
    manager = make_manager(tmp_path)
    first = manager.get_or_create(owner_host_session_id="host:a")
    second = manager.get_or_create(owner_host_session_id="host:b")

    first_result = first.execute(TerminalRequest(command="sleep 5", yield_time_ms=0))
    second_result = second.execute(TerminalRequest(command="sleep 5", yield_time_ms=0))
    assert first_result.process_id is not None
    assert second_result.process_id is not None

    assert [process.process_id for process in manager.list_processes(owner_host_session_id="host:a")] == [
        first_result.process_id
    ]
    assert [process.process_id for process in manager.list_processes(owner_host_session_id="host:b")] == [
        second_result.process_id
    ]
    manager.shutdown()


def test_terminal_runtime_log_process_returns_output_and_summary(tmp_path) -> None:
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(TerminalRequest(command="sleep 0.05 && printf LOG_OK", yield_time_ms=0))
    assert result.process_id is not None
    manager.wait_process(result.process_id, timeout_seconds=2)
    log = manager.log_process(result.process_id, owner_host_session_id="host:a")

    assert log.output == "LOG_OK"
    assert log.truncated is False
    assert log.process.process_id == result.process_id
    assert log.process.command == "sleep 0.05 && printf LOG_OK"
    with pytest.raises(KeyError):
        manager.log_process(result.process_id, owner_host_session_id="host:b")


def test_terminal_runtime_finished_process_list_respects_ttl_cleanup(tmp_path) -> None:
    manager = make_manager(tmp_path, finished_ttl_seconds=0.01)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(TerminalRequest(command="sleep 0.05 && printf old", yield_time_ms=0))
    assert result.process_id is not None
    manager.wait_process(result.process_id, timeout_seconds=2)
    time.sleep(0.03)

    assert manager.list_processes(owner_host_session_id="host:a") == []


def test_terminal_runtime_yielded_process_records_completion_event_once(tmp_path) -> None:
    events = []
    ctx = EventContext(run_id="run:terminal", turn_id="turn:terminal", reply_id="reply:terminal")
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(
        TerminalRequest(
            command="sleep 0.05 && printf API_KEY=secret-token",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:terminal",
                "record_event": events.append,
            },
        )
    )
    assert result.process_id is not None
    manager.wait_process(result.process_id, timeout_seconds=2)
    manager.poll_process(result.process_id)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TerminalProcessCompletedEvent)
    assert event.run_id == ctx.run_id
    assert event.turn_id == ctx.turn_id
    assert event.reply_id == ctx.reply_id
    assert event.tool_call_id == "call:terminal"
    assert event.process_id == result.process_id
    assert event.status == "success"
    assert event.exit_code == 0
    assert "API_KEY=[REDACTED]" in event.output_preview
    assert "secret-token" not in event.output_preview


def test_terminal_runtime_in_window_completion_does_not_record_completion_event(tmp_path) -> None:
    events = []
    ctx = EventContext(run_id="run:foreground", turn_id="turn:foreground", reply_id="reply:foreground")
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(
        TerminalRequest(
            command="printf FOREGROUND_DONE",
            yield_time_ms=10_000,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:terminal",
                "record_event": events.append,
            },
        )
    )

    assert result.status is TerminalStatus.SUCCESS
    assert result.process_id is None
    assert events == []


def test_terminal_runtime_completion_event_race_does_not_miss_fast_yield(tmp_path) -> None:
    events = []
    ctx = EventContext(run_id="run:race", turn_id="turn:race", reply_id="reply:race")
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(
        TerminalRequest(
            command="printf FAST_DONE",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:terminal",
                "record_event": events.append,
            },
        )
    )
    if result.process_id is not None:
        manager.wait_process(result.process_id, timeout_seconds=2)

    assert result.process_id is None or len(events) == 1


def test_terminal_runtime_user_kill_records_completion_event_but_shutdown_suppresses(tmp_path) -> None:
    ctx = EventContext(run_id="run:kill", turn_id="turn:kill", reply_id="reply:kill")
    user_events = []
    teardown_events = []
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    user = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={"origin_event_context": ctx, "tool_call_id": "call:user", "record_event": user_events.append},
        )
    )
    teardown = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:teardown",
                "record_event": teardown_events.append,
            },
        )
    )
    assert user.process_id is not None
    assert teardown.process_id is not None

    manager.kill_process(user.process_id)
    manager.kill_owned("host:a")

    assert len(user_events) == 1
    assert user_events[0].status == "killed"
    assert user_events[0].completion_reason == "user_tool_kill"
    assert "completion_reason" not in user_events[0].metadata
    assert teardown_events == []


def test_terminal_user_kill_does_not_relabel_existing_natural_terminal_fact(
    tmp_path,
) -> None:
    process = spawn_local_process(
        terminal_session_id="default",
        command="printf NATURAL_DONE",
        cwd=tmp_path,
        max_output_chars=1000,
        stdin_pipe=True,
        capture_cwd=False,
    )
    assert wait_for_process(process, timeout_seconds=2, kill_on_timeout=False)
    assert process.status is TerminalStatus.SUCCESS

    acquired = kill_process(process, reason=TerminalKillReason.USER)

    assert acquired is False
    assert process.status is TerminalStatus.SUCCESS
    assert process.exit_code == 0
    assert process.completion_reason is None


def test_terminal_completion_record_failure_returns_pending_and_retries_stable_event(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-retry",
        turn_id="turn:completion-retry",
        reply_id="reply:completion-retry",
    )
    attempts: list[str] = []
    recorded: list[TerminalProcessCompletedEvent] = []
    allow_success = False

    def recorder(event):
        attempts.append(event.id)
        if not allow_success:
            raise RuntimeError("synthetic pre-commit failure")
        recorded.append(event)
        return event

    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    result = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-retry",
                "record_event": recorder,
            },
        )
    )
    assert result.process_id is not None

    manager.kill_process(result.process_id)
    state = session.process_registry._processes[result.process_id]  # noqa: SLF001
    assert attempts
    assert recorded == []
    assert state.completion_record_state is TerminalCompletionRecordState.PENDING
    assert state.completion_event_recorded is False

    allow_success = True
    _maybe_record_completion_event(state)

    assert len(recorded) == 1
    assert len(set(attempts)) == 1
    assert recorded[0].id == state.completion_event_id
    assert state.completion_record_state is TerminalCompletionRecordState.RECORDED
    assert state.completion_event_recorded is True


def test_terminal_completion_uncertain_commit_is_confirmed_by_bounded_retry(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-uncertain",
        turn_id="turn:completion-uncertain",
        reply_id="reply:completion-uncertain",
    )
    event_log = InMemoryEventLog()
    calls = 0

    def recorder(event):
        nonlocal calls
        calls += 1
        stored = event_log.append(event)
        if calls == 1:
            raise RuntimeError("simulated lost commit acknowledgement")
        return stored

    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    result = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-uncertain",
                "record_event": recorder,
            },
        )
    )
    assert result.process_id is not None

    manager.kill_process(result.process_id, owner_host_session_id="host:a")
    state = session.process_registry._processes[result.process_id]  # noqa: SLF001
    deadline = time.monotonic() + 2
    while not state.completion_event_recorded and time.monotonic() < deadline:
        time.sleep(0.01)

    assert calls == 2
    assert len(event_log.iter()) == 1
    assert event_log.iter()[0].id == state.completion_event_id
    assert state.completion_record_state is TerminalCompletionRecordState.RECORDED


def test_terminal_pending_completion_is_not_pruned_by_ttl_or_capacity(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-pending-retention",
        turn_id="turn:completion-pending-retention",
        reply_id="reply:completion-pending-retention",
    )

    def recorder(_event):
        raise RuntimeError("persistent synthetic commit failure")

    manager = make_manager(
        tmp_path,
        finished_ttl_seconds=0,
        max_finished_processes=0,
    )
    session = manager.get_or_create(owner_host_session_id="host:a")
    result = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-pending-retention",
                "record_event": recorder,
            },
        )
    )
    assert result.process_id is not None

    manager.kill_process(result.process_id, owner_host_session_id="host:a")
    processes = manager.list_processes(owner_host_session_id="host:a")

    assert [process.process_id for process in processes] == [result.process_id]
    state = session.process_registry._processes[result.process_id]  # noqa: SLF001
    assert state.completion_event_recorded is False


def test_terminal_pending_completion_cap_blocks_new_yielded_process(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-pending-cap",
        turn_id="turn:completion-pending-cap",
        reply_id="reply:completion-pending-cap",
    )

    def recorder(_event):
        raise RuntimeError("persistent synthetic commit failure")

    manager = make_manager(
        tmp_path,
        max_live_processes=1,
        max_finished_processes=0,
        max_pending_completion_records=1,
        finished_ttl_seconds=0,
    )
    session = manager.get_or_create(owner_host_session_id="host:a")
    first = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-pending-cap:first",
                "record_event": recorder,
            },
        )
    )
    assert first.process_id is not None
    manager.kill_process(first.process_id, owner_host_session_id="host:a")
    assert manager.process_registry.pending_completion_count() == 1

    blocked = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-pending-cap:blocked",
                "record_event": recorder,
            },
        )
    )

    assert blocked.status is TerminalStatus.BLOCKED
    assert blocked.process_id is None
    assert blocked.error == "max pending terminal completion records reached: 1"
    assert manager.process_registry.pending_completion_count() == 1
    assert len(manager.process_registry._processes) == 1  # noqa: SLF001


def test_terminal_owner_release_drains_pending_completion_or_remains_retryable(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-owner-release",
        turn_id="turn:completion-owner-release",
        reply_id="reply:completion-owner-release",
    )
    available = False

    def recorder(event):
        if not available:
            raise RuntimeError("persistent synthetic commit failure")
        return event

    manager = make_manager(tmp_path, max_pending_completion_records=1)
    session = manager.get_or_create(owner_host_session_id="host:a")
    first = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-owner-release",
                "record_event": recorder,
            },
        )
    )
    assert first.process_id is not None
    manager.kill_process(first.process_id, owner_host_session_id="host:a")

    with pytest.raises(PendingTerminalCompletionError):
        manager.release_owner(
            "host:a",
            completion_drain_timeout_seconds=0.05,
        )

    assert manager.pending_completion_count(owner_host_session_id="host:a") == 1
    assert manager.session_count() == 1

    available = True
    manager.release_owner(
        "host:a",
        completion_drain_timeout_seconds=0.5,
    )

    assert manager.pending_completion_count(owner_host_session_id="host:a") == 0
    assert manager.session_count() == 0
    replacement = manager.get_or_create(owner_host_session_id="host:b")
    second = replacement.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": EventContext(
                    run_id="run:completion-new-owner",
                    turn_id="turn:completion-new-owner",
                    reply_id="reply:completion-new-owner",
                ),
                "tool_call_id": "call:completion-new-owner",
                "record_event": lambda event: event,
            },
        )
    )
    assert second.status is TerminalStatus.RUNNING
    assert second.process_id is not None
    manager.kill_process(second.process_id, owner_host_session_id="host:b")


def test_terminal_owner_release_deadline_is_not_blocked_by_stuck_recorder(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-blocked-recorder",
        turn_id="turn:completion-blocked-recorder",
        reply_id="reply:completion-blocked-recorder",
    )
    allow = threading.Event()
    entered = threading.Event()
    block_recorder = False

    def recorder(event):
        if not block_recorder:
            raise RuntimeError("initial synthetic commit failure")
        entered.set()
        allow.wait()
        return event

    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    started = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-blocked-recorder",
                "record_event": recorder,
            },
        )
    )
    assert started.process_id is not None
    manager.kill_process(started.process_id, owner_host_session_id="host:a")
    state = session.process_registry._processes[started.process_id]  # noqa: SLF001
    with state.lock:
        timer = state.completion_retry_timer
        state.completion_retry_timer = None
        state.completion_record_attempts = 99
    if timer is not None:
        timer.cancel()
    block_recorder = True

    release_started_at = time.monotonic()
    with pytest.raises(PendingTerminalCompletionError):
        manager.release_owner(
            "host:a",
            completion_drain_timeout_seconds=0.05,
        )
    release_elapsed = time.monotonic() - release_started_at

    assert entered.wait(timeout=1)
    assert release_elapsed < 0.2
    assert manager.pending_completion_count(owner_host_session_id="host:a") == 1

    allow.set()
    deadline = time.monotonic() + 1
    while not state.completion_event_recorded and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state.completion_event_recorded is True

    manager.release_owner(
        "host:a",
        completion_drain_timeout_seconds=0.05,
    )
    assert manager.pending_completion_count(owner_host_session_id="host:a") == 0


def test_terminal_recording_worker_start_failure_restores_pending_and_close_blocker(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = EventContext(
        run_id="run:completion-worker-start-failure",
        turn_id="turn:completion-worker-start-failure",
        reply_id="reply:completion-worker-start-failure",
    )

    def recorder(_event):
        raise RuntimeError("synthetic event store outage")

    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    started = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-worker-start-failure",
                "record_event": recorder,
            },
        )
    )
    assert started.process_id is not None
    manager.kill_process(started.process_id, owner_host_session_id="host:a")
    state = session.process_registry._processes[started.process_id]  # noqa: SLF001
    with state.lock:
        timer = state.completion_retry_timer
        state.completion_retry_timer = None
        state.completion_record_attempts = 99
    if timer is not None:
        timer.cancel()

    class FailingThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("cannot start worker")

    monkeypatch.setattr(process_mod, "Thread", FailingThread)

    with pytest.raises(PendingTerminalCompletionError):
        manager.release_owner(
            "host:a",
            completion_drain_timeout_seconds=0.01,
        )

    assert state.completion_record_state is TerminalCompletionRecordState.PENDING
    assert manager.pending_completion_count(owner_host_session_id="host:a") == 1
    assert manager.session_count() == 1


def test_terminal_retry_timer_start_failure_does_not_leave_fake_schedule(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = EventContext(
        run_id="run:completion-timer-start-failure",
        turn_id="turn:completion-timer-start-failure",
        reply_id="reply:completion-timer-start-failure",
    )

    class FailingTimer:
        daemon = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("cannot start retry timer")

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(process_mod, "Timer", FailingTimer)
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    started = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-timer-start-failure",
                "record_event": lambda _event: (_ for _ in ()).throw(
                    RuntimeError("synthetic event store outage")
                ),
            },
        )
    )
    assert started.process_id is not None

    manager.kill_process(started.process_id, owner_host_session_id="host:a")
    state = session.process_registry._processes[started.process_id]  # noqa: SLF001

    assert state.completion_record_state is TerminalCompletionRecordState.PENDING
    assert state.completion_retry_timer is None


def test_terminal_recording_worker_base_exception_returns_ownership_to_pending(
    tmp_path,
) -> None:
    ctx = EventContext(
        run_id="run:completion-worker-base-exception",
        turn_id="turn:completion-worker-base-exception",
        reply_id="reply:completion-worker-base-exception",
    )
    raise_base_exception = False

    def recorder(_event):
        if raise_base_exception:
            raise KeyboardInterrupt
        raise RuntimeError("initial synthetic event store outage")

    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")
    started = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:completion-worker-base-exception",
                "record_event": recorder,
            },
        )
    )
    assert started.process_id is not None
    manager.kill_process(started.process_id, owner_host_session_id="host:a")
    state = session.process_registry._processes[started.process_id]  # noqa: SLF001
    with state.lock:
        timer = state.completion_retry_timer
        state.completion_retry_timer = None
        state.completion_record_attempts = 99
    if timer is not None:
        timer.cancel()
    raise_base_exception = True

    with pytest.raises(PendingTerminalCompletionError):
        manager.release_owner(
            "host:a",
            completion_drain_timeout_seconds=0.02,
        )

    assert state.completion_record_state is TerminalCompletionRecordState.PENDING
    assert manager.pending_completion_count(owner_host_session_id="host:a") == 1


def test_terminal_runtime_lifetime_watchdog_suppresses_completion_event(tmp_path) -> None:
    ctx = EventContext(run_id="run:watchdog", turn_id="turn:watchdog", reply_id="reply:watchdog")
    events = []
    manager = make_manager(tmp_path)
    session = manager.get_or_create(owner_host_session_id="host:a")

    result = session.execute(
        TerminalRequest(
            command="sleep 5",
            yield_time_ms=0,
            max_lifetime_seconds=1,
            metadata={
                "origin_event_context": ctx,
                "tool_call_id": "call:watchdog",
                "record_event": events.append,
            },
        )
    )
    assert result.process_id is not None

    deadline = time.monotonic() + 3
    final = manager.poll_process(result.process_id, owner_host_session_id="host:a")
    while final.status is TerminalStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(0.05)
        final = manager.poll_process(result.process_id, owner_host_session_id="host:a")

    assert final.status is TerminalStatus.KILLED
    assert events == []


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


def test_terminal_runtime_large_output_keeps_redacted_full_output_text(tmp_path) -> None:
    session = make_session(tmp_path)

    result = run(
        session,
        "python -c 'print(\"HEAD\"); print(\"API_KEY=secret-token\"); print(\"x\" * 1100000); print(\"TAIL\")'",
        max_output_chars=120,
    )

    assert result.status is TerminalStatus.SUCCESS
    assert result.truncated is True
    assert result.full_output_text is not None
    assert "OUTPUT TRUNCATED" in result.output
    assert "HEAD" in result.output
    assert "TAIL" in result.output
    assert "HEAD" in result.full_output_text
    assert "TAIL" in result.full_output_text
    assert "API_KEY=[REDACTED]" in result.full_output_text
    assert "secret-token" not in result.full_output_text


def test_terminal_runtime_yielded_large_output_log_keeps_full_output_text(tmp_path) -> None:
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
    log = manager.log_process(result.process_id, max_output_chars=100)

    assert final.status is TerminalStatus.SUCCESS
    assert final.truncated is True
    assert final.full_output_text is not None
    assert log.truncated is True
    assert log.full_output_text == final.full_output_text
    assert "START" in final.full_output_text
    assert "END" in final.full_output_text


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
