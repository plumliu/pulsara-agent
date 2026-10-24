"""Does the original carrier ancestry prevent fallback after anchor death?"""

import ctypes as c
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import socket_probe as S

BASE = Path(__file__).resolve().parent


def run():
    label = "same-chain-" + uuid.uuid4().hex[:8]
    record = BASE / (label + ".jsonl")
    linux_record = Path(str(record) + ".linux")
    gate = BASE / (label + ".go")
    name = "Local\\PulsaraG0Socket-" + label
    job = S.J.check(S.J.K.CreateJobObjectW(None, name))
    limits = S.J.Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    S.J.check(S.J.K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    other = S.anchor(label + "-B")
    pe_args = [
        S.linux_path(sys.executable),
        str(BASE / "socket_probe.py"),
        "pe",
        str(record),
        name,
        other["name"],
        "parent",
        "0",
        ".2",
    ]
    worker = """import json,os,subprocess,sys,time
record,gate=sys.argv[1:3]
f=open(record,'a',buffering=1)
f.write(json.dumps(dict(event='worker-ready',pid=os.getpid(),ppid=os.getppid(),pgid=os.getpgrp(),sid=os.getsid(0),parentPgid=os.getpgid(os.getppid()),starttime=open('/proc/self/stat').read().split()[21],socket=os.environ.get('WSL_INTEROP')))+'\\n')
deadline=time.monotonic()+20
while not os.path.exists(gate):
 if time.monotonic()>deadline: raise TimeoutError('gate')
 time.sleep(.02)
f.write(json.dumps(dict(event='worker-after-death',pid=os.getpid(),ppid=os.getppid(),socketExists=os.path.exists(os.environ['WSL_INTEROP'])))+'\\n')
p=subprocess.Popen(sys.argv[3:],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
f.write(json.dumps(dict(event='worker-done',code=p.wait()))+'\\n')
"""
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-u','-c',sys.argv[1],*sys.argv[2:]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=False); time.sleep(90)"
    args = S.command(
        code, worker, S.linux_path(linux_record), S.linux_path(gate), *pe_args
    )
    si = S.J.SI()
    si.cb = c.sizeof(si)
    pi = S.J.PI()
    S.J.check(
        S.J.K.CreateProcessW(
            args[0],
            c.create_unicode_buffer(subprocess.list2cmdline(args)),
            None,
            None,
            False,
            0x08000000 | 4,
            None,
            None,
            c.byref(si),
            c.byref(pi),
        )
    )
    summary = {"label": label, "args": args}
    try:
        S.J.check(S.J.K.AssignProcessToJobObject(job, pi.hProcess))
        assert S.J.K.ResumeThread(pi.hThread) != 0xFFFFFFFF
        ready = S.wait_row(linux_record, "worker-ready")
        summary["ready"] = ready
        S.J.check(S.J.K.TerminateJobObject(job, 97))
        assert S.J.K.WaitForSingleObject(pi.hProcess, 5000) == 0
        time.sleep(0.5)
        gate.touch()
        done = S.wait_row(linux_record, "worker-done")
        summary["linux"] = S.J.rows(linux_record)
        summary["facts"] = S.facts(record)
        assert done["code"] == 0
        assert len(summary["facts"]) == 2
        assert all(not any(x["memberships"].values()) for x in summary["facts"])
        summary["fallbackCounterexampleObserved"] = True
    finally:
        for row in S.J.rows(record):
            if row["event"] == "pe-start":
                S.J.emit(
                    record,
                    "cleanup-exact-pe",
                    fact=S.J.own_fact(row["pid"], row["creation"], True),
                )
        S.J.K.TerminateJobObject(job, 98)
        for handle in [pi.hThread, pi.hProcess, job]:
            S.J.K.CloseHandle(handle)
        S.cleanup_anchor(other)
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(path)


if __name__ == "__main__":
    run()
