"""Windows finite-call observer; exact Ubuntu/plumwsl selection throughout."""

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

out = Path(__file__).resolve().parent
repo = "/home/plumwsl/workspaces/pulsara_agent"
linux = repo + "/output/windows-g0/pg_admission_r3/round3"
python = repo + "/.venv/bin/python"
base = ["wsl", "-d", "Ubuntu", "-u", "plumwsl", "--cd", repo, "--exec"]
records = []
active = []


def record(kind, **values):
    item = {"kind": kind, "time_ns": time.time_ns(), **values}
    records.append(item)
    print(json.dumps(item, ensure_ascii=False), flush=True)


def ctl(action, home=None, *args):
    cmd = (
        base
        + [python, linux + "/control.py", action]
        + ([home] if home else [])
        + list(map(str, args))
    )
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
    )
    record("control", command=cmd, exit=r.returncode, stdout=r.stdout, stderr=r.stderr)
    assert r.returncode == 0, (action, r.stdout, r.stderr)
    return json.loads(r.stdout)


def helper(home, stage, mode):
    cmd = base + [python, linux + "/helper.py", repo, home, stage, mode]
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    ready = json.loads(p.stdout.readline())
    record("helper-ready", command=cmd, frame=ready)
    assert ready["type"] == "ready"
    p.stdin.write(json.dumps({"operation": "start"}) + "\n")
    p.stdin.flush()
    active.append((home, p, ready))
    return p, ready


failures = []
cases = []
try:
    # Unpaused control uses the identical output hook library and real production PG arguments.
    runs = [
        ("plain", "separate"),
        ("unpaused", "separate"),
        ("pgctl-before-fork", "separate"),
        ("pgctl-child-after-setsid", "separate"),
        ("postgres-exec", "separate"),
        ("postmaster-before-ready", "separate"),
        ("pgctl-after-ready", "separate"),
    ]
    for stage, mode in runs:
        case = {"stage": stage, "mode": mode}
        cases.append(case)
        record("case-start", **case)
        home = ctl("setup")["home"]
        case["home"] = home
        p, ready = helper(home, stage, mode)
        if stage in ("unpaused", "plain"):
            p.stdin.close()
            stdout = p.stdout.read()
            stderr = p.stderr.read()
            p.wait(timeout=40)
            record(
                "helper-finished",
                home=home,
                exit=p.returncode,
                stdout=stdout,
                stderr=stderr,
            )
            case["result"] = json.loads(stdout)
            assert case["result"]["exit"] == 0 and case["result"]["status"] == 0
            case["afterSettleLock"] = ctl("lock", home)
            assert case["afterSettleLock"]["acquired"]
            case["afterSettleLifetime"] = ctl("lifetime", home)
            assert not case["afterSettleLifetime"]["acquired"]
            case["actualQuery"] = ctl("query", home)
            assert case["actualQuery"]["exit"] == 0
            case["afterSettleState"] = ctl("state", home)
        else:
            case["atGate"] = ctl("observe", home)
            case["beforeDeathLock"] = ctl("lock", home)
            assert not case["beforeDeathLock"]["acquired"]
            case["beforeDeathStatus"] = ctl("status", home)
            ctl("signal", home, ready["pid"], "SIGKILL")
            case["afterDeathLock"] = ctl("lock", home)
            assert case["afterDeathLock"]["acquired"]
            case["afterDeathLifetime"] = ctl("lifetime", home)
            assert not case["afterDeathLifetime"]["acquired"]
            case["afterDeathLockedStatus"] = ctl("locked-status", home)
            case["afterDeathState"] = ctl("state", home)
            case["afterDeathStatus"] = ctl("status", home)
            ctl("resume", home)
            # A finite observation deadline only for this diagnostic call, not product lifetime.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                s = ctl("status", home)
                if s["exit"] == 0:
                    break
                time.sleep(0.05)
            case["afterResumeStatus"] = s
            case["afterResumeState"] = ctl("state", home)
            case["afterResumeLock"] = ctl("lock", home)
            if s["exit"] == 0:
                query = ctl("query", home)
                assert query["exit"] == 0, query
                case["actualQuery"] = query
            try:
                p.stdin.close()
                stdout, stderr = p.communicate(timeout=10)
            except ValueError:
                stdout = p.stdout.read()
                stderr = p.stderr.read()
                p.wait(timeout=10)
            record(
                "helper-finished",
                home=home,
                exit=p.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if case.get("afterResumeStatus", {}).get("exit", 0) == 0:
            case["stop"] = ctl("stop", home)
            assert case["stop"]["exit"] == 0
        case["stoppedStatus"] = ctl("status", home)
        assert case["stoppedStatus"]["exit"] == 3
        case["stoppedLifetime"] = ctl("lifetime", home)
        assert case["stoppedLifetime"]["acquired"]
        case["cleanup"] = ctl("cleanup", home)
        record("case-result", case=case)
except BaseException as e:
    failures.append(traceback.format_exc())
    record("failure", error=str(e), traceback=failures[-1])
finally:
    for home, p, ready in active:
        if p.poll() is not None and p.stdout and not p.stdout.closed:
            try:
                record(
                    "remaining-helper-output",
                    home=home,
                    stdout=p.stdout.read(),
                    stderr=p.stderr.read(),
                    exit=p.returncode,
                )
            except Exception as e:
                record("remaining-helper-output-failure", home=home, error=str(e))
        if p.poll() is None:
            try:
                ctl("signal", home, ready["pid"], "SIGKILL")
            except Exception as e:
                record("cleanup-kill-failure", home=home, error=str(e))
        if not any(
            c.get("home") == home and c.get("cleanup", {}).get("homeRemoved")
            for c in cases
        ):
            try:
                ctl("cleanup", home)
            except Exception as e:
                record("cleanup-failure", home=home, error=str(e))
    (out / "results.json").write_text(
        json.dumps(
            {"cases": cases, "failures": failures, "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
sys.exit(bool(failures))
