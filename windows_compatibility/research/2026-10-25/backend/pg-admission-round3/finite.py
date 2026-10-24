"""Finite stop probe: inherit only the existing kernel lock to real pg_ctl."""

import json
import fcntl
import os
from pathlib import Path
import subprocess
import stat
import sys

repo, home, stage = map(str, sys.argv[1:])
home = Path(home)
assert home.resolve() == home and home.parent == Path("/home/plumwsl")
assert home.name.startswith(".g0p3-") and home.stat().st_uid == os.getuid()
os.environ["PULSARA_HOME"] = str(home)
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402 -- preserve executed research snapshot

fd = acquire_kernel_lock()
print(json.dumps({"type": "ready", "pid": os.getpid(), "fd": fd}), flush=True)
line = sys.stdin.readline()
if not line:
    sys.exit(0)
data = home / "desktop/postgres"
socket = home / "desktop/socket"
if stage.startswith("initdb-"):
    assert json.loads(line) == {"operation": "initdb"}
    assert not data.exists()
    pg_fd = os.open(
        home / "desktop/pg-lifetime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    metadata = os.fstat(pg_fd)
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
    fcntl.flock(pg_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    inherited_fds = (fd, pg_fd)
    command = [
        str(Path(repo) / "desktop/resources/postgres/bin/initdb"),
        "-D",
        str(data),
        "-U",
        "pulsara_admin",
        "--auth-local=trust",
        "--auth-host=reject",
        "--encoding=UTF8",
        "--locale=C",
    ]
else:
    assert json.loads(line) == {"operation": "stop"}
    lines = (data / "postmaster.pid").read_text().splitlines()
    assert lines[1] == str(data) and lines[4] == str(socket), lines
    command = [
        str(Path(repo) / "desktop/resources/postgres/bin/pg_ctl"),
        "stop",
        "-w",
        "-D",
        str(data),
        "-m",
        "fast",
    ]
    inherited_fds = (fd,)
hook = str(Path(__file__).parent / "fault.so")
child = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    close_fds=True,
    pass_fds=inherited_fds,
    env={
        **os.environ,
        "LC_ALL": "C",
        "LD_PRELOAD": hook,
        "G0_HOOK_HOME": str(home),
        "G0_HOOK_STAGE": stage,
    },
)
(home / "pgctl.pid").write_text(str(child.pid))
stdout, stderr = child.communicate()
print(
    json.dumps({"exit": child.returncode, "stdout": stdout, "stderr": stderr}),
    flush=True,
)
os.close(fd)
