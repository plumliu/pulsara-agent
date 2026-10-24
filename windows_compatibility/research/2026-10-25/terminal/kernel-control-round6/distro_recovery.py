"""New-namespace observation; never treat it as a pre-loss pidfd continuation."""

import fcntl
import json
import os
from pathlib import Path
import sys

home = Path(sys.argv[2])
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-kernel6-")
assert home.resolve() == home and home.stat().st_uid == os.getuid()
data = home / "desktop/postgres"


def lock(name):
    path = home / "desktop" / name
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        return dict(acquired=acquired, inode=os.fstat(descriptor).st_ino)
    finally:
        os.close(descriptor)


owned = []
for path in Path("/proc").iterdir():
    if path.name.isdigit():
        try:
            if str(data).encode() in (path / "cmdline").read_bytes():
                owned.append(
                    dict(
                        pid=int(path.name),
                        command=(path / "cmdline")
                        .read_bytes()
                        .decode(errors="replace"),
                    )
                )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
pidfile = data / "postmaster.pid"
result = dict(
    pidNamespace=os.readlink("/proc/self/ns/pid"),
    bootId=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    kernelLock=lock("kernel.lock"),
    pgLock=lock("pg-lifetime.lock"),
    ownedPgProcesses=owned,
    pgVersion=(data / "PG_VERSION").read_text().strip(),
    pgDataInode=data.stat().st_ino,
    postmasterFile=pidfile.read_text() if pidfile.exists() else None,
)
if sys.argv[1] == "remove-stale-pid-for-cleanup":
    assert (
        result["kernelLock"]["acquired"] and result["pgLock"]["acquired"] and not owned
    )
    if pidfile.exists():
        assert Path(pidfile.read_text().splitlines()[1]) == data
        pidfile.unlink()
    result["removedOnlyOwnStalePidFile"] = True
else:
    assert sys.argv[1] == "snapshot"
print(json.dumps(result))
