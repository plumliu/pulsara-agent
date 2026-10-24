"""One exact recovery of the retained first fixture logging failure."""

import json
import subprocess
import time

import windows_probe as W

label = "normal-pty-0dac9d8d"
summary = json.loads((W.BASE / (label + "-summary.json")).read_text())
facts = [
    W.J.own_fact(row["pid"], row["creation"])
    for row in summary["cleanupWindows"][0]["held"]
]
assert all(row["exists"] is False or row.get("sameIdentity") is False for row in facts)
W.atomic_json(
    W.BASE / (label + "-A.job-joined.json"), dict(recoveryExactFacts=facts, joined=True)
)
record = W.BASE / (label + ".manager.jsonl")
deadline = time.monotonic() + 20
while time.monotonic() < deadline and not any(
    row["event"] == "manager-finished" for row in W.J.rows(record)
):
    time.sleep(0.02)
result = dict(
    windows=facts,
    managerFinished=any(row["event"] == "manager-finished" for row in W.J.rows(record)),
)
code = """import json,os,sys
from pathlib import Path
matches=[]
for p in Path('/proc').iterdir():
 if p.name.isdigit() and int(p.name)!=os.getpid():
  try:
   args=(p/'cmdline').read_bytes()
   if sys.argv[1].encode() in args and b'manager_probe.py' in args:
    fd=os.pidfd_open(int(p.name)); current=(p/'stat').read_text().rsplit(')',1)[1].split()[19]
    matches.append(dict(pid=int(p.name),starttime=current)); os.close(fd)
  except (FileNotFoundError,ProcessLookupError): pass
print(json.dumps(dict(matches=matches,privateDirectoryExists=Path(sys.argv[2]).exists())))
"""
audit = subprocess.run(
    W.guest(
        "-c",
        code,
        label + ".manager-config.json",
        "/home/plumwsl/.g0t4-" + label + "-A",
    ),
    capture_output=True,
    text=True,
    check=True,
    env=W.ENV,
    timeout=10,
)
result["linuxAudit"] = json.loads(audit.stdout)
(W.BASE / "first-probe-recovery-final.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
print(json.dumps(result))
assert (
    result["managerFinished"]
    and not result["linuxAudit"]["privateDirectoryExists"]
    and not result["linuxAudit"]["matches"]
)
