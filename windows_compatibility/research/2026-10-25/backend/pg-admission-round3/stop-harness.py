"""Windows orchestrates actual finite stop calls and observes exact Linux locks."""

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
cases = []
failures = []


def record(kind, **facts):
    item = {"kind": kind, "time_ns": time.time_ns(), **facts}
    records.append(item)
    print(json.dumps(item), flush=True)


def control(action, home=None, *args):
    command = BASE + [PYTHON, LINUX + "/control.py", action]
    command += ([home] if home else []) + list(map(str, args))
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


for stage in ("pgctl-stop-exec", "pgctl-after-ready"):
    home = None
    stopper = None
    case = {"stage": stage}
    cases.append(case)
    try:
        home = control("setup")["home"]
        case["home"] = home
        start_command = BASE + [
            PYTHON,
            LINUX + "/helper.py",
            REPO,
            home,
            "plain",
            "separate",
        ]
        start = subprocess.run(
            start_command,
            input='{"operation":"start"}\n',
            capture_output=True,
            text=True,
            timeout=30,
        )
        record(
            "start",
            command=start_command,
            exit=start.returncode,
            stdout=start.stdout,
            stderr=start.stderr,
        )
        assert start.returncode == 0
        assert json.loads(start.stdout.splitlines()[1])["exit"] == 0
        command = BASE + [PYTHON, LINUX + "/finite.py", REPO, home, stage]
        stopper = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = json.loads(stopper.stdout.readline())
        record("stop-ready", command=command, frame=ready)
        stopper.stdin.write('{"operation":"stop"}\n')
        stopper.stdin.flush()
        case["atGate"] = control("observe", home)
        case["beforeDeathKernel"] = control("lock", home)
        assert not case["beforeDeathKernel"]["acquired"]
        control("signal", home, ready["pid"], "SIGKILL")
        case["afterDeathKernel"] = control("lock", home)
        case["afterDeathLifetime"] = control("lifetime", home)
        case["afterDeathStatus"] = control("status", home)
        case["afterDeathState"] = control("state", home)
        assert not case["afterDeathKernel"]["acquired"]
        if stage == "pgctl-after-ready":
            assert case["afterDeathStatus"]["exit"] == 3
            assert case["afterDeathLifetime"]["acquired"]
        control("resume", home)
        stopper.stdin.close()
        stdout = stopper.stdout.read()
        stderr = stopper.stderr.read()
        stopper.wait(timeout=15)
        record("stop-finished", exit=stopper.returncode, stdout=stdout, stderr=stderr)
        case["afterSettleKernel"] = control("lock", home)
        assert case["afterSettleKernel"]["acquired"]
        case["afterSettleStatus"] = control("status", home)
        case["cleanup"] = control("cleanup", home)
        record("case", case=case)
    except BaseException:
        failures.append(traceback.format_exc())
        record("failure", traceback=failures[-1])
        if home:
            try:
                control("resume", home)
                if stopper:
                    stopper.wait(timeout=10)
                control("cleanup", home)
            except BaseException:
                record("cleanup-failure", traceback=traceback.format_exc())
        break

(OUT / "stop-results.json").write_text(
    json.dumps({"cases": cases, "failures": failures, "records": records}, indent=2),
    encoding="utf-8",
)
raise SystemExit(bool(failures))
