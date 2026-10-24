"""Read-only final identity audit of this review directory only."""

import json
from pathlib import Path
import subprocess
import socket_probe as S

base = Path(__file__).resolve().parent
windows = {}
linux = {}
for path in base.glob("*.jsonl*"):
    try:
        for row in S.J.rows(path):
            if row.get("event") == "pe-start":
                windows[(row["pid"], row["creation"])] = True
            if row.get("event") == "anchor-job":
                windows[(row["carrierPid"], row["carrierCreation"])] = True
            if (
                row.get("event")
                in {
                    "anchor-ready",
                    "pin-ready",
                    "relay-ready",
                    "worker-ready",
                    "runner-start",
                }
                and "starttime" in row
            ):
                linux[(row["pid"], str(row["starttime"]))] = True
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
for path in base.glob("*summary.json"):
    data = json.loads(path.read_text(encoding="utf-8"))

    def visit(value):
        if isinstance(value, dict):
            if value.get("event") == "relay-ready":
                linux[(value["pid"], str(value["starttime"]))] = True
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(data)
winfacts = [S.J.own_fact(pid, creation) for pid, creation in windows]
code = """import json,os,sys
result=[]
for pid,expected in json.load(sys.stdin):
 path='/proc/'+str(pid)+'/stat'
 try:
  data=open(path).read(); rest=data[data.rfind(')')+2:].split(); actual=rest[19]
  result.append(dict(pid=pid,expectedStarttime=expected,exists=True,starttime=actual,state=rest[0],sameIdentity=actual==expected))
 except FileNotFoundError: result.append(dict(pid=pid,expectedStarttime=expected,exists=False))
print(json.dumps(result))
"""
raw = subprocess.run(
    S.command(code),
    input=json.dumps(list(linux)),
    capture_output=True,
    text=True,
    timeout=10,
)
assert raw.returncode == 0, raw.stderr
linuxfacts = json.loads(raw.stdout)
result = dict(
    windows=winfacts,
    linux=linuxfacts,
    unknownWindows=[x for x in winfacts if x.get("exists") is None],
    remainingWindows=[x for x in winfacts if x.get("exists") and x.get("sameIdentity")],
    remainingLinux=[x for x in linuxfacts if x.get("exists") and x.get("sameIdentity")],
)
(base / "cleanup-final.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
assert (
    not result["unknownWindows"]
    and not result["remainingWindows"]
    and not result["remainingLinux"]
), result
print(
    json.dumps(
        dict(
            windowsIdentities=len(windows),
            linuxIdentities=len(linux),
            remainingWindows=[],
            remainingLinux=[],
        )
    )
)
