import os
import stat
import subprocess
import time

from pulsara_agent.runtime.terminal.env import (
    TerminalEnvBuilder,
    TerminalEnvConfig,
    capture_shell_env_snapshot,
    find_nearest_venv_bin,
    sanitize_subprocess_env,
)
from pulsara_agent.runtime.terminal import env as terminal_env
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


def test_sanitize_subprocess_env_strips_provider_and_secret_envs() -> None:
    env, diagnostics = sanitize_subprocess_env(
        {
            "HOME": "/home/user",
            "PATH": "/usr/bin",
            "LANG": "en_US.UTF-8",
            "PULSARA_API_KEY": "pulsara-secret",
            "OPENAI_API_KEY": "openai-secret",
            "FOO_TOKEN": "foo-secret",
            "PWD": "/tmp/old",
        },
        config=TerminalEnvConfig(enable_shell_snapshot=False),
    )

    assert env == {
        "HOME": "/home/user",
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
    }
    assert diagnostics["sanitized_env_removed_count"] == 4


def test_sanitize_subprocess_env_preserves_operational_and_toolchain_names() -> None:
    source = {
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "XAUTHORITY": "/run/user/501/Xauthority.ABCDEF",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/501/bus,guid=a1b2c3d4e5f6",
        "NVM_DIR": "/Users/me/.nvm",
        "PYENV_ROOT": "/Users/me/.pyenv",
        "VOLTA_HOME": "/Users/me/.volta",
        "PNPM_HOME": "/Users/me/Library/pnpm",
    }

    env, diagnostics = sanitize_subprocess_env(
        source,
        config=TerminalEnvConfig(enable_shell_snapshot=False),
    )

    assert env == source
    assert diagnostics["sanitized_env_removed_count"] == 0


def test_sanitize_subprocess_env_strips_loader_and_hook_vars() -> None:
    env, _ = sanitize_subprocess_env(
        {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/tmp/evil",
            "NODE_OPTIONS": "--require /tmp/hook.js",
            "LD_PRELOAD": "/tmp/lib.so",
            "DYLD_INSERT_LIBRARIES": "/tmp/lib.dylib",
        },
        config=TerminalEnvConfig(enable_shell_snapshot=False),
    )

    assert env == {"PATH": "/usr/bin"}


def test_sanitize_subprocess_env_value_scan_is_shape_specific_not_entropy_based() -> None:
    env, diagnostics = sanitize_subprocess_env(
        {
            "LANG": "Bearer definitely-secret",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/501/bus,guid=a1b2c3d4e5f6",
            "XAUTHORITY": "/run/user/501/Xauthority.ABCDEF123456",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        },
        config=TerminalEnvConfig(enable_shell_snapshot=False),
    )

    assert "LANG" not in env
    assert env["DBUS_SESSION_BUS_ADDRESS"].endswith("guid=a1b2c3d4e5f6")
    assert env["XAUTHORITY"] == "/run/user/501/Xauthority.ABCDEF123456"
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert diagnostics["sanitized_env_secret_value_removed_count"] == 1


def test_sanitize_subprocess_env_does_not_value_scan_path_structural_vars() -> None:
    source = {
        "PATH": "/Users/me/.cache/sk-1a2b3c4d5e6f/bin:/usr/bin",
        "HOME": "/Users/sk-1a2b3c4d5e6f",
        "NVM_DIR": "/Users/me/.nvm/sk-1a2b3c4d5e6f",
        "PYENV_ROOT": "/Users/me/.pyenv/sk-1a2b3c4d5e6f",
    }

    env, diagnostics = sanitize_subprocess_env(
        source,
        config=TerminalEnvConfig(enable_shell_snapshot=False),
    )

    assert env == source
    assert diagnostics["sanitized_env_secret_value_removed_count"] == 0


def test_passthrough_names_are_exact_user_extensions() -> None:
    env, _ = sanitize_subprocess_env(
        {
            "CUSTOM_FLAG": "1",
            "CUSTOM_FLAG_EXTRA": "2",
            "FOO_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz",
        },
        config=TerminalEnvConfig(
            enable_shell_snapshot=False,
            inherit_allowlist=frozenset({"CUSTOM_FLAG"}),
            passthrough_names=frozenset({"FOO_TOKEN"}),
        ),
    )

    assert env == {
        "CUSTOM_FLAG": "1",
        "FOO_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz",
    }


def test_capture_shell_env_snapshot_filters_profile_noise_and_secrets(tmp_path) -> None:
    fake_shell = tmp_path / "fake-sh"
    fake_shell.write_text(
        "#!/bin/sh\n"
        "printf 'profile-noise\\n'\n"
        "printf '__PULSARA_ENV_START__\\0'\n"
        "printf 'PATH=/custom/bin:/usr/bin\\0'\n"
        "printf 'OPENAI_API_KEY=secret\\0'\n"
        "printf 'NVM_DIR=/Users/me/.nvm\\0'\n",
        encoding="utf-8",
    )
    fake_shell.chmod(fake_shell.stat().st_mode | stat.S_IXUSR)

    snapshot = capture_shell_env_snapshot(
        shell=TerminalShellConfig(path=fake_shell),
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        config=TerminalEnvConfig(enable_shell_snapshot=True, shell_snapshot_timeout_seconds=2),
        now=123.0,
    )

    assert snapshot.error is None
    assert snapshot.env["PATH"] == "/custom/bin:/usr/bin"
    assert snapshot.env["NVM_DIR"] == "/Users/me/.nvm"
    assert "OPENAI_API_KEY" not in snapshot.env
    assert "profile-noise" not in snapshot.env


def test_capture_shell_env_snapshot_uses_login_interactive_probe_for_zshrc_tools(tmp_path) -> None:
    fake_zsh = tmp_path / "zsh"
    fake_zsh.write_text(
        "#!/bin/sh\n"
        "has_login=0\n"
        "has_interactive=0\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -l) has_login=1 ;;\n"
        "    -i) has_interactive=1 ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        "printf '__PULSARA_ENV_START__\\0'\n"
        "if [ \"$has_login\" = 1 ] && [ \"$has_interactive\" = 1 ]; then\n"
        "  printf 'PATH=/zshrc-tool/bin:/usr/bin\\0'\n"
        "else\n"
        "  printf 'PATH=/login-only/bin:/usr/bin\\0'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_zsh.chmod(fake_zsh.stat().st_mode | stat.S_IXUSR)

    snapshot = capture_shell_env_snapshot(
        shell=TerminalShellConfig(path=fake_zsh),
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        config=TerminalEnvConfig(enable_shell_snapshot=True, shell_snapshot_timeout_seconds=2),
        now=123.0,
    )

    assert snapshot.error is None
    assert snapshot.env["PATH"].split(os.pathsep)[0] == "/zshrc-tool/bin"


def test_terminal_env_builder_uses_snapshot_cache_until_startup_file_changes(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".profile"
    profile.write_text("# first\n", encoding="utf-8")
    path_value = home / "path_value"
    path_value.write_text("/snapshot-1:/usr/bin", encoding="utf-8")
    fake_shell = tmp_path / "fake-sh"
    fake_shell.write_text(
        "#!/bin/sh\n"
        "snapshot_path=$(/bin/cat \"$HOME/path_value\")\n"
        "printf '__PULSARA_ENV_START__\\0'\n"
        "printf 'PATH='\"$snapshot_path\"'\\0'\n",
        encoding="utf-8",
    )
    fake_shell.chmod(fake_shell.stat().st_mode | stat.S_IXUSR)
    builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(
            enable_shell_snapshot=True,
            shell_snapshot_ttl_seconds=300,
        ),
        parent_env={"HOME": str(home), "PATH": "/parent/bin"},
    )
    shell = TerminalShellConfig(path=fake_shell)

    first = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    second = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    first_mtime = profile.stat().st_mtime_ns
    profile.write_text("# second with a different signature\n", encoding="utf-8")
    path_value.write_text("/snapshot-2:/usr/bin", encoding="utf-8")
    os.utime(profile, ns=(first_mtime + 1_000_000_000, first_mtime + 1_000_000_000))
    third = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)

    assert first.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert second.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert third.env["PATH"].split(os.pathsep)[0] == "/snapshot-2"


def test_terminal_env_builder_uses_snapshot_cache_until_zshrc_changes(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    zshrc = home / ".zshrc"
    zshrc.write_text("# first\n", encoding="utf-8")
    path_value = home / "path_value"
    path_value.write_text("/snapshot-1:/usr/bin", encoding="utf-8")
    fake_zsh = tmp_path / "zsh"
    fake_zsh.write_text(
        "#!/bin/sh\n"
        "snapshot_path=$(/bin/cat \"$HOME/path_value\")\n"
        "printf '__PULSARA_ENV_START__\\0'\n"
        "printf 'PATH='\"$snapshot_path\"'\\0'\n",
        encoding="utf-8",
    )
    fake_zsh.chmod(fake_zsh.stat().st_mode | stat.S_IXUSR)
    builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=True, shell_snapshot_ttl_seconds=300),
        parent_env={"HOME": str(home), "PATH": "/parent/bin"},
    )
    shell = TerminalShellConfig(path=fake_zsh)

    first = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    second = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    first_mtime = zshrc.stat().st_mtime_ns
    zshrc.write_text("# second with zshrc tools\n", encoding="utf-8")
    path_value.write_text("/snapshot-2:/usr/bin", encoding="utf-8")
    os.utime(zshrc, ns=(first_mtime + 1_000_000_000, first_mtime + 1_000_000_000))
    third = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)

    assert first.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert second.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert third.env["PATH"].split(os.pathsep)[0] == "/snapshot-2"


def test_terminal_env_builder_snapshot_failure_falls_back_to_parent_and_sane_path(tmp_path) -> None:
    missing_shell = tmp_path / "missing-shell"
    builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=True),
        parent_env={"HOME": str(tmp_path), "PATH": "/parent/bin"},
    )

    result = builder.build(
        cwd=tmp_path,
        workspace_root=tmp_path,
        shell=TerminalShellConfig(path=missing_shell),
    )

    assert result.env["PATH"].split(os.pathsep)[0] == "/parent/bin"
    assert "/usr/bin" in result.env["PATH"].split(os.pathsep)
    assert result.diagnostics["shell_snapshot_used"] is False
    assert result.diagnostics["shell_snapshot_error"]


def test_terminal_env_builder_snapshot_can_be_disabled(tmp_path) -> None:
    fake_shell = tmp_path / "fake-sh"
    fake_shell.write_text(
        "#!/bin/sh\nprintf '__PULSARA_ENV_START__\\0PATH=/snapshot/bin\\0'\n",
        encoding="utf-8",
    )
    fake_shell.chmod(fake_shell.stat().st_mode | stat.S_IXUSR)
    builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=False),
        parent_env={"HOME": str(tmp_path), "PATH": "/parent/bin"},
    )

    result = builder.build(
        cwd=tmp_path,
        workspace_root=tmp_path,
        shell=TerminalShellConfig(path=fake_shell),
    )

    assert result.env["PATH"].split(os.pathsep)[0] == "/parent/bin"
    assert result.diagnostics["shell_snapshot_used"] is False
    assert result.diagnostics["shell_snapshot_error"] is None


def test_terminal_env_builder_refreshes_snapshot_after_ttl(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path_value = home / "path_value"
    path_value.write_text("/snapshot-1:/usr/bin", encoding="utf-8")
    fake_shell = tmp_path / "fake-sh"
    fake_shell.write_text(
        "#!/bin/sh\n"
        "snapshot_path=$(/bin/cat \"$HOME/path_value\")\n"
        "printf '__PULSARA_ENV_START__\\0'\n"
        "printf 'PATH='\"$snapshot_path\"'\\0'\n",
        encoding="utf-8",
    )
    fake_shell.chmod(fake_shell.stat().st_mode | stat.S_IXUSR)
    now = 100.0
    builder = TerminalEnvBuilder(
        config=TerminalEnvConfig(enable_shell_snapshot=True, shell_snapshot_ttl_seconds=10),
        parent_env={"HOME": str(home), "PATH": "/parent/bin"},
        time_fn=lambda: now,
    )
    shell = TerminalShellConfig(path=fake_shell)

    first = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    path_value.write_text("/snapshot-2:/usr/bin", encoding="utf-8")
    second = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)
    now = 111.0
    third = builder.build(cwd=tmp_path, workspace_root=tmp_path, shell=shell)

    assert first.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert second.env["PATH"].split(os.pathsep)[0] == "/snapshot-1"
    assert third.env["PATH"].split(os.pathsep)[0] == "/snapshot-2"


def test_read_bounded_stdout_returns_on_eof_before_process_exit() -> None:
    proc = subprocess.Popen(
        ["python", "-c", "import os, time; os.close(1); time.sleep(0.2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()

    output = terminal_env._read_bounded_stdout(proc, timeout_seconds=2)
    elapsed = time.monotonic() - started
    proc.wait(timeout=2)

    assert output == b""
    assert elapsed < 1


def test_find_nearest_venv_bin_prefers_package_local_venv(tmp_path) -> None:
    root_venv = tmp_path / ".venv" / "bin"
    package_venv = tmp_path / "packages" / "foo" / ".venv" / "bin"
    package_cwd = tmp_path / "packages" / "foo" / "src"
    root_venv.mkdir(parents=True)
    package_venv.mkdir(parents=True)
    package_cwd.mkdir(parents=True)

    assert find_nearest_venv_bin(package_cwd, tmp_path) == package_venv
    assert find_nearest_venv_bin(tmp_path / "packages", tmp_path) == root_venv
    assert find_nearest_venv_bin(tmp_path.parent, tmp_path) is None
