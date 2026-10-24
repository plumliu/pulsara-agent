"""Populate only this fresh registration with bundled runtime resources."""

import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
config = json.loads((BASE / "distro-state.json").read_text(encoding="utf-8"))
subprocess.run([sys.executable, str(BASE / "distro_manage.py"), "guard"], check=True)
name = config["name"]
repo = "/home/plumwsl/workspaces/pulsara_agent"
wsl = "C:/Windows/System32/wsl.exe"


def run(user, *args):
    result = subprocess.run(
        [wsl, "-d", name, "-u", user, "--exec", *args], capture_output=True
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return dict(
        exit=result.returncode,
        stdout=result.stdout.decode(errors="replace"),
        stderr=result.stderr.decode(errors="replace"),
    )


created = run(
    "root",
    "/bin/sh",
    "-c",
    "useradd --create-home --shell /bin/bash plumwsl && chmod 700 /home/plumwsl",
)
layout = run(
    "plumwsl",
    "mkdir",
    "-p",
    repo + "/desktop",
    repo + "/output/windows-g0/interop_tasks_r3/round7/distro-pg",
)
source = subprocess.Popen(
    [
        wsl,
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--exec",
        "tar",
        "-c",
        "-C",
        repo,
        "desktop/resources/python",
        "desktop/resources/postgres",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
target = subprocess.Popen(
    [
        wsl,
        "-d",
        name,
        "-u",
        "plumwsl",
        "--exec",
        "tar",
        "-x",
        "--no-same-owner",
        "-C",
        repo,
    ],
    stdin=source.stdout,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
source.stdout.close()
source.stdout = None
target_out, target_err = target.communicate(timeout=180)
source_out, source_err = source.communicate(timeout=15)
assert source.returncode == target.returncode == 0, (source_err, target_err)
linux_base = (
    "/mnt/c/python_workspace/pulsara-agent/output/windows-g0/interop_tasks_r3/round7"
)
helpers = run(
    "plumwsl",
    "cp",
    linux_base + "/distro-pg/helper.py",
    linux_base + "/distro-pg/control.py",
    repo + "/output/windows-g0/interop_tasks_r3/round7/distro-pg/",
)
facts = run(
    "plumwsl",
    "/bin/sh",
    "-c",
    "id; cat /etc/os-release; readlink /proc/self/ns/pid; cat /proc/sys/kernel/random/boot_id; df -B1 /home/plumwsl; du -sb "
    + repo
    + "/desktop/resources/python "
    + repo
    + "/desktop/resources/postgres; ldd "
    + repo
    + "/desktop/resources/python/bin/python3; ldd "
    + repo
    + "/desktop/resources/postgres/bin/postgres; "
    + repo
    + "/desktop/resources/python/bin/python3 -c 'import pulsara_agent.desktop_runtime,psycopg; print(pulsara_agent.desktop_runtime.__file__)'",
)
result = dict(
    name=name,
    user=created,
    layout=layout,
    resourceTransfer=dict(
        sourceExit=source.returncode,
        targetExit=target.returncode,
        sourceStderr=source_err.decode(errors="replace"),
        targetStderr=target_err.decode(errors="replace"),
    ),
    helpers=helpers,
    facts=facts,
)
(BASE / "distro-prepare.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result))
