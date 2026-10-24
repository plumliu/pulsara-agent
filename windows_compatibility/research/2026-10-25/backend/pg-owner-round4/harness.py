"""Windows owns the real Node observer; every guest call selects Ubuntu/plumwsl."""

import os
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

OUT = Path(__file__).resolve().parent
REPO = "/home/plumwsl/workspaces/pulsara_agent"
REMOTE = REPO + "/output/windows-g0/pg_admission_r3/round4"
NODE = r"C:\Users\plumlocal\AppData\Local\mise\installs\node\22.23.3\node.exe"
BASE = [
    r"C:\Windows\System32\wsl.exe",
    "-d",
    "Ubuntu",
    "-u",
    "plumwsl",
    "--cd",
    REPO,
    "--exec",
]
records = []
cases = []
failures = []
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def record(kind, **facts):
    value = {"kind": kind, "time_ns": time.time_ns(), **facts}
    records.append(value)
    print(json.dumps(value), flush=True)


def control(action, home=None, *arguments):
    argv = BASE + [REPO + "/.venv/bin/python", REMOTE + "/control.py", action]
    argv += ([home] if home else []) + list(map(str, arguments))
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", timeout=35
    )
    record(
        "control",
        argv=argv,
        exit=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    assert result.returncode == 0, (argv, result.stderr)
    return json.loads(result.stdout)


def hold_windows(pid):
    handle = kernel32.OpenProcess(0x1000 | 0x100000, False, pid)
    assert handle, (pid, ctypes.get_last_error())
    return handle


scenarios = sys.argv[1:] or [
    "normal",
    "before-ready-eof",
    "ready-eof",
    "ready-cancel",
    "operation-eof",
    "reply-loss",
    "unterminated-result",
    "helper-death",
    "carrier-death",
    "node-death",
]
for scenario in scenarios:
    home = None
    node = None
    carrier_handles = {}
    case = {"scenario": scenario}
    cases.append(case)
    try:
        home = control("setup")["home"]
        case["home"] = home
        argv = [
            NODE,
            str(OUT / "owner.cjs"),
            home,
            scenario,
            os.environ.get("PG4_IMPLEMENTATION", "baseline"),
        ]
        node = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        case["node_pid"] = node.pid
        case["events"] = []
        record("node-spawn", argv=argv, pid=node.pid)
        for line in node.stdout:
            event = json.loads(line)
            case["events"].append(event)
            record("node-event", scenario=scenario, record=event)
            if event["event"] == "carrier-spawn":
                carrier_handles[event["carrierPid"]] = hold_windows(event["carrierPid"])
            if event["event"] == "node-kill-ready":
                case["before_node_death"] = control("state", home)
                node.kill()
                record("node-terminated", pid=node.pid, state=case["before_node_death"])
                case["after_node_death"] = control("state", home)
                control("resume-pg", home)
        stderr = node.stderr.read()
        node.wait(timeout=25)
        case["node_exit"] = node.returncode
        case["stderr"] = stderr
        record("node-exit", scenario=scenario, exit=node.returncode, stderr=stderr)
        if scenario != "node-death":
            assert node.returncode == 0, stderr
            assert any(event["event"] == "case-complete" for event in case["events"])
        if scenario in ("helper-death", "carrier-death", "node-death"):
            deadline = time.monotonic() + 15
            while True:
                state = control("state", home)
                if state["kernel"]["acquired"] and state["status"]["exit"] == 0:
                    query = control("query", home)
                    if query["exit"] == 0:
                        break
                assert time.monotonic() < deadline, state
                time.sleep(0.05)
            case["after_loss_query"] = query
            case["after_loss_state"] = state
        case["cleanup"] = control("cleanup", home)
        case["carrier_waits"] = [
            {"pid": pid, "wait": kernel32.WaitForSingleObject(handle, 5000)}
            for pid, handle in carrier_handles.items()
        ]
        assert all(item["wait"] == 0 for item in case["carrier_waits"]), case[
            "carrier_waits"
        ]
        record("case-result", case=case)
    except BaseException:
        failure = traceback.format_exc()
        failures.append(failure)
        record("failure", scenario=scenario, traceback=failure)
        if node and node.poll() is None:
            node.kill()
            node.wait(timeout=10)
        if home:
            try:
                case["failure_cleanup"] = control("cleanup", home)
            except BaseException:
                record("cleanup-failure", traceback=traceback.format_exc())
        break
    finally:
        for handle in carrier_handles.values():
            kernel32.CloseHandle(handle)

(OUT / "results.json").write_text(
    json.dumps({"cases": cases, "failures": failures, "records": records}, indent=2),
    encoding="utf-8",
)
raise SystemExit(bool(failures))
