import os
import subprocess
from types import SimpleNamespace

import pytest

from pulsara_agent import desktop_maintenance as module


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "data"
    home.mkdir(mode=0o700)
    (home / "desktop").mkdir(mode=0o700)
    resources = root / "resources"
    resources.mkdir()
    monkeypatch.setenv("PULSARA_HOME", str(home))
    return home, resources


def test_reset_removes_invalid_settings_credentials_and_files_preserving_external_project_and_lock(
    layout,
):
    home, resources = layout
    (home / "local-settings.yaml").write_text("not a valid configuration: [secret-key")
    (home / "workspaces").mkdir()
    (home / "workspaces/quick-file").write_text("quick work")
    project = home.parent / "project"
    project.mkdir()
    (project / "file").write_text("external")
    (home / "link").symlink_to(project)
    with module.Maintenance(home, resources, resources) as operation:
        inode = (home / "desktop/kernel.lock").stat().st_ino
        operation.clear()
        assert sorted(path.name for path in home.iterdir()) == ["desktop"]
        assert (project / "file").read_text() == "external"
        assert (home / "desktop/kernel.lock").stat().st_ino == inode
        from pulsara_agent.desktop_runtime import acquire_kernel_lock

        with pytest.raises(BlockingIOError):
            acquire_kernel_lock()
    from pulsara_agent.settings import LocalSettingsStore

    assert LocalSettingsStore().read().model_api_keys == ()


def test_reset_busy_kernel_leaves_all_data(layout):
    home, resources = layout
    (home / "local-settings.yaml").write_text("keep")
    from pulsara_agent.desktop_runtime import acquire_kernel_lock

    fd = acquire_kernel_lock()
    try:
        with pytest.raises(BlockingIOError):
            with module.Maintenance(home, resources, resources):
                pytest.fail("maintenance admitted a busy kernel")
        assert (home / "local-settings.yaml").read_text() == "keep"
    finally:
        os.close(fd)


def test_reset_refuses_symlinked_home_and_resource_ancestor(layout, monkeypatch):
    home, resources = layout
    alias = home.parent / "alias"
    alias.symlink_to(home)
    monkeypatch.setenv("PULSARA_HOME", str(alias))
    with pytest.raises(ValueError):
        with module.Maintenance(alias, resources, resources):
            pytest.fail("linked target admitted")
    with pytest.raises(ValueError):
        module.validate_home(home.parent, resources)


@pytest.mark.parametrize("stops", [(1, 0), (1, 1), (0,)])
def test_postgres_fast_then_immediate_and_stopped_verification(
    layout, monkeypatch, stops
):
    home, resources = layout
    data = home / "desktop/postgres"
    data.mkdir(mode=0o700)
    for name, text in [
        ("PG_VERSION", "17"),
        ("postmaster.pid", f"123\n{data}\n0\n5432\n{home}/desktop/socket\n"),
    ]:
        (data / name).write_text(text)
        (data / name).chmod(0o600)
    calls = []
    remaining = iter(stops)
    stopped = False

    def run(args, **kwargs):
        nonlocal stopped
        calls.append(args)
        if args[1] == "status":
            code = 3 if stopped else 0
        else:
            code = next(remaining)
            stopped = code == 0
        return SimpleNamespace(returncode=code, stdout="", stderr="busy")

    monkeypatch.setattr(subprocess, "run", run)
    if stops == (1, 1):
        with pytest.raises(RuntimeError, match="尚未确认停止"):
            with module.Maintenance(home, resources, resources):
                pytest.fail("live database admitted")
        assert data.exists()
    else:
        with module.Maintenance(home, resources, resources) as operation:
            operation.clear()
        assert not data.exists()
    assert [args[-1] for args in calls if args[1] == "stop"] == (
        ["fast"] if stops == (0,) else ["fast", "immediate"]
    )


def test_uninstall_only_terminates_verified_kernel_and_escalates(layout, monkeypatch):
    home, resources = layout
    calls = []

    class Process:
        def __init__(self, exe):
            self.executable = exe

        def uids(self):
            return SimpleNamespace(real=os.getuid())

        def cmdline(self):
            return ["python", "-m", "pulsara_agent.desktop_runtime"]

        def exe(self):
            return str(self.executable)

        def environ(self):
            return {"PULSARA_HOME": str(home)}

        def children(self, recursive):
            return []

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    target = Process(resources / "python")
    monkeypatch.setattr(
        module.psutil,
        "process_iter",
        lambda: [Process(home.parent / "foreign/python"), target],
    )
    waits = iter([([], [target]), ([target], [])])
    monkeypatch.setattr(
        module.psutil, "wait_procs", lambda *args, **kwargs: next(waits)
    )
    module.stop_kernel(resources, home)
    assert calls == ["terminate", "kill"]
