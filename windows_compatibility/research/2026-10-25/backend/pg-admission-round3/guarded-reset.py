"""Research-only reset: fresh facts and both OS locks remain held through removal."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

repo = Path(sys.argv[1])
home = Path(sys.argv[2])
assert home.resolve() == home and home.parent == Path("/home/plumwsl")
assert home.name.startswith(".g0p3-")
os.environ["PULSARA_HOME"] = str(home)
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402 -- preserve executed research snapshot

kernel_fd = acquire_kernel_lock()
print(json.dumps({"type": "ready", "pid": os.getpid()}), flush=True)
frame = sys.stdin.readline()
if not frame:
    sys.exit(0)
assert json.loads(frame) == {"operation": "reset"}
data = home / "desktop/postgres"
socket = home / "desktop/socket"
assert data.resolve() == data and data.parent.resolve() == data.parent
metadata = data.stat()
assert metadata.st_uid == os.getuid() and stat.S_ISDIR(metadata.st_mode)
assert (data / "PG_VERSION").read_text().strip().isdigit()
pg_ctl = repo / "desktop/resources/postgres/bin/pg_ctl"


def command(arguments):
    result = subprocess.run(
        [str(pg_ctl), *arguments],
        text=True,
        capture_output=True,
        pass_fds=(kernel_fd,),
        env={**os.environ, "LC_ALL": "C"},
    )
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


status = command(["status", "-D", str(data)])
stop = None
if status["exit"] == 0:
    lines = (data / "postmaster.pid").read_text().splitlines()
    assert lines[1] == str(data) and lines[4] == str(socket), lines
    stop = command(["stop", "-w", "-D", str(data), "-m", "fast"])
    assert stop["exit"] == 0, stop
else:
    assert status["exit"] == 3, status

lock = home / "desktop/pg-lifetime.lock"
pg_fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
metadata = os.fstat(pg_fd)
assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
assert stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
try:
    fcntl.flock(pg_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(
        json.dumps(
            {
                "type": "busy-inflight",
                "status": status,
                "stop": stop,
                "data_preserved": data.exists(),
                "deleted": False,
            }
        ),
        flush=True,
    )
    sys.exit(74)

current_status = command(["status", "-D", str(data)])
assert current_status["exit"] == 3, current_status
sentinel_before = (home / "outside-sentinel").read_text()
lock_inode = os.fstat(pg_fd).st_ino
shutil.rmtree(data)
assert os.fstat(pg_fd).st_ino == lock.stat().st_ino == lock_inode
assert socket.is_dir() and (home / "outside-sentinel").read_text() == sentinel_before
print(
    json.dumps(
        {
            "type": "reset",
            "deleted": not data.exists(),
            "kernel_held": True,
            "lifetime_held": True,
            "lock_inode": lock_inode,
            "same_lock_inode": True,
            "socket_preserved": True,
            "sentinel_preserved": True,
            "status": status,
            "current_status": current_status,
            "stop": stop,
        }
    ),
    flush=True,
)
os.close(pg_fd)
os.close(kernel_fd)
