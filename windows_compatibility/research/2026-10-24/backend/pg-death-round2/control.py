import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import shutil
from pathlib import Path

repo = Path("/home/plumwsl/workspaces/pulsara_agent")
bin = repo / "desktop/resources/postgres/bin"
action = sys.argv[1]


def command(args):
    r = subprocess.run(
        [str(bin / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    return {"exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr}


def proc(pid):
    p = Path("/proc") / str(pid)
    if not p.exists():
        return {"pid": pid, "exists": False}
    try:
        status = (p / "status").read_text()
        cmd = (p / "cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
        fds = {}
        for fd in (p / "fd").iterdir():
            try:
                fds[fd.name] = os.readlink(fd)
            except FileNotFoundError:
                pass
        return {
            "pid": pid,
            "exists": True,
            "exe": os.readlink(p / "exe"),
            "cmdline": cmd,
            "status": status,
            "fds": fds,
            "stat": (p / "stat").read_text(),
        }
    except (FileNotFoundError, PermissionError) as e:
        return {"pid": pid, "exists": True, "error": str(e)}


if action == "setup":
    h = Path(tempfile.mkdtemp(prefix=".g0d2-", dir="/home/plumwsl"))
    (h / "desktop").mkdir(mode=0o700)
    (h / "desktop/socket").mkdir(mode=0o700)
    r = command(
        [
            "initdb",
            "-D",
            str(h / "desktop/postgres"),
            "-U",
            "pulsara_admin",
            "--auth-local=trust",
            "--auth-host=reject",
            "--encoding=UTF8",
            "--locale=C",
        ]
    )
    assert r["exit"] == 0, r
    print(json.dumps({"home": str(h), "uid": os.getuid(), "initdb": r}))
    sys.exit()
h = Path(sys.argv[2])
assert (
    h.resolve() == h
    and (str(h).startswith("/tmp/g0d2-") or str(h).startswith("/home/plumwsl/.g0d2-"))
    and h.stat().st_uid == os.getuid()
)
data = h / "desktop/postgres"
socket = h / "desktop/socket"


def snapshot():
    ids = []
    for name in ("pgctl.pid", "gate"):
        p = h / name
        if p.exists():
            ids.append(
                int(p.read_text().split()[1] if name == "gate" else p.read_text())
            )
    pidfile = data / "postmaster.pid"
    pidtext = pidfile.read_text() if pidfile.exists() else None
    if pidtext:
        ids.append(int(pidtext.splitlines()[0]))
    return {
        "home": str(h),
        "gate": (h / "gate").read_text() if (h / "gate").exists() else None,
        "postmaster_pid": pidtext,
        "pg_log": (h / "desktop/postgres.log").read_text()
        if (h / "desktop/postgres.log").exists()
        else None,
        "data_stat": {
            "uid": data.stat().st_uid,
            "mode": oct(stat.S_IMODE(data.stat().st_mode)),
            "device": data.stat().st_dev,
            "inode": data.stat().st_ino,
        },
        "processes": [proc(i) for i in dict.fromkeys(ids)],
    }


if action == "observe":
    deadline = time.monotonic() + 15
    while not (h / "gate").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (h / "gate").exists(), "gate not reached"
    print(json.dumps(snapshot()))
elif action == "state":
    print(json.dumps(snapshot()))
elif action == "status":
    print(json.dumps(command(["pg_ctl", "status", "-D", str(data)])))
elif action == "lock":
    fd = os.open(h / "desktop/kernel.lock", os.O_RDWR)
    acquired = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except BlockingIOError:
        pass
    print(
        json.dumps(
            {
                "acquired": acquired,
                "kernel_lock": str(h / "desktop/kernel.lock"),
                "stat": {"inode": os.fstat(fd).st_ino, "device": os.fstat(fd).st_dev},
            }
        )
    )
    os.close(fd)
elif action == "locked-status":
    fd = os.open(h / "desktop/kernel.lock", os.O_RDWR)
    acquired = False
    result = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        result = command(["pg_ctl", "status", "-D", str(data)])
    except BlockingIOError:
        pass
    print(
        json.dumps(
            {
                "type": "finite-locked-status",
                "pid": os.getpid(),
                "acquired": acquired,
                "status": result,
                "lock_inode": os.fstat(fd).st_ino,
            }
        )
    )
    os.close(fd)
elif action == "query":
    print(
        json.dumps(
            command(
                [
                    "psql",
                    "-h",
                    str(socket),
                    "-p",
                    "5432",
                    "-U",
                    "pulsara_admin",
                    "-d",
                    "postgres",
                    "-Atc",
                    "select current_setting('data_directory'),current_setting('unix_socket_directories'),pg_postmaster_start_time();",
                ]
            )
        )
    )
elif action == "signal":
    pid = int(sys.argv[3])
    sig = getattr(signal, sys.argv[4])
    facts = proc(pid)
    assert facts["exists"] and str(h) in facts.get("cmdline", ""), facts
    os.kill(pid, sig)
    print(json.dumps({"signal": sig, "process": facts}))
elif action == "resume":
    pid = int((h / "gate").read_text().split()[1])
    facts = proc(pid)
    # postgres cmdline includes exact data; pg_ctl includes exact -D.
    if facts.get("cmdline"):
        assert str(data) in facts["cmdline"], facts
        os.kill(pid, signal.SIGCONT)
    print(json.dumps({"resumed": pid, "before": facts}))
elif action == "cleanup":
    # Resume only this exact gate before stopping the exact identified PG.
    if (h / "gate").exists():
        pid = int((h / "gate").read_text().split()[1])
        facts = proc(pid)
        if str(data) in facts.get("cmdline", ""):
            os.kill(pid, signal.SIGCONT)
    pidfile = data / "postmaster.pid"
    stop = None
    if pidfile.exists():
        lines = pidfile.read_text().splitlines()
        assert lines[1] == str(data), lines
        if len(lines) > 4:
            assert lines[4] == str(socket), lines
        stop = command(["pg_ctl", "stop", "-w", "-D", str(data), "-m", "fast"])
        assert stop["exit"] == 0, stop
    status = command(["pg_ctl", "status", "-D", str(data)])
    assert status["exit"] == 3, status
    matches = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                c = (p / "cmdline").read_bytes()
                if str(data).encode() in c:
                    matches.append(
                        {"pid": int(p.name), "cmdline": c.decode(errors="replace")}
                    )
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
    assert not matches, matches
    assert (str(h).startswith("/tmp/g0d2-") and h.parent == Path("/tmp")) or (
        str(h).startswith("/home/plumwsl/.g0d2-") and h.parent == Path("/home/plumwsl")
    )
    shutil.rmtree(h)
    print(
        json.dumps(
            {
                "home": str(h),
                "stop": stop,
                "status": status,
                "matches": matches,
                "homeRemoved": not h.exists(),
            }
        )
    )
