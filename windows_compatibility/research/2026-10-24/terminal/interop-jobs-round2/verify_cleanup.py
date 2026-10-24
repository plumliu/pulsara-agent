"""Observe only our recorded exact identities; do not terminate any process."""

import json
import os
from pathlib import Path
import subprocess
import sys  # noqa: F401 -- preserve the executed research snapshot
from job_probe import own_fact, rows

base = Path(__file__).resolve().parent
windows = {}
linux = {}
for path in base.glob("anchor-*.ready.json"):
    x = json.loads(path.read_text())
    linux[(x["pid"], x["starttime"])] = path.name
for path in base.glob("*.jsonl"):
    if path.name.endswith(".linux.jsonl"):
        for x in rows(path):
            if "starttime" in x:
                linux[(x["pid"], x["starttime"])] = path.name
        continue
    for x in rows(path):
        if x["event"] == "pe-start":
            windows[(x["pid"], x["creation"])] = path.name
        if x["event"] in ("carrier-assigned", "anchor-before-resume"):
            windows[(x["carrierPid"], x["carrierCreation"])] = path.name
            windows[(x["pid"], x["ownerCreation"])] = path.name
        for key in ("jobMembers", "members"):
            for m in x.get(key, []):
                if "creation" in m:
                    windows[(m["pid"], m["creation"])] = path.name
facts = [
    dict(source=source, expectedCreation=creation, **own_fact(pid, creation))
    for (pid, creation), source in windows.items()
]
guest_code = """import json,os,sys
out=[]
for pid,start,source in json.loads(sys.argv[1]):
 p='/proc/'+str(pid); d=dict(pid=pid,expectedStarttime=start,source=source,exists=os.path.exists(p))
 if d['exists']:
  stat=open(p+'/stat').read().rsplit(')',1)[1].split(); d.update(state=stat[0],starttime=stat[19],sameIdentity=stat[19]==start,cmdline=open(p+'/cmdline','rb').read().decode(errors='replace').replace('\\0',' '))
 out.append(d)
print(json.dumps(dict(uid=os.getuid(),kernel=os.uname().release,linux=out)))
"""
env = {k: v for k, v in os.environ.items() if k.upper() != "WSLENV"}
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
    guest_code,
    json.dumps([(pid, start, source) for (pid, start), source in linux.items()]),
]
guest = json.loads(subprocess.check_output(args, env=env, text=True))
result = dict(windows=facts, **guest)
(base / "cleanup-final.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
remaining_windows = [
    x for x in facts if x["exists"] is None or (x["exists"] and x["sameIdentity"])
]
remaining_linux = [x for x in guest["linux"] if x["exists"] and x["sameIdentity"]]
print(
    json.dumps(
        dict(
            windowsCount=len(facts),
            linuxCount=len(linux),
            remainingWindows=remaining_windows,
            remainingLinux=remaining_linux,
        ),
        ensure_ascii=False,
        indent=2,
    )
)
assert not remaining_windows and not remaining_linux
