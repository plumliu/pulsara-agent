"""Private experiment observation, exact pidfd signalling and final cleanup."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("config", type=Path)
parser.add_argument("action")
parser.add_argument("value", nargs="?")
args = parser.parse_args()
config = json.loads(args.config.read_text())
home = Path(config["home"])
installed = Path(config["installed"])
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".pbfpg-")
assert home.stat().st_uid == os.getuid() == 1000

def actor(pid):
    path = Path("/proc") / str(pid)
    return {"pid": pid, "uid": path.stat().st_uid,
            "start_ticks": path.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]}

def signalled(info, number):
    fd = os.pidfd_open(info["pid"])
    try:
        assert actor(info["pid"]) == {key: info[key] for key in ("pid", "uid", "start_ticks")}
        command = Path(f"/proc/{info['pid']}/cmdline").read_bytes()
        assert os.fsencode(str(home)) in command or os.fsencode(str(args.config)) in command
        signal.pidfd_send_signal(fd, number)
    finally:
        os.close(fd)

def read_gate(name):
    path = home / name
    while not path.exists():
        time.sleep(0.02)
    if name == "gate-native":
        stage, pid, parent = path.read_text().split()
        result = actor(int(pid))
        result.update(stage=stage, parent=int(parent))
        return result
    return json.loads(path.read_text())

def state():
    result = {"locks": {}}
    for name in ("kernel.lock", "pg-lifetime.lock"):
        path = home / "desktop" / name
        if not path.exists():
            result["locks"][name] = {"exists": False}
            continue
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = False
            except BlockingIOError:
                busy = True
            result["locks"][name] = {"exists": True, "busy": busy, "inode": info.st_ino}
        finally:
            os.close(fd)
    data = home / "desktop/postgres"
    result.update(data_exists=data.exists(), version=(data / "PG_VERSION").read_text().strip() if (data / "PG_VERSION").exists() else None,
                  outside=(home / "outside").read_text(), sentinel=(data / "sentinel").read_text() if (data / "sentinel").exists() else None)
    command = subprocess.run([str(installed / "postgres/bin/pg_ctl"), "status", "-D", str(data)], capture_output=True, text=True)
    result["status"] = {"exit": command.returncode, "stdout": command.stdout, "stderr": command.stderr}
    return result

if args.action == "state":
    result = state()
elif args.action.startswith("wait-") and args.action != "wait-query" and args.action != "wait-helper-exit":
    result = read_gate(args.action[5:])
elif args.action in {"resume", "kill-helper"}:
    info = json.loads(args.value)
    signalled(info, signal.SIGCONT if args.action == "resume" else signal.SIGKILL)
    result = {"signalled": info, "signal": args.action}
elif args.action == "wait-helper-exit":
    info = json.loads(args.value)
    try:
        fd = os.pidfd_open(info["pid"])
    except ProcessLookupError:
        result = {"exited": True}
    else:
        try:
            assert actor(info["pid"])["start_ticks"] == info["start_ticks"]
            select.select([fd], [], [])
            result = {"exited": True}
        finally:
            os.close(fd)
elif args.action == "wait-query":
    import psycopg
    while True:
        try:
            with psycopg.connect(host=str(home / "desktop/socket"), user="pulsara_admin", dbname="postgres", connect_timeout=1) as connection:
                row = connection.execute("SELECT system_identifier::text,current_setting('data_directory'),current_setting('unix_socket_directories'),current_setting('listen_addresses') FROM pg_control_system()").fetchone()
            assert row[1:] == (str(home / "desktop/postgres"), str(home / "desktop/socket"), "")
            result = {"identity": row}
            break
        except psycopg.OperationalError:
            time.sleep(0.02)
elif args.action == "sentinel":
    (home / "desktop/postgres/sentinel").write_text("data-sentinel")
    result = state()
elif args.action == "helper":
    result = json.loads((home / f"helper-{args.value}.json").read_text())
elif args.action == "children":
    result = [json.loads(line) for line in (home / f"children-{args.value}.jsonl").read_text().splitlines()]
else:
    raise ValueError(args.action)
print(json.dumps(result))
