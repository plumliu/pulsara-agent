"""Real interop with a Linux-owned O_PATH reference; no protocol proxy."""

import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import socket_probe as S

BASE = Path(__file__).resolve().parent


def launch(record, anchor, other, gate=None):
    args = [
        S.linux_path(sys.executable),
        str(BASE / "socket_probe.py"),
        "pe",
        str(record),
        anchor["name"],
        other["name"],
        "parent",
        "0",
        ".3",
    ]
    code = """import json,os,subprocess,sys,time
record,socket,gate=sys.argv[1:4]
f=open(record,'a',buffering=1)
fd=os.open(socket,os.O_PATH|os.O_CLOEXEC)
pin='/proc/'+str(os.getpid())+'/fd/'+str(fd)
f.write(json.dumps(dict(event='pin-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=socket,pin=pin,inode=os.stat(pin).st_ino,fd=fd))+'\\n')
if gate:
 deadline=time.monotonic()+20
 while not os.path.exists(gate):
  if time.monotonic()>deadline: raise TimeoutError('probe gate')
  time.sleep(.02)
f.write(json.dumps(dict(event='before-pe',pinExists=os.access(pin,os.F_OK),originalExists=os.path.exists(socket),statLinks=os.stat(pin).st_nlink))+'\\n')
env=os.environ.copy(); env['WSL_INTEROP']=pin
p=subprocess.Popen(sys.argv[4:],env=env,stdin=subprocess.DEVNULL,start_new_session=True)
rc=p.wait()
f.write(json.dumps(dict(event='after-pe',code=rc,pinExists=os.access(pin,os.F_OK)))+'\\n')
os.close(fd)
"""
    return subprocess.Popen(
        S.command(
            code,
            S.linux_path(str(record) + ".linux"),
            anchor["ready"]["socket"],
            S.linux_path(gate) if gate else "",
            *args,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run():
    label = "pin-" + uuid.uuid4().hex[:8]
    a = S.anchor(label + "-A")
    b = S.anchor(label + "-B")
    records = []
    runners = []
    summary = {"label": label}
    try:
        live = BASE / (label + "-live.jsonl")
        records.append(live)
        runner = launch(live, a, b)
        runners.append(runner)
        out, err = runner.communicate(timeout=10)
        summary["live"] = {
            "returncode": runner.returncode,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "linux": S.J.rows(Path(str(live) + ".linux")),
            "facts": S.facts(live),
        }
        assert len(summary["live"]["facts"]) == 2
        assert all(
            x["memberships"][a["name"]] and not x["memberships"][b["name"]]
            for x in summary["live"]["facts"]
        )
        gated = BASE / (label + "-after-anchor-death.jsonl")
        records.append(gated)
        gate = BASE / (label + ".go")
        runner = launch(gated, a, b, gate)
        runners.append(runner)
        S.wait_row(Path(str(gated) + ".linux"), "pin-ready")
        S.J.check(S.J.K.TerminateJobObject(a["job"], 96))
        # Wait exact carrier HANDLE, then give the WSL relay time to remove its path.
        assert S.J.K.WaitForSingleObject(a["pi"].hProcess, 5000) == 0
        time.sleep(0.5)
        gate.touch()
        out, err = runner.communicate(timeout=10)
        linux = S.J.rows(Path(str(gated) + ".linux"))
        summary["afterAnchorDeath"] = {
            "returncode": runner.returncode,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "linux": linux,
            "peFacts": S.facts(gated),
        }
        before = [x for x in linux if x["event"] == "before-pe"][-1]
        after = [x for x in linux if x["event"] == "after-pe"][-1]
        assert (
            before["pinExists"]
            and not before["originalExists"]
            and before["statLinks"] == 0
        )
        assert after["code"] != 0 and not summary["afterAnchorDeath"]["peFacts"]
        summary["passedObservedBoundary"] = True
    finally:
        for record in records:
            for row in S.J.rows(record):
                if row["event"] == "pe-start":
                    S.J.emit(
                        record,
                        "cleanup-exact-pe",
                        fact=S.J.own_fact(row["pid"], row["creation"], True),
                    )
        S.cleanup_anchor(a)
        S.cleanup_anchor(b)
        for runner in runners:
            if runner.poll() is None:
                runner.communicate(timeout=10)
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(path)


if __name__ == "__main__":
    run()
