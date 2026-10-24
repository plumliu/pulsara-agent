"""Read-only observation and exact private-scope research cleanup."""

import fcntl

import json

import os

from pathlib import Path

import shutil

import signal


import subprocess

import sys

import tempfile

import time


REPO = Path("/home/plumwsl/workspaces/pulsara_agent")

BIN = REPO / "desktop/resources/postgres/bin"

action = sys.argv[1]

if action == "setup":
    home = Path(
        tempfile.mkdtemp(prefix=".g0p5-", suffix="-中文 空", dir="/home/plumwsl")
    )

    (home / "outside-sentinel").write_text("outside-sentinel")

    print(
        json.dumps(
            {"home": str(home), "uid": os.getuid(), "device": home.stat().st_dev}
        )
    )

    raise SystemExit()

home = Path(sys.argv[2])

assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-")

assert home.resolve() == home and home.stat().st_uid == os.getuid()

data = home / "desktop/postgres"

socket = home / "desktop/socket"


def command(binary, arguments):

    result = subprocess.run(
        [str(BIN / binary), *arguments], text=True, capture_output=True
    )

    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def process(pid):

    directory = Path(f"/proc/{pid}")

    try:
        return {
            "pid": pid,
            "start_ticks": (directory / "stat").read_text().split()[21],
            "cmdline": (directory / "cmdline")
            .read_bytes()
            .decode(errors="replace")
            .replace("\0", " "),
            "status": (directory / "status").read_text(),
            "exe": os.readlink(directory / "exe"),
            "fds": {fd.name: os.readlink(fd) for fd in (directory / "fd").iterdir()},
        }

    except (FileNotFoundError, ProcessLookupError):
        return {"pid": pid, "exists": False}


def send(pid, name, ticks=None):

    descriptor = os.pidfd_open(pid)

    try:
        facts = process(pid)

        if ticks is not None:
            assert facts["start_ticks"] == ticks, facts

        environment = Path(f"/proc/{pid}/environ").read_bytes()

        assert (
            str(home) in facts.get("cmdline", "")
            or ("G0_PG5_HOME=" + str(home)).encode() + b"\0" in environment
        )

        signal.pidfd_send_signal(descriptor, getattr(signal, name))

        return facts

    finally:
        os.close(descriptor)


def matching():

    matches = []

    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                cmdline = (entry / "cmdline").read_bytes()

                environment = (entry / "environ").read_bytes()

                if (
                    str(data).encode() in cmdline
                    or ("G0_PG5_HOME=" + str(home)).encode() + b"\0" in environment
                ):
                    matches.append(process(int(entry.name)))

            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass

    return matches


def lock(name):

    path = home / "desktop" / name

    if not path.exists():
        return {"exists": False, "acquired": True}

    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)

    acquired = False

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            acquired = True

        except BlockingIOError:
            pass

        return {"exists": True, "acquired": acquired, "inode": os.fstat(fd).st_ino}

    finally:
        os.close(fd)


def snapshot():

    return {
        "home": str(home),
        "kernel": lock("kernel.lock"),
        "pg": lock("pg-lifetime.lock"),
        "status": command("pg_ctl", ["status", "-D", str(data)]),
        "partial_deletion": json.loads((home / "partial-deletion").read_text())
        if (home / "partial-deletion").exists()
        else None,
        "data_exists": data.exists(),
        "socket_exists": socket.exists(),
        "outside_sentinel": (home / "outside-sentinel").read_text(),
        "data_sentinel": (data / "data-sentinel").read_text()
        if (data / "data-sentinel").exists()
        else None,
        "pidfile": (data / "postmaster.pid").read_text()
        if (data / "postmaster.pid").exists()
        else None,
        "processes": matching(),
    }


if action == "state":
    print(json.dumps(snapshot()))

elif action == "wait-query":
    deadline = time.monotonic() + 15

    while True:
        result = command(
            "psql",
            [
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
            ],
        )

        if result["exit"] == 0 or time.monotonic() >= deadline:
            break

        time.sleep(0.02)

    print(json.dumps(result))

elif action == "queries":
    result = subprocess.run(
        [
            str(REPO / ".venv/bin/python"),
            str(Path(__file__).parent / "queries.py"),
            str(home),
            sys.argv[3],
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)

    print(
        json.dumps(
            {
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    )

elif action == "version":
    assert sys.argv[3] in ("16", "17")

    (data / "PG_VERSION").write_text(sys.argv[3] + "\n")

    print(json.dumps({"version": sys.argv[3]}))

elif action == "pidfile":
    contents = json.loads(sys.argv[3])

    assert str(data) in contents and str(socket) in contents

    (data / "postmaster.pid").write_text(contents)

    print(json.dumps({"updated": True}))

elif action == "query":
    print(
        json.dumps(
            command(
                "psql",
                [
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
                ],
            )
        )
    )

elif action == "data-sentinel":
    (data / "data-sentinel").write_text("data-sentinel")

    print(json.dumps({"written": True}))

elif action == "waitgate":
    gate_name = sys.argv[3]

    deadline = time.monotonic() + 15

    while not (home / gate_name).exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert (home / gate_name).exists(), "diagnostic gate deadline"

    gate = (home / gate_name).read_text()

    pid = int(gate.split()[1]) if gate_name == "gate" else json.loads(gate)["pid"]

    print(json.dumps({"gate": gate, "process": process(pid), "state": snapshot()}))

elif action == "signal":
    print(json.dumps({"signaled": send(int(sys.argv[3]), sys.argv[5], sys.argv[4])}))

elif action == "resume-pg":
    if (home / "gate").exists():
        pid = int((home / "gate").read_text().split()[1])

        print(json.dumps({"resumed": send(pid, "SIGCONT")}))

    else:
        print(json.dumps({"resumed": None}))

elif action == "cleanup":
    for name in ("helper-gate", "gate"):
        if (home / name).exists():
            text = (home / name).read_text()

            pid = int(text.split()[1]) if name == "gate" else json.loads(text)["pid"]

            try:
                send(pid, "SIGCONT")

            except (ProcessLookupError, FileNotFoundError):
                pass

    deadline = time.monotonic() + 15

    while time.monotonic() < deadline:
        pidfile = data / "postmaster.pid"

        if pidfile.exists():
            lines = pidfile.read_text().splitlines()

            assert Path(lines[1]).resolve() == data

            if len(lines) > 4 and lines[4]:
                assert lines[4] == str(socket)

                result = command(
                    "pg_ctl", ["stop", "-w", "-D", str(data), "-m", "fast"]
                )

                if result["exit"] == 0:
                    break

        if (
            lock("kernel.lock")["acquired"]
            and lock("pg-lifetime.lock")["acquired"]
            and not matching()
        ):
            break

        time.sleep(0.05)

    state = snapshot()

    assert (
        state["kernel"]["acquired"]
        and state["pg"]["acquired"]
        and not state["processes"]
    ), state

    assert state["status"]["exit"] in (3, 4), state

    assert home.resolve() == home and home.parent == Path("/home/plumwsl")

    shutil.rmtree(home)

    print(json.dumps({"home_removed": not home.exists(), "before": state}))

else:
    raise AssertionError(action)
