from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import signal
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the WSL guest helper uses Linux flock and ext4")
if sys.platform == "linux":
    from pulsara_agent import desktop_guest_ops as module


@pytest.fixture
def layout(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix=".pg-guest-test-", dir=Path.home())).resolve()
    monkeypatch.setenv("PULSARA_HOME", str(home))
    binary = home / "bundle/postgres/bin"
    binary.mkdir(parents=True)
    # Unit fixtures do not depend on the test runner's storage. Real ext4 is
    # checked by the separate installed-PG integration, without this substitution.
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"filesystems": [{"fstype": "ext4", "fsroot": "/"}]})))
    try:
        yield home, binary
    finally:
        shutil.rmtree(home)


def stopped_data(home):
    data = home / "desktop/postgres"
    data.parent.mkdir(mode=0o700, exist_ok=True)
    data.mkdir(mode=0o700)
    (data / "PG_VERSION").write_text("17\n")
    (data / "PG_VERSION").chmod(0o600)
    (home / "desktop/socket").mkdir(mode=0o700)
    return data


def test_start_lifetime_lock_does_not_block_kernel_but_blocks_new_start(layout):
    home, binary = layout
    first = module.PostgresFacts(binary, "run:pg_ctl:start")
    first.acquire()
    inherited = os.dup(first.pg_fd)
    first.close()
    try:
        reader = module.PostgresFacts(binary, "readVersion")
        reader.acquire()
        reader.close()
        competitor = module.PostgresFacts(binary, "run:pg_ctl:start")
        try:
            with pytest.raises(module.OperationError) as failure:
                competitor.acquire()
            assert failure.value.code == "PG_LIFETIME_BUSY"
            assert failure.value.execution == "not-submitted"
        finally:
            competitor.close()
    finally:
        os.close(inherited)


def test_lock_inode_must_be_private_regular_and_not_linked(layout):
    home, binary = layout
    (home / "desktop").mkdir(mode=0o700)
    lock = home / "desktop/kernel.lock"
    lock.write_text("")
    lock.chmod(0o600)
    os.link(lock, home / "other-link")
    facts = module.PostgresFacts(binary, "readVersion")
    try:
        with pytest.raises(ValueError, match="single-link"):
            facts.acquire()
    finally:
        facts.close()


def test_remove_rechecks_current_pg_and_preserves_outside_inode(layout):
    home, binary = layout
    data = stopped_data(home)
    (data / "owned").write_text("data")
    (home / "outside").write_text("outside")
    pgctl = binary / "pg_ctl"
    pgctl.write_text("#!/bin/sh\nexit 3\n")
    pgctl.chmod(0o755)
    facts = module.PostgresFacts(binary, "removeDataDirectory")
    facts.acquire()
    inode = os.fstat(facts.pg_fd).st_ino
    try:
        facts.perform({"operation": "removeDataDirectory"})
        assert not data.exists()
        assert (home / "outside").read_text() == "outside"
        assert facts.socket.is_dir()
        assert (home / "desktop/pg-lifetime.lock").stat().st_ino == inode
    finally:
        facts.close()
    data = stopped_data_without_socket(home)
    pgctl.write_text("#!/bin/sh\nexit 0\n")
    facts = module.PostgresFacts(binary, "removeDataDirectory")
    facts.acquire()
    try:
        with pytest.raises(module.OperationError, match="fresh stopped"):
            facts.perform({"operation": "removeDataDirectory"})
        assert data.is_dir()
    finally:
        facts.close()


def stopped_data_without_socket(home):
    data = home / "desktop/postgres"
    data.mkdir(mode=0o700)
    (data / "PG_VERSION").write_text("17\n")
    (data / "PG_VERSION").chmod(0o600)
    return data


def test_commands_reject_arbitrary_program_arguments_and_protect_wrong_instance(layout):
    home, binary = layout
    data = stopped_data(home)
    facts = module.PostgresFacts(binary, "run:pg_ctl:start")
    with pytest.raises(ValueError, match="does not match"):
        facts.validate_arguments("pg_ctl", ["start", "-D", "/somewhere/else"])
    (data / "postmaster.pid").write_text("123\n/wrong\n0\n5432\n/wrong/socket\n")
    (data / "postmaster.pid").chmod(0o600)
    with pytest.raises(ValueError, match="differs"):
        facts.verify_instance()


def test_local_command_outcomes_and_inherited_descriptors(layout):
    home, binary = layout
    target = binary / "pg_ctl"
    target.symlink_to(sys.executable)
    facts = module.PostgresFacts(binary, "run:pg_ctl:status")
    facts.acquire()
    try:
        result = facts.command("pg_ctl", ["-c", f"import os;os.fstat({facts.kernel_fd});print('out');raise SystemExit(3)"], (facts.kernel_fd,))
        assert result == {"code": 3, "stdout": "out\n", "stderr": ""}
        with pytest.raises(module.OperationError) as failure:
            facts.command("initdb", [], (facts.kernel_fd,))
        assert failure.value.execution == "not-submitted" and failure.value.code == "ENOENT"
        with pytest.raises(module.OperationError) as failure:
            facts.command("pg_ctl", ["-c", "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"], (facts.kernel_fd,))
        assert failure.value.execution == "unknown"
        with pytest.raises(module.OperationError) as failure:
            facts.command("pg_ctl", ["-c", "import sys;sys.stdout.write('x'*(4*1024*1024+1))"], (facts.kernel_fd,))
        assert failure.value.execution == "unknown" and failure.value.code == "ERR_CHILD_PROCESS_STDIO_MAXBUFFER"
    finally:
        facts.close()


def test_maintenance_only_observes_current_stopped_data_and_keeps_lock_inode(layout):
    home, binary = layout
    data = stopped_data(home)
    sentinel = data / "sentinel"
    sentinel.write_text("keep")
    pgctl = binary / "pg_ctl"
    pgctl.write_text("#!/bin/sh\nexit 3\n")
    pgctl.chmod(0o755)
    facts = module.PostgresFacts(binary, "maintenanceCheck")
    facts.acquire()
    inode = os.fstat(facts.pg_fd).st_ino
    try:
        assert facts.perform({"operation": "maintenanceCheck"}) is None
        assert sentinel.read_text() == "keep" and facts.mutating is False
    finally:
        facts.close()
    assert (home / "desktop/pg-lifetime.lock").stat().st_ino == inode
    pgctl.write_text("#!/bin/sh\nexit 0\n")
    facts = module.PostgresFacts(binary, "maintenanceCheck")
    facts.acquire()
    try:
        with pytest.raises(module.OperationError) as failure:
            facts.perform({"operation": "maintenanceCheck"})
        assert failure.value.execution == "unknown" and sentinel.read_text() == "keep"
    finally:
        facts.close()


def helper_process(binary, method, before_main=""):
    return subprocess.Popen(
        [sys.executable, "-c", "import pulsara_agent.desktop_guest_ops as m\nm.require_native_ext4=lambda *a,**k:None\n" + before_main + "\nm.main()", "--postgres-bin", str(binary), "--method", method],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.mark.parametrize("stage", ["entry", "acquire"])
def test_early_operation_is_rejected_even_when_lock_acquisition_is_delayed(layout, stage):
    home, binary = layout
    gate = "import signal,sys;signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGUSR1});print('gate',file=sys.stderr,flush=True);signal.sigwait({signal.SIGUSR1});signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGUSR1})"
    before = gate if stage == "entry" else "original=m.PostgresFacts.acquire\ndef delayed(self):\n " + gate + "\n original(self)\nm.PostgresFacts.acquire=delayed"
    child = helper_process(binary, "prepareLayout", before)
    try:
        assert child.stderr.readline() == b"gate\n"
        child.stdin.write(b'{"operation":"prepareLayout"}\n')
        child.stdin.close()
        child.stdin = None
        child.send_signal(signal.SIGUSR1)
        stdout, stderr = child.communicate()
        assert child.returncode == 0 and stderr == b""
        reply = json.loads(stdout)
        assert reply["type"] == "result" and reply["ok"] is False
        assert reply["error"]["execution"] == "not-submitted"
        assert not (home / "desktop/socket").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.parametrize("payload", [b'{"operation":"prepareLayout"}\n' * 2, b'{"operation":"prepareLayout"}', b'{invalid}\n'])
def test_only_one_complete_operation_and_eof_can_mutate(layout, payload):
    home, binary = layout
    child = helper_process(binary, "prepareLayout")
    assert json.loads(child.stdout.readline()) == {"type": "ready", "method": "prepareLayout"}
    stdout, stderr = child.communicate(payload)
    assert child.returncode == 0 and stderr == b""
    assert json.loads(stdout)["ok"] is False
    assert not (home / "desktop/socket").exists()


def test_complete_operation_waits_for_input_eof_before_execution(layout):
    home, binary = layout
    child = helper_process(binary, "prepareLayout")
    assert json.loads(child.stdout.readline())["type"] == "ready"
    child.stdin.write(b'{"operation":"prepareLayout"}\n')
    child.stdin.flush()
    assert not (home / "desktop/socket").exists()
    child.stdin.close()
    child.stdin = None
    stdout, stderr = child.communicate()
    assert child.returncode == 0 and stderr == b""
    assert json.loads(stdout) == {"type": "result", "ok": True, "value": None}
    assert (home / "desktop/socket").is_dir()
