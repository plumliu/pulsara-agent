"""Output-only fault probe; no PG lifecycle policy or saved settings."""

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("repo")
p.add_argument("home")
p.add_argument("stage")
p.add_argument("mode", choices=["cloexec", "inherit"])
a = p.parse_args()
home = Path(a.home)
repo = Path(a.repo)
assert home.resolve() == home and (
    str(home).startswith("/tmp/g0d2-") or str(home).startswith("/home/plumwsl/.g0d2-")
)
assert home.stat().st_uid == os.getuid() and stat.S_IMODE(home.stat().st_mode) == 0o700
os.environ["PULSARA_HOME"] = str(home)
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402 -- preserve the executed research snapshot


def emit(x):
    print(json.dumps(x), flush=True)


try:
    fd = acquire_kernel_lock()
except BlockingIOError:
    emit({"type": "busy"})
    sys.exit(73)
emit(
    {
        "type": "ready",
        "pid": os.getpid(),
        "fd": fd,
        "inheritable": os.get_inheritable(fd),
        "mode": a.mode,
    }
)
line = sys.stdin.readline()
if not line:
    sys.exit(0)
assert json.loads(line) == {"operation": "start"}
data = home / "desktop/postgres"
sock = home / "desktop/socket"
assert len(str(sock / ".s.PGSQL.5432").encode()) < 104
for d in (data, sock):
    assert (
        d.resolve() == d
        and d.stat().st_uid == os.getuid()
        and not stat.S_IMODE(d.stat().st_mode) & 0o077
    )
assert (data / "PG_VERSION").read_text().strip() == "17"
hook = Path(__file__).parent / "fault.so"
libraries = "pg_search" if a.stage == "plain" else f"pg_search,{hook}"
options = f"-c listen_addresses='' -k '{sock}' -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries='{libraries}'"
cmd = [
    str(repo / "desktop/resources/postgres/bin/pg_ctl"),
    "start",
    "-w",
    "-D",
    str(data),
    "-l",
    str(home / "desktop/postgres.log"),
    "-o",
    options,
]
env = {
    **os.environ,
    "LC_ALL": "C",
    "LD_PRELOAD": str(hook),
    "G0_HOOK_HOME": str(home),
    "G0_HOOK_STAGE": a.stage,
}
if a.stage == "plain":
    for key in ("LD_PRELOAD", "G0_HOOK_HOME", "G0_HOOK_STAGE"):
        env.pop(key, None)
child = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=env,
    close_fds=True,
    pass_fds=(fd,) if a.mode == "inherit" else (),
)
(home / "pgctl.pid").write_text(str(child.pid))
out, err = child.communicate()
readback = subprocess.run(
    [str(repo / "desktop/resources/postgres/bin/pg_ctl"), "status", "-D", str(data)],
    capture_output=True,
    text=True,
)
emit(
    {
        "type": "result",
        "exit": child.returncode,
        "stdout": out,
        "stderr": err,
        "status": readback.returncode,
        "readback": readback.stdout,
    }
)
os.close(fd)
