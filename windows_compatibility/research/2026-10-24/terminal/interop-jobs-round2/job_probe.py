"""Research-only Windows Job/WSL probe. Never imported by production code."""

import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

K = c.WinDLL("kernel32", use_last_error=True)
H = w.HANDLE
K.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
K.CreateJobObjectW.restype = H
K.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
K.OpenJobObjectW.restype = H
K.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
K.OpenProcess.restype = H
for name, args in {
    "CloseHandle": [H],
    "AssignProcessToJobObject": [H, H],
    "IsProcessInJob": [H, H, c.POINTER(w.BOOL)],
    "SetInformationJobObject": [H, c.c_int, c.c_void_p, w.DWORD],
    "TerminateJobObject": [H, w.UINT],
    "TerminateProcess": [H, w.UINT],
    "GetProcessTimes": [
        H,
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
    ],
    "WaitForSingleObject": [H, w.DWORD],
    "ResumeThread": [H],
}.items():
    getattr(K, name).argtypes = args
K.GetCurrentProcess.restype = H
K.QueryInformationJobObject.argtypes = [
    H,
    c.c_int,
    c.c_void_p,
    w.DWORD,
    c.POINTER(w.DWORD),
]
K.QueryFullProcessImageNameW.argtypes = [H, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]


class Basic(c.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", c.c_longlong),
        ("PerJobUserTimeLimit", c.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", c.c_size_t),
        ("MaximumWorkingSetSize", c.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", c.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IO(c.Structure):
    _fields_ = [
        (x, c.c_ulonglong)
        for x in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class Extended(c.Structure):
    _fields_ = [
        ("BasicLimitInformation", Basic),
        ("IoInfo", IO),
        ("ProcessMemoryLimit", c.c_size_t),
        ("JobMemoryLimit", c.c_size_t),
        ("PeakProcessMemoryUsed", c.c_size_t),
        ("PeakJobMemoryUsed", c.c_size_t),
    ]


class SI(c.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", c.c_void_p),
        ("hStdInput", H),
        ("hStdOutput", H),
        ("hStdError", H),
    ]


class PI(c.Structure):
    _fields_ = [
        ("hProcess", H),
        ("hThread", H),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


K.CreateProcessW.argtypes = [
    w.LPCWSTR,
    w.LPWSTR,
    c.c_void_p,
    c.c_void_p,
    w.BOOL,
    w.DWORD,
    c.c_void_p,
    w.LPCWSTR,
    c.POINTER(SI),
    c.POINTER(PI),
]


def check(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


def creation(handle):
    ft = [w.FILETIME() for _ in range(4)]
    check(K.GetProcessTimes(handle, *[c.byref(x) for x in ft]))
    return (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime


def member(handle, job):
    result = w.BOOL()
    check(K.IsProcessInJob(handle, job, c.byref(result)))
    return bool(result.value)


def emit(path, event, **kw):
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(dict(event=event, time=time.time(), pid=os.getpid(), **kw))
            + "\n"
        )


def rows(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def own_fact(pid, expected=None, terminate=False):
    handle = K.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, pid)
    if not handle:
        error = c.get_last_error()
        return dict(pid=pid, exists=False if error == 87 else None, error=error)
    try:
        actual = creation(handle)
        same = expected is None or actual == expected
        result = dict(
            pid=pid,
            exists=K.WaitForSingleObject(handle, 0) == 258,
            creation=actual,
            sameIdentity=same,
        )
        buf = c.create_unicode_buffer(32768)
        size = w.DWORD(len(buf))
        if K.QueryFullProcessImageNameW(handle, 0, buf, c.byref(size)):
            result["image"] = buf.value
        if terminate and same and result["exists"]:
            check(K.TerminateProcess(handle, 91))
            result["terminatedExactHandle"] = True
            result["waitResult"] = K.WaitForSingleObject(handle, 5000)
        return result
    finally:
        K.CloseHandle(handle)


def job_facts(job):
    count = 16  # initial allocation grows from the OS count; no process admission limit
    while True:
        buf = c.create_string_buffer(8 + count * c.sizeof(c.c_size_t))
        returned = w.DWORD()
        ok = K.QueryInformationJobObject(job, 3, buf, len(buf), c.byref(returned))
        assigned, listed = (w.DWORD * 2).from_buffer(buf)
        if ok:
            pids = (c.c_size_t * listed).from_buffer(buf, 8)
            return [own_fact(pid) for pid in pids]
        if c.get_last_error() != 234:
            check(ok)
        count = max(count * 2, assigned)


class Accounting(c.Structure):
    _fields_ = [
        (x, c.c_longlong)
        for x in (
            "TotalUserTime",
            "TotalKernelTime",
            "ThisPeriodTotalUserTime",
            "ThisPeriodTotalKernelTime",
        )
    ] + [
        (x, w.DWORD)
        for x in (
            "TotalPageFaultCount",
            "TotalProcesses",
            "ActiveProcesses",
            "TotalTerminatedProcesses",
        )
    ]


def accounting(job):
    info = Accounting()
    returned = w.DWORD()
    check(
        K.QueryInformationJobObject(
            job, 1, c.byref(info), c.sizeof(info), c.byref(returned)
        )
    )
    return {name: getattr(info, name) for name, _ in Accounting._fields_}


def pe(record, job_name, role, enroll, root_exit):
    job = check(K.OpenJobObjectW(0x0004 | (0x0001 if enroll else 0), False, job_name))
    handle = K.GetCurrentProcess()
    before = member(handle, job)
    if enroll:
        check(K.AssignProcessToJobObject(job, handle))
    after = member(handle, job)
    # Enrollment retains no extra job handle: owner death must close the last one.
    check(K.CloseHandle(job))
    emit(
        record,
        "pe-start",
        role=role,
        ppid=os.getppid(),
        creation=creation(handle),
        inJobBefore=before,
        inJobAfter=after,
        python=sys.version,
    )
    if role == "parent":
        child = subprocess.Popen(
            [sys.executable, __file__, "pe", record, job_name, "child", "0", "0"],
            creationflags=0x08000000,
            **(
                {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                if root_exit
                else {}
            ),
        )
        emit(
            record,
            "ordinary-child-spawn",
            childPid=child.pid,
            childStdio="DEVNULL" if root_exit else "inherit",
        )
        while not any(x.get("role") == "child" for x in rows(Path(record))):
            time.sleep(0.02)
        if root_exit:
            emit(record, "parent-early-exit")
            return
        child.wait()
    else:
        time.sleep(25)  # finite probe workload, not a product lifetime cap
    emit(record, "pe-finished", role=role)


def owner(record, job_name, mode, root_exit):
    os.environ.pop("WSLENV", None)
    job = check(K.CreateJobObjectW(None, job_name))
    K.GetHandleInformation.argtypes = [H, c.POINTER(w.DWORD)]
    job_flags = w.DWORD()
    check(K.GetHandleInformation(job, c.byref(job_flags)))
    assert not job_flags.value & 1
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
    check(K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    pe_args = [
        sys.executable,
        __file__,
        "pe",
        record,
        job_name,
        "parent",
        "1" if mode == "enroll" else "0",
        root_exit,
    ]
    if mode == "native":
        args = pe_args
    else:
        linux_record = (
            record.replace("\\", "/").replace("C:", "/mnt/c") + ".linux.jsonl"
        )
        linux_code = "import json,os,subprocess,sys,time; f=open(sys.argv[1],'a',buffering=1); f.write(json.dumps(dict(event='linux-start',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21]))+'\\n'); p=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL); f.write(json.dumps(dict(event='proxy-start',pid=p.pid,starttime=open('/proc/'+str(p.pid)+'/stat').read().split()[21]))+'\\n'); rc=p.wait(); f.write(json.dumps(dict(event='linux-wait',returncode=rc))+'\\n')"
        pe_args[0] = pe_args[0].replace("\\", "/").replace("C:", "/mnt/c")
        args = [
            os.environ["SystemRoot"] + r"\System32\wsl.exe",
            "-d",
            "Ubuntu",
            "-u",
            "plumwsl",
            "--cd",
            "/home/plumwsl/workspaces/pulsara_agent",
            "--exec",
            "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
            "-u",
            "-c",
            linux_code,
            linux_record,
            *pe_args,
        ]
    si = SI()
    si.cb = c.sizeof(si)
    pi = PI()
    check(
        K.CreateProcessW(
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
    try:
        check(K.AssignProcessToJobObject(job, pi.hProcess))
        emit(
            record,
            "carrier-assigned",
            carrierPid=pi.dwProcessId,
            carrierCreation=creation(pi.hProcess),
            carrierInJob=member(pi.hProcess, job),
            ownerCreation=creation(K.GetCurrentProcess()),
            jobHandleFlags=job_flags.value,
            mode=mode,
            args=args,
        )
        assert not any(x["event"] == "pe-start" for x in rows(Path(record)))
        assert K.ResumeThread(pi.hThread) != 0xFFFFFFFF
        while not Path(record + ".cancel").exists():
            time.sleep(0.02)
        check(K.TerminateJobObject(job, 92))
        emit(record, "terminate-job-returned")
        deadline = time.monotonic() + 5
        while accounting(job)["ActiveProcesses"] and time.monotonic() < deadline:
            time.sleep(0.01)
        emit(
            record,
            "job-accounting-after-terminate",
            accounting=accounting(job),
            members=job_facts(job),
        )
        assert accounting(job)["ActiveProcesses"] == 0
        for x in rows(Path(record)):
            if x["event"] == "pe-start":
                emit(
                    record,
                    "member-after-terminate",
                    role=x["role"],
                    fact=own_fact(x["pid"], x["creation"]),
                )
    finally:
        K.CloseHandle(pi.hThread)
        K.CloseHandle(pi.hProcess)
        K.CloseHandle(job)


def run():
    base = Path(__file__).resolve().parent
    result = []
    for mode in ["native", "carrier", "enroll"]:
        for action in ["cancel", "crash", "rootexit-crash"]:
            label = mode + "-" + action + "-" + uuid.uuid4().hex[:8]
            record = base / (label + ".jsonl")
            job_name = "Local\\PulsaraG0Research-" + label
            host = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "owner",
                    str(record),
                    job_name,
                    mode,
                    "1" if action == "rootexit-crash" else "0",
                ]
            )
            owner_creation = own_fact(host.pid)["creation"]
            deadline = time.monotonic() + 30
            try:
                while not any(x.get("role") == "child" for x in rows(record)):
                    if host.poll() is not None:
                        raise RuntimeError(f"owner exited {host.returncode}: {record}")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"probe startup: {record}")
                    time.sleep(0.05)
                if action == "rootexit-crash":
                    time.sleep(0.5)
                starts = [x for x in rows(record) if x["event"] == "pe-start"]
                before = [
                    dict(
                        role=x["role"],
                        fact=own_fact(x["pid"], x["creation"]),
                        inJob=x["inJobAfter"],
                    )
                    for x in starts
                ]
                query_job = check(K.OpenJobObjectW(4, False, job_name))
                job_members = job_facts(query_job)
                check(K.CloseHandle(query_job))
                emit(
                    record,
                    "controller-before-fault",
                    facts=before,
                    jobMembers=job_members,
                )
                if action == "cancel":
                    Path(str(record) + ".cancel").touch()
                    host.wait(timeout=10)
                else:
                    emit(
                        record,
                        "exact-owner-crash",
                        fact=own_fact(host.pid, owner_creation, True),
                    )
                    host.wait(timeout=10)
                time.sleep(0.5)
                after = [
                    dict(role=x["role"], fact=own_fact(x["pid"], x["creation"]))
                    for x in starts
                ]
                emit(
                    record,
                    "controller-after-fault",
                    facts=after,
                    ownerExit=host.returncode,
                )
                expected = True  # all three modes observed to inherit; assert the repeated fact
                assert all(x["inJobAfter"] == expected for x in starts), (mode, starts)
                if expected:
                    assert all(x["fact"]["exists"] is False for x in after), (
                        mode,
                        after,
                    )
                result.append(
                    dict(
                        label=label,
                        mode=mode,
                        action=action,
                        before=before,
                        after=after,
                        passedObservation=True,
                    )
                )
            finally:
                if host.poll() is None:
                    emit(
                        record,
                        "cleanup-owner",
                        fact=own_fact(host.pid, owner_creation, True),
                    )
                # Only the exact process identities emitted by this finite probe.
                for x in reversed(rows(record)):
                    if x["event"] == "pe-start":
                        emit(
                            record,
                            "cleanup-pe",
                            role=x["role"],
                            fact=own_fact(x["pid"], x["creation"], True),
                        )
                time.sleep(0.2)
                emit(
                    record,
                    "cleanup-final-windows",
                    facts=[
                        own_fact(x["pid"], x["creation"])
                        for x in rows(record)
                        if x["event"] == "pe-start"
                    ],
                )
    (base / "matrix-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        run()
    elif sys.argv[1] == "pe":
        pe(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            sys.argv[5] == "1",
            sys.argv[6] == "1",
        )
    elif sys.argv[1] == "owner":
        owner(*sys.argv[2:])
