"""Read-only audit of this branch's recorded Unix self-check identities/paths."""

import json
import os
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
identities = set()
directories = set()
roots = set()


def collect(value):
    if isinstance(value, dict):
        if isinstance(value.get("pid"), int) and isinstance(
            value.get("starttime"), str
        ):
            identities.add((value["pid"], value["starttime"]))
        directory = value.get("directory")
        if isinstance(directory, str) and directory.startswith(
            "/home/plumwsl/.g0r4check-"
        ):
            directories.add(directory)
            roots.add(str(Path(directory).parent))
        for item in value.values():
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)


for path in BASE.glob("*-run-*.log"):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("{"):
            collect(json.loads(line))
code = """import json,os,sys
result=[]
for pid,expected in json.loads(sys.argv[1]):
 try:
  text=open('/proc/'+str(pid)+'/stat').read(); tail=text.rsplit(')',1)[1].split()
 except FileNotFoundError:
  result.append(dict(pid=pid,expectedStarttime=expected,exists=False))
 else:
  result.append(dict(pid=pid,expectedStarttime=expected,starttime=tail[19],state=tail[0],sameIdentity=tail[19]==expected,exists=True))
print(json.dumps(dict(identities=result,paths=[dict(path=p,exists=os.path.exists(p)) for p in json.loads(sys.argv[2])])))
"""
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
    "-c",
    code,
    json.dumps(sorted(identities)),
    json.dumps(sorted(directories | roots)),
]
result = json.loads(subprocess.check_output(args, text=True))
result["remainingProcesses"] = [
    fact
    for fact in result["identities"]
    if fact.get("sameIdentity") and fact["state"] != "Z"
]
result["remainingPaths"] = [fact for fact in result["paths"] if fact["exists"]]
(BASE / "cleanup-final.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            identities=len(identities),
            privateDirectories=len(directories),
            roots=len(roots),
            remainingProcesses=result["remainingProcesses"],
            remainingPaths=result["remainingPaths"],
        )
    )
)
assert not result["remainingProcesses"] and not result["remainingPaths"]
