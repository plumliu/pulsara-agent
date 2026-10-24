"""Finite initdb and real bootstrap postgres retain kernel lock after helper loss."""

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


for stage in ("initdb-exec", "initdb-bootstrap"):
    home = None
    helper = None
    case = {"stage": stage}
    cases.append(case)
    try:
        home = control("setup-empty")["home"]
        case["home"] = home
        command = BASE + [PYTHON, LINUX + "/finite.py", REPO, home, stage]
        helper = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = json.loads(helper.stdout.readline())
        record("initdb-ready", command=command, frame=ready)
        helper.stdin.write('{"operation":"initdb"}\n')
        helper.stdin.flush()
        case["atGate"] = control("observe", home)
        control("signal", home, ready["pid"], "SIGKILL")
        case["afterDeathKernel"] = control("lock", home)
        case["afterDeathLifetime"] = control("lifetime", home)
        assert not case["afterDeathLifetime"]["acquired"]
        case["afterDeathState"] = control("state", home)
        assert not case["afterDeathKernel"]["acquired"]
        control("resume", home)
        helper.stdin.close()
        stdout = helper.stdout.read()
        stderr = helper.stderr.read()
        helper.wait(timeout=15)
        record("initdb-finished", exit=helper.returncode, stdout=stdout, stderr=stderr)
        case["afterSettleKernel"] = control("lock", home)
        assert case["afterSettleKernel"]["acquired"]
        case["cleanup"] = control("cleanup-partial", home)
        record("case", case=case)
    except BaseException:
        failures.append(traceback.format_exc())
        record("failure", traceback=failures[-1])
        if home:
            try:
                control("resume", home)
                if helper:
                    helper.wait(timeout=10)
                control("cleanup-partial", home)
            except BaseException:
                record("cleanup-failure", traceback=traceback.format_exc())
        break

(OUT / "initdb-results.json").write_text(
    json.dumps({"cases": cases, "failures": failures, "records": records}, indent=2),
    encoding="utf-8",
)
raise SystemExit(bool(failures))
