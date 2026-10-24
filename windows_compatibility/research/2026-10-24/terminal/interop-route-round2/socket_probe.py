"""Research-only per-operation WSL_INTEROP routing; reuse Job probe API."""

import ctypes as c
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "job_fixture", BASE.parents[1] / "interop_jobs/round2/job_probe.py"
)
J = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(J)
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
LINUX_PY = "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python"


def linux_path(path):
    return str(path).replace("\\", "/").replace("C:", "/mnt/c")


def command(code, *args):
    return [
        WSL,
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--cd",
        "/home/plumwsl/workspaces/pulsara_agent",
        "--exec",
        LINUX_PY,
        "-u",
        "-c",
        code,
        *args,
    ]


def wait_row(path, event, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [x for x in J.rows(path) if x["event"] == event]
        if matches:
            return matches[-1]
        time.sleep(0.03)
    raise TimeoutError((path, event))


def anchor(label):
    record = BASE / (label + ".anchor.jsonl")
    name = "Local\\PulsaraG0Socket-" + label
    job = J.check(J.K.CreateJobObjectW(None, name))
    limits = J.Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    J.check(J.K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    code = "import json,os,sys,time; p=sys.argv[1]; f=open(p,'a',buffering=1); f.write(json.dumps(dict(event='anchor-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=os.environ.get('WSL_INTEROP')))+'\\n'); time.sleep(90)"
    args = command(code, linux_path(record))
    si = J.SI()
    si.cb = c.sizeof(si)
    pi = J.PI()
    J.check(
        J.K.CreateProcessW(
            args[0],
            c.create_unicode_buffer(subprocess.list2cmdline(args)),
            None,
            None,
            False,
            0x08000000 | 0x4,
            None,
            None,
            c.byref(si),
            c.byref(pi),
        )
    )
    J.check(J.K.AssignProcessToJobObject(job, pi.hProcess))
    assert J.K.ResumeThread(pi.hThread) != 0xFFFFFFFF
    ready = wait_row(record, "anchor-ready")
    J.emit(
        record,
        "anchor-job",
        job=name,
        carrierPid=pi.dwProcessId,
        carrierCreation=J.creation(pi.hProcess),
        carrierMember=J.member(pi.hProcess, job),
        args=args,
    )
    return dict(record=record, name=name, job=job, pi=pi, ready=ready)


def pe(record, a_name, b_name, role, root_exit, seconds):
    memberships = {}
    for name in [a_name, b_name]:
        job = J.check(J.K.OpenJobObjectW(0x4, False, name))
        memberships[name] = J.member(J.K.GetCurrentProcess(), job)
        J.K.CloseHandle(job)
    J.emit(
        record,
        "pe-start",
        role=role,
        ppid=os.getppid(),
        creation=J.creation(J.K.GetCurrentProcess()),
        memberships=memberships,
    )
    if role == "parent":
        child = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "pe",
                record,
                a_name,
                b_name,
                "child",
                "0",
                seconds,
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=0x08000000,
        )
        wait_row(Path(record), "child-ready")
        if root_exit == "1":
            J.emit(record, "parent-early-exit")
            return
        child.wait()
    else:
        J.emit(record, "child-ready")
        time.sleep(float(seconds))
    J.emit(record, "pe-finished", role=role)


def launch(record, socket, a, b, root_exit=False, seconds=5):
    pe_args = [
        linux_path(sys.executable),
        str(Path(__file__).resolve()),
        "pe",
        str(record),
        a["name"],
        b["name"],
        "parent",
        "1" if root_exit else "0",
        str(seconds),
    ]
    code = "import json,os,subprocess,sys; e=os.environ.copy(); original=e.get('WSL_INTEROP'); e['WSL_INTEROP']=sys.argv[2]; f=open(sys.argv[1],'a',buffering=1); f.write(json.dumps(dict(event='runner-start',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],originalSocket=original,targetSocket=e['WSL_INTEROP']))+'\\n'); p=subprocess.Popen(sys.argv[3:],env=e,stdin=subprocess.DEVNULL,start_new_session=True); rc=p.wait(); f.write(json.dumps(dict(event='runner-exit',code=rc))+'\\n')"
    return subprocess.Popen(
        command(code, linux_path(str(record) + ".linux"), socket, *pe_args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def facts(record):
    return [
        dict(
            role=x["role"],
            fact=J.own_fact(x["pid"], x["creation"]),
            memberships=x["memberships"],
        )
        for x in J.rows(record)
        if x["event"] == "pe-start"
    ]


def cleanup_anchor(a):
    J.check(J.K.TerminateJobObject(a["job"], 94))
    ready = a["ready"]
    code = "import json,os,signal,sys; pid=int(sys.argv[1]); expected=sys.argv[2]; p='/proc/'+str(pid)+'/stat'; exists=os.path.exists(p); actual=open(p).read().split()[21] if exists else None; same=actual==expected; print(json.dumps(dict(pid=pid,exists=exists,starttime=actual,sameIdentity=same))); os.kill(pid,signal.SIGTERM) if same else None"
    clean = subprocess.run(
        command(code, str(ready["pid"]), ready["starttime"]),
        capture_output=True,
        text=True,
        timeout=10,
    )
    J.emit(
        a["record"],
        "cleanup-exact-linux-anchor",
        output=clean.stdout,
        stderr=clean.stderr,
        code=clean.returncode,
    )
    for handle in [a["pi"].hThread, a["pi"].hProcess, a["job"]]:
        J.K.CloseHandle(handle)


def run():
    label = uuid.uuid4().hex[:8]
    a = anchor(label + "-A")
    b = anchor(label + "-B")
    result = BASE / (label + "-summary.json")
    records = []
    runners = []
    summary = {
        "label": label,
        "anchors": {key: value["ready"] for key, value in [("A", a), ("B", b)]},
    }
    try:
        for key, target in [("A", a), ("B", b)]:
            record = BASE / (label + "-route-" + key + ".jsonl")
            records.append(record)
            runners.append(
                launch(
                    record,
                    target["ready"]["socket"],
                    a,
                    b,
                    root_exit=key == "A",
                    seconds=9,
                )
            )
            wait_row(record, "child-ready")
        time.sleep(0.2)
        summary["beforeCancel"] = {"A": facts(records[0]), "B": facts(records[1])}
        assert all(
            x["memberships"][a["name"]] and not x["memberships"][b["name"]]
            for x in summary["beforeCancel"]["A"]
        )
        assert all(
            x["memberships"][b["name"]] and not x["memberships"][a["name"]]
            for x in summary["beforeCancel"]["B"]
        )
        assert any(x["event"] == "parent-early-exit" for x in J.rows(records[0]))
        assert runners[0].poll() is None, (
            "root-exited Windows child must keep pipe/relay alive"
        )
        J.check(J.K.TerminateJobObject(a["job"], 95))
        time.sleep(0.4)
        summary["afterCancel"] = {"A": facts(records[0]), "B": facts(records[1])}
        assert all(not x["fact"]["exists"] for x in summary["afterCancel"]["A"])
        assert all(x["fact"]["exists"] for x in summary["afterCancel"]["B"])
        for runner in runners:
            out, err = runner.communicate(timeout=15)
            assert runner.returncode == 0, (runner.returncode, out, err)
        summary["normalBFinished"] = [
            x for x in J.rows(records[1]) if x["event"] == "pe-finished"
        ]
        assert len(summary["normalBFinished"]) == 2
        # A's socket disappeared with its anchor; WSL's documented fallback is measured.
        stale = BASE / (label + "-missing-socket.jsonl")
        records.append(stale)
        runner = launch(stale, a["ready"]["socket"], a, b, seconds=0.2)
        runners.append(runner)
        out, err = runner.communicate(timeout=10)
        summary["missingSocket"] = {
            "code": runner.returncode,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "peFacts": facts(stale),
            "linux": J.rows(Path(str(stale) + ".linux")),
        }
        assert summary["missingSocket"]["peFacts"], (
            "counterexample expected fallback to runner's own relay"
        )
        assert all(
            not any(x["memberships"].values())
            for x in summary["missingSocket"]["peFacts"]
        )
        summary["observed"] = True
    finally:
        for record in records:
            for row in J.rows(record):
                if row["event"] == "pe-start":
                    J.emit(
                        record,
                        "cleanup-exact-pe",
                        fact=J.own_fact(row["pid"], row["creation"], True),
                    )
        cleanup_anchor(a)
        cleanup_anchor(b)
        for runner in runners:
            if runner.poll() is None:
                runner.communicate(timeout=10)
        result.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(result)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pe":
        pe(*sys.argv[2:])
    else:
        run()
