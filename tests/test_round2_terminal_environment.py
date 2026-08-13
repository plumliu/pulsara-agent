from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep

import pytest

import pulsara_agent.terminal_process.environment as environment_module
from pulsara_agent.terminal_process.environment import (
    TerminalEnvConfig,
    TerminalEnvironmentOwner,
    TerminalShellConfig,
    detect_terminal_shell,
    find_nearest_venv_bin,
)
from pulsara_agent.terminal_process.manager import ProcessRegistry


def test_round2_environment_is_default_deny_and_diagnostic_has_no_values(
    tmp_path: Path,
) -> None:
    parent = {
        "HOME": str(tmp_path),
        "USER": "tester",
        "SHELL": "/bin/sh",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": "sk-this-must-not-escape",
        "HTTP_PROXY": "http://capability.invalid",
        "SSH_AUTH_SOCK": "/private/socket",
        "PYTHONPATH": "/private/import-hook",
        "CUSTOM_SAFE": "ordinary",
        "CUSTOM_SECRET": "sk-this-inherited-value-must-be-scanned",
        "CUSTOM_PASSTHROUGH": "sk-explicit-high-authority-value",
    }
    owner = TerminalEnvironmentOwner(
        tmp_path,
        config=TerminalEnvConfig(
            enable_shell_snapshot=False,
            inherit_allowlist=frozenset({"CUSTOM_SAFE", "CUSTOM_SECRET"}),
            passthrough_names=frozenset({"CUSTOM_PASSTHROUGH"}),
        ),
        parent_env=parent,
    )
    environment = owner.build(cwd=tmp_path)
    assert environment.values["CUSTOM_SAFE"] == "ordinary"
    assert "CUSTOM_SECRET" not in environment.values
    assert environment.values["CUSTOM_PASSTHROUGH"].startswith("sk-")
    for forbidden in ("OPENAI_API_KEY", "HTTP_PROXY", "SSH_AUTH_SOCK", "PYTHONPATH"):
        assert forbidden not in environment.values
    serialized_diagnostic = repr(environment.diagnostic)
    assert "sk-this" not in serialized_diagnostic
    assert "capability.invalid" not in serialized_diagnostic
    assert "ordinary" not in serialized_diagnostic
    owner.close(timeout_seconds=1)


def test_round2_nearest_venv_and_shell_fallback_are_bounded_to_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "a" / "b"
    nested.mkdir(parents=True)
    venv_bin = workspace / "a" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    assert find_nearest_venv_bin(nested, workspace) == venv_bin
    assert find_nearest_venv_bin(tmp_path, workspace) is None

    shell = detect_terminal_shell({"SHELL": "/definitely/missing"})
    assert shell.path.is_absolute()
    assert shell.path.is_file()
    assert shell.command_argv("true")[-2:] == ("-c", "true")


def test_round2_shell_snapshot_is_single_flight_and_only_success_is_cached(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    calls = 0
    call_lock = Lock()

    class Owner(TerminalEnvironmentOwner):
        def _run_probe(self, shell, attempt):  # type: ignore[override]
            del shell, attempt
            nonlocal calls
            with call_lock:
                calls += 1
            entered.set()
            release.wait(2)
            return {"PATH": "/profile/bin", "LANG": "C.UTF-8"}

    owner = Owner(
        tmp_path,
        config=TerminalEnvConfig(
            shell_snapshot_timeout_seconds=2,
            shell_snapshot_ttl_seconds=60,
        ),
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": "/bin/sh",
            "PATH": "/usr/bin:/bin",
        },
    )
    shell = TerminalShellConfig(Path("/bin/sh"), "sh")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(owner._snapshot, shell) for _ in range(4)]  # noqa: SLF001
        assert entered.wait(1)
        sleep(0.05)
        release.set()
        results = [future.result(timeout=2) for future in futures]
    assert calls == 1
    assert all(result[0] is not None and result[1] is None for result in results)
    cached, error = owner._snapshot(shell)  # noqa: SLF001
    assert cached is not None and error is None
    assert calls == 1
    owner.close(timeout_seconds=1)


def test_round2_terminal_child_receives_nearest_venv_and_not_parent_secret(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    (nested / ".venv" / "bin").mkdir(parents=True)
    parent = dict(os.environ)
    parent["OPENAI_API_KEY"] = "sk-private-parent"
    owner = TerminalEnvironmentOwner(
        workspace,
        config=TerminalEnvConfig(enable_shell_snapshot=False),
        parent_env=parent,
    )
    environment = owner.build(cwd=nested)
    assert environment.values["PATH"].split(os.pathsep)[0] == str(
        nested / ".venv" / "bin"
    )
    assert "OPENAI_API_KEY" not in environment.values
    owner.close(timeout_seconds=1)


def test_round2_shell_probe_timeout_physically_terminates_its_group(
    tmp_path: Path,
) -> None:
    shell = tmp_path / "slow-shell"
    shell.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    shell.chmod(0o755)
    owner = TerminalEnvironmentOwner(
        tmp_path,
        config=TerminalEnvConfig(shell_snapshot_timeout_seconds=0.1),
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": str(shell),
            "PATH": "/usr/bin:/bin",
        },
    )
    started = monotonic()
    snapshot, error = owner._snapshot(  # noqa: SLF001
        TerminalShellConfig(shell, "slow-shell")
    )
    assert snapshot is None
    assert error == "PROBE_TIMEOUT"
    assert monotonic() - started < 2
    owner.close(timeout_seconds=1)


def test_round2_shell_snapshot_ttl_and_startup_signature_invalidate_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    startup = home / ".zshrc"
    startup.write_text("# first\n", encoding="utf-8")
    calls = 0

    class Owner(TerminalEnvironmentOwner):
        def _run_probe(self, shell, attempt):  # type: ignore[override]
            del shell, attempt
            nonlocal calls
            calls += 1
            return {"PATH": f"/profile/{calls}"}

    owner = Owner(
        tmp_path,
        config=TerminalEnvConfig(shell_snapshot_ttl_seconds=0.05),
        parent_env={
            "HOME": str(home),
            "USER": "tester",
            "SHELL": "/bin/sh",
            "PATH": "/usr/bin:/bin",
        },
    )
    shell = TerminalShellConfig(Path("/bin/sh"), "zsh")
    first, _ = owner._snapshot(shell)  # noqa: SLF001
    second, _ = owner._snapshot(shell)  # noqa: SLF001
    assert first == second and calls == 1
    startup.write_text("# second and different size\n", encoding="utf-8")
    changed, _ = owner._snapshot(shell)  # noqa: SLF001
    assert changed is not None and changed.values["PATH"] == "/profile/2"
    sleep(0.06)
    expired, _ = owner._snapshot(shell)  # noqa: SLF001
    assert expired is not None and expired.values["PATH"] == "/profile/3"
    owner.close(timeout_seconds=1)


def test_round2_probe_nonzero_and_oversize_are_typed_uncached_fallbacks(
    tmp_path: Path,
) -> None:
    nonzero = tmp_path / "nonzero-shell"
    nonzero.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    nonzero.chmod(0o755)
    owner = TerminalEnvironmentOwner(
        tmp_path,
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": str(nonzero),
            "PATH": "/usr/bin:/bin",
        },
    )
    snapshot, error = owner._snapshot(  # noqa: SLF001
        TerminalShellConfig(nonzero, "sh")
    )
    assert snapshot is None and error == "PROBE_FAILED"
    owner.close(timeout_seconds=1)

    oversize = tmp_path / "oversize-shell"
    oversize.write_text(
        "#!/bin/sh\nprintf 'PULSARA_TERMINAL_ENV_V1\\0'; head -c 1100000 /dev/zero\n",
        encoding="utf-8",
    )
    oversize.chmod(0o755)
    owner = TerminalEnvironmentOwner(
        tmp_path,
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": str(oversize),
            "PATH": "/usr/bin:/bin",
        },
    )
    snapshot, error = owner._snapshot(  # noqa: SLF001
        TerminalShellConfig(oversize, "sh")
    )
    assert snapshot is None and error == "PROBE_OVERSIZE"
    owner.close(timeout_seconds=1)


def test_round2_environment_owner_close_joins_inflight_probe_group(
    tmp_path: Path,
) -> None:
    shell = tmp_path / "close-shell"
    shell.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    shell.chmod(0o755)
    owner = TerminalEnvironmentOwner(
        tmp_path,
        config=TerminalEnvConfig(shell_snapshot_timeout_seconds=5),
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": str(shell),
            "PATH": "/usr/bin:/bin",
        },
    )
    finished = Event()
    result: list[tuple[object, str | None]] = []

    def probe() -> None:
        result.append(
            owner._snapshot(TerminalShellConfig(shell, "sh"))  # noqa: SLF001
        )
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(probe)
        deadline = monotonic() + 1
        while True:
            with owner._lock:  # noqa: SLF001
                attempts = tuple(owner._attempts.values())  # noqa: SLF001
                started = bool(attempts and attempts[0].process is not None)
            if started:
                break
            assert monotonic() < deadline
            sleep(0.01)
        owner.close(timeout_seconds=2)
        future.result(timeout=2)
    assert finished.is_set()
    assert result and result[0][0] is None


def test_round2_environment_close_linearizes_popen_and_waits_attempt_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TerminalEnvironmentOwner(
        tmp_path,
        config=TerminalEnvConfig(shell_snapshot_timeout_seconds=2),
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": "/bin/sh",
            "PATH": "/usr/bin:/bin",
        },
    )
    entered_popen = Event()
    release_popen = Event()
    spawned: list[object] = []
    real_popen = environment_module.subprocess.Popen

    def delayed_popen(*args, **kwargs):
        entered_popen.set()
        assert release_popen.wait(2)
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(environment_module.subprocess, "Popen", delayed_popen)
    shell = TerminalShellConfig(Path("/bin/sh"), "sh")
    with ThreadPoolExecutor(max_workers=2) as executor:
        probe = executor.submit(owner._snapshot, shell)  # noqa: SLF001
        assert entered_popen.wait(1)
        close = executor.submit(owner.close, timeout_seconds=2)
        sleep(0.05)
        assert close.done() is False
        release_popen.set()
        close.result(timeout=2)
        snapshot, error = probe.result(timeout=2)

    assert snapshot is None
    assert error == "PROBE_TIMEOUT"
    assert spawned
    assert all(process.poll() is not None for process in spawned)
    with owner._lock:  # noqa: SLF001
        assert owner._attempts == {}  # noqa: SLF001


def test_round2_pipe_and_pty_receive_the_same_bounded_environment(
    tmp_path: Path,
) -> None:
    owner_id = "host:environment-parity"
    environment_owner = TerminalEnvironmentOwner(
        tmp_path,
        config=TerminalEnvConfig(
            enable_shell_snapshot=False,
            inherit_allowlist=frozenset({"CUSTOM_VISIBLE"}),
        ),
        parent_env={
            "HOME": str(tmp_path),
            "USER": "tester",
            "SHELL": "/bin/sh",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "CUSTOM_VISIBLE": "ordinary",
            "OPENAI_API_KEY": "sk-private-parent-value",
        },
    )
    environment = environment_owner.build(cwd=tmp_path)
    registry = ProcessRegistry()
    registry.activate_owner(owner_id)
    outputs: list[str] = []
    command = "printf '%s' \"$CUSTOM_VISIBLE|${OPENAI_API_KEY-unset}\""
    for tty in (False, True):
        state, yielded, _cwd = registry.exec_with_yield(
            terminal_session_id="default",
            command=command,
            cwd=tmp_path,
            yield_time_ms=2_000,
            tty=tty,
            max_lifetime_seconds=5,
            owner_host_session_id=owner_id,
            shell_argv=environment.shell.command_argv(command),
            decision_deadline_monotonic=monotonic() + 5,
            env=environment.values,
        )
        assert yielded is False
        outputs.append(state.output.snapshot(maximum_chars=512).text)
    assert outputs == ["ordinary|unset", "ordinary|unset"]
    registry.release_owner(owner_id, timeout_seconds=2)
    environment_owner.close(timeout_seconds=1)
