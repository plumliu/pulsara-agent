"""Read-only audit of recorded finite helper identities and private roots."""

import json
import os
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
data = json.loads((BASE / "payload-results.json").read_text(encoding="utf-8"))
post = json.loads((BASE / "post-publication-run-1.log").read_text(encoding="utf-8"))
identities = set()


def collect(value):
    if isinstance(value, dict):
        if isinstance(value.get("pid"), int) and isinstance(
            value.get("starttime"), str
        ):
            identities.add((value["pid"], value["starttime"]))
        for item in value.values():
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)


collect(data)
collect(post)
roots = [data["root"], post["root"]]
code = """import json,os,sys
identities=[]
for pid,expected in json.loads(sys.argv[1]):
 try:
  text=open('/proc/'+str(pid)+'/stat').read(); tail=text.rsplit(')',1)[1].split()
 except FileNotFoundError:
  identities.append(dict(pid=pid,expectedStarttime=expected,exists=False))
 else:
  identities.append(dict(pid=pid,expectedStarttime=expected,starttime=tail[19],state=tail[0],sameIdentity=tail[19]==expected,exists=True))
print(json.dumps(dict(identities=identities,roots=[dict(path=p,exists=os.path.exists(p)) for p in json.loads(sys.argv[2])])))
"""
args = [
    os.environ["SystemRoot"] + r"\System32\wsl.exe",
    "-d",
    "Ubuntu",
    "-u",
    "plumwsl",
    "--cd",
    "/home/plumwsl",
    "--exec",
    "/usr/bin/python3",
    "-c",
    code,
    json.dumps(sorted(identities)),
    json.dumps(roots),
]
result = json.loads(subprocess.check_output(args, text=True))
result["remainingProcesses"] = [
    fact
    for fact in result["identities"]
    if fact.get("sameIdentity") and fact["state"] != "Z"
]
result["remainingRoots"] = [fact for fact in result["roots"] if fact["exists"]]
(BASE / "cleanup-final.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            identities=len(identities),
            roots=len(roots),
            remainingProcesses=result["remainingProcesses"],
            remainingRoots=result["remainingRoots"],
        )
    )
)
assert not result["remainingProcesses"] and not result["remainingRoots"]
