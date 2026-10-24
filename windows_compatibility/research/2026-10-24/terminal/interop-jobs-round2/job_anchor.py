"""Research fixture: one Windows Job owns one WSL interop anchor, no service."""

import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
from job_probe import K, H, SI, PI, Extended, check, creation, member, emit, job_facts

record, ready_file, job_name = sys.argv[1:]
os.environ.pop("WSLENV", None)
job = check(K.CreateJobObjectW(None, job_name))
K.GetHandleInformation.argtypes = [H, c.POINTER(w.DWORD)]
handle_flags = w.DWORD()
check(K.GetHandleInformation(job, c.byref(handle_flags)))
assert not handle_flags.value & 1, "Job handle must not be inheritable"
limits = Extended()
limits.BasicLimitInformation.LimitFlags = 0x2000
check(K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
linux_ready = ready_file.replace("\\", "/").replace("C:", "/mnt/c")
code = "import json,os,sys; f=open(sys.argv[1],'w'); json.dump(dict(event='anchor-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=os.environ.get('WSL_INTEROP'),uid=os.getuid()),f); f.close(); sys.stdin.buffer.read()"
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
    code,
    linux_ready,
]
read_fd, write_fd = os.pipe()
child_stdin = msvcrt.get_osfhandle(read_fd)
os.set_handle_inheritable(child_stdin, True)
out = open(record + ".carrier-output", "wb")
out_handle = msvcrt.get_osfhandle(out.fileno())
os.set_handle_inheritable(out_handle, True)
si = SI()
si.cb = c.sizeof(si)
si.dwFlags = 0x100
si.hStdInput = child_stdin
si.hStdOutput = out_handle
si.hStdError = out_handle
pi = PI()
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
os.set_handle_inheritable(out_handle, False)
try:
    check(K.AssignProcessToJobObject(job, pi.hProcess))
    emit(
        record,
        "anchor-before-resume",
        carrierPid=pi.dwProcessId,
        carrierCreation=creation(pi.hProcess),
        carrierInJob=member(pi.hProcess, job),
        ownerCreation=creation(K.GetCurrentProcess()),
        jobHandleFlags=handle_flags.value,
        args=args,
    )
    assert not Path(ready_file).exists(), (
        "Guest must not execute before assignment/resume"
    )
    assert K.ResumeThread(pi.hThread) != 0xFFFFFFFF
    while not Path(record + ".cancel").exists():
        if Path(record + ".eof").exists() and write_fd is not None:
            os.close(write_fd)
            write_fd = None
            emit(record, "anchor-stdin-eof")
        if Path(record + ".snapshot").exists():
            emit(record, "job-snapshot", members=job_facts(job))
            Path(record + ".snapshot").unlink()
        time.sleep(0.02)
    emit(record, "before-terminate-job", members=job_facts(job))
    check(K.TerminateJobObject(job, 92))
    emit(record, "terminate-job-returned")
finally:
    if write_fd is not None:
        os.close(write_fd)
    K.CloseHandle(pi.hThread)
    K.CloseHandle(pi.hProcess)
    K.CloseHandle(job)
    out.close()
