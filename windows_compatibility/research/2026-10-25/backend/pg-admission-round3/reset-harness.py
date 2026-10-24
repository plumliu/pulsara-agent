"""One guarded reset negative/positive control, followed by real init/start/query."""

import json
from pathlib import Path
import subprocess
import time
import traceback

OUT = Path(__file__).resolve().parent
REPO = "/home/plumwsl/workspaces/pulsara_agent"
LINUX = REPO + "/output/windows-g0/pg_admission_r3/round3"
PYTHON = REPO + "/.venv/bin/python"
BASE = ["wsl", "-d", "Ubuntu", "-u", "plumwsl", "--cd", REPO, "--exec"]
records = []
facts = {}
failures = []
home = None
starter = None


def record(kind, **values):
    item = {"kind": kind, "time_ns": time.time_ns(), **values}
    records.append(item)
    print(json.dumps(item), flush=True)


def control(action, *args):
    command = BASE + [PYTHON, LINUX + "/control.py", action, *map(str, args)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    record(
        "control",
        command=command,
        exit=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    assert result.returncode == 0, result
    return json.loads(result.stdout)


def finite(script, operation, *arguments):
    command = BASE + [PYTHON, LINUX + "/" + script, REPO, home, *arguments]
    result = subprocess.run(
        command,
        input=json.dumps({"operation": operation}) + "\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    record(
        "finite",
        command=command,
        exit=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    return result, [json.loads(line) for line in result.stdout.splitlines()]


try:
    home = control("setup")["home"]
    facts["home"] = home
    control("sentinels", home)
    command = BASE + [
        PYTHON,
        LINUX + "/helper.py",
        REPO,
        home,
        "postgres-exec",
        "separate",
    ]
    starter = subprocess.Popen(
        command,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = json.loads(starter.stdout.readline())
    record("start-ready", command=command, frame=ready)
    starter.stdin.write('{"operation":"start"}\n')
    starter.stdin.flush()
    facts["gate"] = control("observe", home)
    control("signal", home, ready["pid"], "SIGKILL")
    result, frames = finite("guarded-reset.py", "reset")
    facts["busyReset"] = frames[-1]
    assert result.returncode == 74 and frames[-1]["type"] == "busy-inflight"
    assert frames[-1]["status"]["exit"] == 3 and not frames[-1]["deleted"]
    facts["busySentinels"] = control("sentinel-state", home)
    assert facts["busySentinels"]["data"] == "data-sentinel"
    assert facts["busySentinels"]["outside"] == "outside-sentinel"
    control("resume", home)
    starter.stdin.close()
    stdout = starter.stdout.read()
    stderr = starter.stderr.read()
    starter.wait(timeout=15)
    record("start-finished", exit=starter.returncode, stdout=stdout, stderr=stderr)
    # Finite diagnostic observation deadline, never a product lifetime or replay.
    deadline = time.monotonic() + 15
    while True:
        facts["query"] = control("query", home)
        if facts["query"]["exit"] == 0 or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert facts["query"]["exit"] == 0
    facts["stop"] = control("stop", home)
    assert facts["stop"]["exit"] == 0
    result, frames = finite("guarded-reset.py", "reset")
    facts["reset"] = frames[-1]
    assert result.returncode == 0 and frames[-1]["deleted"]
    facts["absentStatus"] = control("status", home)
    assert facts["absentStatus"]["exit"] == 4
    result, frames = finite("finite.py", "initdb", "initdb-plain")
    assert result.returncode == 0 and frames[-1]["exit"] == 0
    result, frames = finite("helper.py", "start", "plain", "separate")
    assert result.returncode == 0 and frames[-1]["exit"] == 0
    facts["rebuildQuery"] = control("query", home)
    assert facts["rebuildQuery"]["exit"] == 0
    facts["rebuiltSentinels"] = control("sentinel-state", home)
    assert facts["rebuiltSentinels"]["outside"] == "outside-sentinel"
    assert facts["rebuiltSentinels"]["data"] is None
    assert facts["rebuiltSentinels"]["lock_inode"] == facts["reset"]["lock_inode"]
    facts["cleanup"] = control("cleanup", home)
except BaseException:
    failures.append(traceback.format_exc())
    record("failure", traceback=failures[-1])
    if home:
        try:
            control("resume", home)
            if starter:
                starter.wait(timeout=10)
            control("cleanup", home)
        except BaseException:
            record("cleanup-failure", traceback=traceback.format_exc())

(OUT / "reset-results.json").write_text(
    json.dumps({"facts": facts, "failures": failures, "records": records}, indent=2),
    encoding="utf-8",
)
raise SystemExit(bool(failures))
