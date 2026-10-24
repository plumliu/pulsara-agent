"""Read the same live kernel/PG with pidfd validation and a real private SQL query."""

import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys

home = Path(sys.argv[1])
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-kernel6-")
assert home.resolve() == home and home.stat().st_uid == os.getuid()
record = [json.loads(line) for line in Path(sys.argv[2]).read_text().splitlines()]
kernel = next(row for row in record if row["event"] == "kernel-process-started")
pg_pid = int((home / "desktop/postgres/postmaster.pid").read_text().splitlines()[0])


def identity(pid, expected=None):
    descriptor = os.pidfd_open(pid)
    try:
        current = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        assert expected is None or current == expected
        assert not select.select([descriptor], [], [], 0)[0]
        return dict(pid=pid, starttime=current, alive=True)
    finally:
        os.close(descriptor)


def lock(name):
    descriptor = os.open(home / "desktop" / name, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        return dict(acquired=acquired, inode=os.fstat(descriptor).st_ino)
    finally:
        os.close(descriptor)


psql = Path(
    "/home/plumwsl/workspaces/pulsara_agent/desktop/resources/postgres/bin/psql"
)
query = subprocess.run(
    [
        str(psql),
        "-h",
        str(home / "desktop/socket"),
        "-U",
        "pulsara_admin",
        "-d",
        "pulsara",
        "-Atc",
        "select pg_postmaster_start_time(), current_database();",
    ],
    capture_output=True,
    text=True,
    check=True,
)
print(
    json.dumps(
        dict(
            kernel=identity(kernel["pid"], kernel["starttime"]),
            postgres=identity(pg_pid),
            kernelLock=lock("kernel.lock"),
            pgLock=lock("pg-lifetime.lock"),
            pidNamespace=os.readlink("/proc/self/ns/pid"),
            query=dict(exit=query.returncode, stdout=query.stdout, stderr=query.stderr),
        )
    )
)
