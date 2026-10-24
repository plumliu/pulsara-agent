"""Finite observations and exact cleanup for named disposable kernel homes."""

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time

REPO = Path("/home/plumwsl/workspaces/pulsara_agent")
BIN = REPO / "desktop/resources/postgres/bin"
action = sys.argv[1]
if action == "setup":
    home = Path(
        tempfile.mkdtemp(
            prefix=".g0p5-kernel6-", suffix="-中文 空", dir="/home/plumwsl"
        )
    )
    (home / "outside-sentinel").write_text("kernel6-disposable")
    print(
        json.dumps(
            dict(
                home=str(home),
                uid=os.getuid(),
                mode=oct(stat.S_IMODE(home.stat().st_mode)),
                device=home.stat().st_dev,
            )
        )
    )
    raise SystemExit()

home = Path(sys.argv[2])
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-kernel6-")
assert (
    home.resolve() == home
    and home.stat().st_uid == os.getuid()
    and stat.S_IMODE(home.stat().st_mode) == 0o700
)


def lock_state():
    path = home / "desktop/kernel.lock"
    if not path.exists():
        return dict(exists=False, acquired=True)
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        return dict(exists=True, acquired=acquired, inode=os.fstat(descriptor).st_ino)
    finally:
        os.close(descriptor)


def kernel_identity(record):
    path = Path(record)
    values = (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )
    return next(
        (row for row in values if row["event"] == "kernel-process-started"), None
    )


def process_fact(identity):
    if identity is None:
        return None
    path = Path("/proc") / str(identity["pid"]) / "stat"
    try:
        descriptor = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return dict(**identity, sameIdentity=False, alive=False)
    try:
        current = (
            path.read_text().rsplit(")", 1)[1].split()[19] if path.exists() else None
        )
        same = current == identity["starttime"]
        return dict(
            identity,
            actualStarttime=current,
            sameIdentity=same,
            alive=same and not bool(select.select([descriptor], [], [], 0)[0]),
        )
    finally:
        os.close(descriptor)


def pg_status():
    completed = subprocess.run(
        [str(BIN / "pg_ctl"), "status", "-D", str(home / "desktop/postgres")],
        text=True,
        capture_output=True,
    )
    return dict(
        exit=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


if action == "prepare":
    (home / "desktop").mkdir(mode=0o700, exist_ok=True)
    print(json.dumps(dict(prepared=True)))
elif action == "log":
    with (home / "desktop/kernel.log").open("ab") as stream:
        stream.write(sys.stdin.buffer.read())
elif action == "state":
    record = sys.argv[3]
    print(
        json.dumps(
            dict(
                home=str(home),
                kernel=process_fact(kernel_identity(record)),
                lock=lock_state(),
                postgres=pg_status(),
                dataExists=(home / "desktop/postgres").exists(),
                closeGateEntered=(home / "close.entered").exists(),
                closeGateReleased=(home / "close.release").exists(),
            )
        )
    )
elif action == "signal":
    identity = kernel_identity(sys.argv[3])
    assert identity is not None
    descriptor = os.pidfd_open(identity["pid"])
    try:
        current = (
            Path(f"/proc/{identity['pid']}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19]
        )
        assert current == identity["starttime"]
        assert ("PULSARA_HOME=" + str(home)).encode() + b"\0" in Path(
            f"/proc/{identity['pid']}/environ"
        ).read_bytes()
        signal.pidfd_send_signal(descriptor, getattr(signal, sys.argv[4]))
        print(json.dumps(dict(signalled=identity, signal=sys.argv[4])))
    finally:
        os.close(descriptor)
elif action == "release-close":
    (home / "close.release").touch()
    print(json.dumps(dict(released=True)))
elif action == "wait-close-gate":
    deadline = time.monotonic() + 20
    while not (home / "close.entered").exists():
        assert time.monotonic() < deadline, "diagnostic close-gate observation deadline"
        time.sleep(0.01)
    print(
        json.dumps(
            dict(
                entered=True,
                lock=lock_state(),
                kernel=process_fact(kernel_identity(sys.argv[3])),
            )
        )
    )
elif action == "observe-loss":
    identity = kernel_identity(sys.argv[3])
    deadline = time.monotonic() + 10
    while True:
        kernel = process_fact(identity)
        gate = (home / "close.entered").exists()
        if not kernel or not kernel["alive"] or gate or time.monotonic() >= deadline:
            classification = (
                "kernel-exited"
                if not kernel or not kernel["alive"]
                else "application-draining"
                if gate
                else "live-without-close-ack"
            )
            print(
                json.dumps(
                    dict(
                        classification=classification,
                        kernel=kernel,
                        lock=lock_state(),
                        postgres=pg_status(),
                        closeGateEntered=gate,
                    )
                )
            )
            break
        time.sleep(0.01)
elif action == "watch":
    identity = kernel_identity(sys.argv[3])
    output = Path(sys.argv[4])
    assert identity is not None
    descriptor = os.pidfd_open(identity["pid"])
    try:
        assert (
            Path(f"/proc/{identity['pid']}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19]
            == identity["starttime"]
        )
        output.write_text(
            json.dumps(
                dict(event="pidfd-watch-ready", time=time.time(), identity=identity)
            )
            + "\n"
        )
        exited = bool(select.select([descriptor], [], [], 45)[0])
        with output.open("a") as stream:
            stream.write(
                json.dumps(
                    dict(
                        event="pidfd-kernel-exit",
                        time=time.time(),
                        signalled=exited,
                        lock=lock_state(),
                    )
                )
                + "\n"
            )
        assert exited, "diagnostic kernel observation deadline"
    finally:
        os.close(descriptor)
elif action == "cleanup":
    record = sys.argv[3]
    (home / "close.release").touch()
    identity = kernel_identity(record)
    before = process_fact(identity)
    forced = False
    if before and before["alive"]:
        descriptor = os.pidfd_open(identity["pid"])
        try:
            assert (
                Path(f"/proc/{identity['pid']}/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19]
                == identity["starttime"]
            )
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], 20)[0]:
                forced = True
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            assert select.select([descriptor], [], [], 5)[0]
        finally:
            os.close(descriptor)
    assert lock_state()["acquired"], (
        "kernel owner still physically holds the private lock"
    )
    log = home / "desktop/kernel.log"
    kernel_log = log.read_text(errors="replace") if log.exists() else ""
    command = [
        str(REPO / ".venv/bin/python"),
        str(REPO / "output/windows-g0/pg_admission_r3/round5/control.py"),
        "cleanup",
        str(home),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    print(
        json.dumps(
            dict(
                before=before,
                forcedExactFixtureTeardown=forced,
                pgCleanup=json.loads(completed.stdout),
                homeExists=home.exists(),
                kernelLog=kernel_log,
            )
        )
    )
else:
    raise ValueError(action)
