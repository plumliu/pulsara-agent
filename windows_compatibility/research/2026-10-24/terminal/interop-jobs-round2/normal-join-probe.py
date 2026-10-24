"""One research-only normal EOF/Job drain case; frozen fixtures remain untouched."""

import ctypes as c
from ctypes import wintypes as w
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid
from job_probe import (
    K,
    H,
    SI,
    PI,
    Extended,
    check,
    creation,
    member,
    emit,
    rows,
    own_fact,
    job_facts,
    accounting,
)

BASE = Path(__file__).resolve().parent
LABEL = "normal-join-" + uuid.uuid4().hex[:8]
RECORD = BASE / (LABEL + ".jsonl")
LINUX = Path(str(RECORD) + ".linux.jsonl")
JOB_NAME = "Local\\PulsaraG0Normal-" + LABEL
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
PYTHON = "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python"
ENV = {k: v for k, v in os.environ.items() if k.upper() != "WSLENV"}


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
        PYTHON,
        "-u",
        "-c",
        code,
        *args,
    ]


def log(event, **kw):
    emit(str(RECORD), event, **kw)


def linux_facts(identities, cleanup=False):
    code = """import json,os,signal,sys
result=[]
for pid,expected,role in json.loads(sys.argv[1]):
 path='/proc/'+str(pid)+'/stat'
 actual=open(path).read().rsplit(')',1)[1].split()[19] if os.path.exists(path) else None
 row=dict(pid=pid,expectedStarttime=expected,starttime=actual,role=role,sameIdentity=actual==expected)
 if actual==expected and sys.argv[2]=='1':
  fd=os.pidfd_open(pid)
  try:
   current=open(path).read().rsplit(')',1)[1].split()[19]
   if current==expected: signal.pidfd_send_signal(fd,signal.SIGTERM); row['cleanupSignal']='SIGTERM-exact-pidfd'
  finally: os.close(fd)
 result.append(row)
print(json.dumps(result))
"""
    return json.loads(
        subprocess.check_output(
            command(code, json.dumps(identities), "1" if cleanup else "0"),
            env=ENV,
            text=True,
            timeout=15,
        )
    )


job = None
pi = None
read_fd = None
write_fd = None
output = None
held = {}
identities = []
result = {"label": LABEL, "passedObservedBoundary": False}
try:
    job = check(K.CreateJobObjectW(None, JOB_NAME))
    K.GetHandleInformation.argtypes = [H, c.POINTER(w.DWORD)]
    flags = w.DWORD()
    check(K.GetHandleInformation(job, c.byref(flags)))
    assert flags.value & 1 == 0
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    check(K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    anchor_code = "import json,os,sys; f=open(sys.argv[1],'a',buffering=1); f.write(json.dumps(dict(event='anchor-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=os.environ.get('WSL_INTEROP'),uid=os.getuid()))+'\\n'); data=sys.stdin.buffer.read(); f.write(json.dumps(dict(event='anchor-eof',pid=os.getpid(),bytes=len(data)))+'\\n')"
    args = command(anchor_code, linux_path(LINUX))
    # Only fixture stdio handles are inheritable; the Job handle never is.
    read_fd, write_fd = os.pipe()
    stdin_handle = msvcrt.get_osfhandle(read_fd)
    os.set_handle_inheritable(stdin_handle, True)
    output = open(str(RECORD) + ".carrier-output", "wb")
    out_handle = msvcrt.get_osfhandle(output.fileno())
    os.set_handle_inheritable(out_handle, True)
    si = SI()
    si.cb = c.sizeof(si)
    si.dwFlags = 0x100
    si.hStdInput = stdin_handle
    si.hStdOutput = out_handle
    si.hStdError = out_handle
    pi = PI()
    # This independent fixture process has no WSLENV forwarding list.
    os.environ.pop("WSLENV", None)
    check(
        K.CreateProcessW(
            args[0],
            c.create_unicode_buffer(subprocess.list2cmdline(args)),
            None,
            None,
            True,
            0x08000000 | 4,
            None,
            None,
            c.byref(si),
            c.byref(pi),
        )
    )
    os.close(read_fd)
    read_fd = None
    os.set_handle_inheritable(out_handle, False)
    check(K.AssignProcessToJobObject(job, pi.hProcess))
    log(
        "anchor-before-resume",
        carrierPid=pi.dwProcessId,
        carrierCreation=creation(pi.hProcess),
        carrierInJob=member(pi.hProcess, job),
        jobHandleFlags=flags.value,
        args=args,
    )
    assert not LINUX.exists()
    assert K.ResumeThread(pi.hThread) != 0xFFFFFFFF
    deadline = (
        time.monotonic() + 20
    )  # diagnostic startup observation, no product lifetime cap
    while not any(x["event"] == "anchor-ready" for x in rows(LINUX)):
        assert time.monotonic() < deadline, "anchor startup diagnostic deadline"
        time.sleep(0.02)
    ready = next(x for x in rows(LINUX) if x["event"] == "anchor-ready")
    assert ready["socket"] and ready["uid"] == 1000
    identities.append([ready["pid"], ready["starttime"], "anchor"])
    pe_args = [
        linux_path(sys.executable),
        str(BASE / "job_probe.py"),
        "pe",
        str(RECORD),
        JOB_NAME,
        "parent",
        "0",
        "1",
    ]
    runner_code = "import json,os,subprocess,sys; f=open(sys.argv[1],'a',buffering=1); env=os.environ.copy(); env['WSL_INTEROP']=sys.argv[2]; f.write(json.dumps(dict(event='runner-start',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=env['WSL_INTEROP']))+'\\n'); p=subprocess.Popen(sys.argv[3:],env=env,stdin=subprocess.DEVNULL); rc=p.wait(); f.write(json.dumps(dict(event='runner-exit',pid=os.getpid(),returncode=rc))+'\\n')"
    runner_args = command(runner_code, linux_path(LINUX), ready["socket"], *pe_args)
    runner = subprocess.run(
        runner_args, env=ENV, capture_output=True, text=True, timeout=20
    )
    log(
        "runner-carrier-closed",
        args=runner_args,
        returncode=runner.returncode,
        stdout=runner.stdout,
        stderr=runner.stderr,
    )
    assert runner.returncode == 0
    runner_ready = next(x for x in rows(LINUX) if x["event"] == "runner-start")
    identities.append([runner_ready["pid"], runner_ready["starttime"], "runner"])
    starts = {x["role"]: x for x in rows(RECORD) if x["event"] == "pe-start"}
    assert len(starts) == 2 and all(x["inJobBefore"] for x in starts.values())
    assert any(x["event"] == "parent-early-exit" for x in rows(RECORD))
    assert (
        next(x for x in rows(RECORD) if x["event"] == "ordinary-child-spawn")[
            "childStdio"
        ]
        == "DEVNULL"
    )
    parent = own_fact(starts["parent"]["pid"], starts["parent"]["creation"])
    child = own_fact(starts["child"]["pid"], starts["child"]["creation"])
    assert (
        parent["exists"] is False and child["exists"] is True and child["sameIdentity"]
    )
    before = job_facts(job)
    launcher_pid = next(
        x["childPid"] for x in rows(RECORD) if x["event"] == "ordinary-child-spawn"
    )
    for fact in before:
        ph = check(K.OpenProcess(0x1000 | 0x100000, False, fact["pid"]))
        assert creation(ph) == fact["creation"]
        role = (
            "anchor-carrier"
            if fact["pid"] == pi.dwProcessId
            else "windows-child"
            if fact["pid"] == starts["child"]["pid"]
            else "windows-child-venv-launcher"
            if fact["pid"] == launcher_pid
            else "infrastructure-" + Path(fact.get("image", "unknown")).name
        )
        held[fact["pid"]] = (fact, ph, role)
    log(
        "before-normal-eof",
        parent=parent,
        child=child,
        members=[dict(**fact, role=role) for fact, ph, role in held.values()],
        accounting=accounting(job),
        linux=linux_facts(identities),
    )
    assert not linux_facts([identities[1]])[0]["sameIdentity"], (
        "Linux runner must already be gone"
    )
    os.close(write_fd)
    write_fd = None
    log("anchor-stdin-eof-sent", jobHandleStillOpen=True, terminateJobCalled=False)
    assert K.WaitForSingleObject(pi.hProcess, 5000) == 0, (
        "anchor carrier did not leave after normal EOF"
    )
    eof_child = own_fact(starts["child"]["pid"], starts["child"]["creation"])
    log(
        "after-normal-eof",
        child=eof_child,
        members=job_facts(job),
        accounting=accounting(job),
        held=[
            dict(**fact, role=role, waitResult=K.WaitForSingleObject(ph, 0))
            for fact, ph, role in held.values()
        ],
        linux=linux_facts(identities),
        jobHandleStillOpen=True,
    )
    assert eof_child["exists"] is True and eof_child["sameIdentity"], (
        "normal anchor EOF killed Windows child"
    )
    assert not any(x["event"] == "pe-finished" for x in rows(RECORD)), (
        "finite child already ended before EOF observation"
    )
    # The child fixture naturally works for 25 seconds. This 40-second diagnostic
    # observation detects a stuck experiment; it is not a product task cap.
    deadline = time.monotonic() + 40
    while accounting(job)["ActiveProcesses"] != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    final_accounting = accounting(job)
    final_members = job_facts(job)
    waits = [
        dict(**fact, role=role, waitResult=K.WaitForSingleObject(ph, 0))
        for fact, ph, role in held.values()
    ]
    finished = [x for x in rows(RECORD) if x["event"] == "pe-finished"]
    final_linux = linux_facts(identities)
    log(
        "normal-join-observed",
        accounting=final_accounting,
        members=final_members,
        held=waits,
        naturalFinished=finished,
        linux=final_linux,
        jobHandleStillOpen=True,
        terminateJobCalled=False,
    )
    assert final_accounting["ActiveProcesses"] == 0 and final_members == []
    assert all(x["waitResult"] == 0 for x in waits)
    assert len(finished) == 1 and finished[0]["role"] == "child"
    assert all(not x["sameIdentity"] for x in final_linux)
    result.update(
        passedObservedBoundary=True,
        accounting=final_accounting,
        members=final_members,
        held=waits,
        naturalFinished=finished,
        linux=final_linux,
    )
except BaseException:
    result["failure"] = traceback.format_exc()
    log("probe-failure", traceback=result["failure"])
finally:
    if read_fd is not None:
        os.close(read_fd)
    if write_fd is not None:
        os.close(write_fd)
    if job:
        if accounting(job)["ActiveProcesses"]:
            log("failure-cleanup-terminate-exact-owned-job", members=job_facts(job))
            check(K.TerminateJobObject(job, 96))
            deadline = time.monotonic() + 5
            while accounting(job)["ActiveProcesses"] and time.monotonic() < deadline:
                time.sleep(0.02)
        result["cleanupAccounting"] = accounting(job)
        result["cleanupMembers"] = job_facts(job)
        log(
            "job-close-after-observation",
            accounting=result["cleanupAccounting"],
            members=result["cleanupMembers"],
            passed=result["passedObservedBoundary"],
        )
        K.CloseHandle(job)
    for _, ph, _ in held.values():
        K.CloseHandle(ph)
    if pi:
        K.CloseHandle(pi.hThread)
        K.CloseHandle(pi.hProcess)
    if output:
        output.close()
    if identities:
        cleanup_linux = linux_facts(identities)
        if any(x["sameIdentity"] for x in cleanup_linux):
            log("failure-cleanup-linux", facts=linux_facts(identities, cleanup=True))
            time.sleep(0.2)
        result["cleanupLinux"] = linux_facts(identities)
    log("cleanup-final", result=result)
    (BASE / (LABEL + "-summary.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
assert result["passedObservedBoundary"], result
