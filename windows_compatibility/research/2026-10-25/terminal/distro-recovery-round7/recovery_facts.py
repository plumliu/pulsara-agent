"""Read unmodified crash data and exact canonical rows; no repair/reset operation."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg.conninfo import make_conninfo

action, home_text = sys.argv[1:3]
home = Path(home_text)
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-kernel6-")
assert home.resolve() == home and home.stat().st_uid == os.getuid()
data = home / "desktop/postgres"
binary = Path("/home/plumwsl/workspaces/pulsara_agent/desktop/resources/postgres/bin")


def lock(name):
    fd = os.open(home / "desktop" / name, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        return dict(acquired=acquired, inode=os.fstat(fd).st_ino)
    finally:
        os.close(fd)


pidfile = data / "postmaster.pid"
control = subprocess.run(
    [str(binary / "pg_controldata"), str(data)],
    capture_output=True,
    text=True,
    check=True,
    env={**os.environ, "LC_ALL": "C"},
)
log = home / "desktop/postgres.log"
result = dict(
    pidNamespace=os.readlink("/proc/self/ns/pid"),
    bootId=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    pgDataInode=data.stat().st_ino,
    pgVersion=(data / "PG_VERSION").read_text().strip(),
    kernelLock=lock("kernel.lock"),
    pgLock=lock("pg-lifetime.lock"),
    pidfileHex=pidfile.read_bytes().hex() if pidfile.exists() else None,
    control=dict(stdout=control.stdout, stderr=control.stderr),
    postgresLog=log.read_text(errors="replace") if log.exists() else None,
)
if action == "query":
    session_id = sys.argv[3]
    dsn = make_conninfo(
        host=str(home / "desktop/socket"), dbname="pulsara", user="pulsara_admin"
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        row = connection.execute(
            "SELECT s.id,s.workspace_id,s.memory_domain_id,s.lifecycle,w.workspace_kind,w.workspace_root FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w ON (w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id) WHERE s.id=%s",
            (session_id,),
        ).fetchone()
        assert row is not None
        facts = connection.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()
        settings = {
            name: connection.execute("SHOW " + name).fetchone()[0]
            for name in ("fsync", "synchronous_commit")
        }
        result["canonicalRow"] = list(row)
        result["systemIdentifier"] = facts[0]
        result["durabilitySettings"] = settings
else:
    assert action == "snapshot"
print(json.dumps(result, default=str))
