from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="WSL uninstall uses Linux locks")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    from pulsara_agent import desktop_guest_ops

    source = Path(__file__).resolve().parents[1] / "desktop/assets/wsl-uninstall.py"
    spec = importlib.util.spec_from_file_location("wsl_uninstall", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path.resolve()
    home.chmod(0o700)
    data = home / ".pulsara"
    data.mkdir(mode=0o700)
    monkeypatch.setenv("PULSARA_HOME", str(data))
    monkeypatch.setattr(desktop_guest_ops, "require_native_ext4", lambda _: None)
    (data / "settings").write_text("personal settings")
    quick = data / "workspaces/quick-example"
    quick.mkdir(parents=True)
    (quick / "work.txt").write_text("quick work")
    project = home / "project"
    project.mkdir()
    (project / "work.txt").write_text("project work")
    base = home / ".local/share/pulsara/runtime"
    base.mkdir(parents=True, mode=0o700)
    versions = [base / version for version in ("0.1.0-alpha.15", "0.1.1-alpha.1")]
    for version in versions:
        version.mkdir(mode=0o700)
        (version / ".pulsara-payload.sha256").write_text("a" * 64)
        (version / "postgres/bin").mkdir(parents=True)
    return module, home, data, versions, project


@pytest.mark.parametrize("mode", ["keep", "all"])
def test_uninstall_removes_every_runtime_and_only_selected_data(layout, mode):
    module, home, data, versions, project = layout
    imports = data / "imports/attachment"
    imports.mkdir(parents=True)
    (imports / "file").write_text("attachment")
    imports.chmod(0o500)
    (data / "project-link").symlink_to(project, target_is_directory=True)
    module.uninstall(versions[-1], home, mode)
    assert all(not version.exists() for version in versions)
    assert (project / "work.txt").read_text() == "project work"
    if mode == "keep":
        assert (data / "settings").read_text() == "personal settings"
        assert (data / "workspaces/quick-example/work.txt").read_text() == "quick work"
        imports.chmod(0o700)
    else:
        assert not data.exists()


@pytest.mark.parametrize("lock_name", ["kernel.lock", "pg-lifetime.lock", ".install.lock"])
def test_busy_owner_prevents_any_deletion(layout, lock_name):
    import fcntl
    import os
    from pulsara_agent.desktop_guest_ops import OperationError

    module, home, data, versions, _ = layout
    parent = versions[-1].parent if lock_name == ".install.lock" else data / "desktop"
    parent.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(parent / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((RuntimeError, OSError, OperationError)):
            module.uninstall(versions[-1], home, "all")
        assert (data / "settings").exists()
        assert all(version.exists() for version in versions)
    finally:
        os.close(descriptor)


def test_resource_symlink_does_not_authorize_outside_deletion(layout):
    module, home, data, versions, project = layout
    (versions[-1].parent / "foreign").symlink_to(project, target_is_directory=True)
    with pytest.raises(ValueError):
        module.uninstall(versions[-1], home, "all")
    assert (project / "work.txt").exists()
    assert (data / "settings").exists()


def test_home_cannot_be_the_personal_data_target(layout, monkeypatch):
    module, home, _, versions, project = layout
    monkeypatch.setenv("PULSARA_HOME", str(home))
    with pytest.raises(ValueError, match="范围"):
        module.uninstall(versions[-1], home, "all")
    assert (project / "work.txt").exists()
    assert all(version.exists() for version in versions)


def test_live_postgres_blocks_cleanup_even_without_lock(layout):
    module, home, data, versions, _ = layout
    database = data / "desktop/postgres"
    database.mkdir(parents=True, mode=0o700)
    database.parent.chmod(0o700)
    (database / "PG_VERSION").write_text("17")
    (database / "PG_VERSION").chmod(0o600)
    pgctl = versions[-1] / "postgres/bin/pg_ctl"
    pgctl.write_text("#!/bin/sh\nexit 0\n")
    pgctl.chmod(0o700)
    with pytest.raises(Exception, match="active"):
        module.uninstall(versions[-1], home, "all")
    assert database.exists()
    assert all(version.exists() for version in versions)
