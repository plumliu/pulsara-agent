"""Read-only exact-instance audit; does not signal or delete anything."""

import json
from pathlib import Path  # noqa: F401 -- preserve executed research snapshot
import subprocess
import windows_probe as P

BASE = P.BASE
windows = set()
linux = set()
directories = set()


def collect(value):
    if isinstance(value, dict):
        if isinstance(value.get("pid"), int) and isinstance(value.get("creation"), int):
            windows.add((value["pid"], value["creation"]))
        if isinstance(value.get("pid"), int) and isinstance(
            value.get("starttime"), str
        ):
            linux.add((value["pid"], value["starttime"]))
        if "carrierPid" in value and "carrierCreation" in value:
            windows.add((value["carrierPid"], value["carrierCreation"]))
        for key in ("directory", "env_home"):
            item = value.get(key)
            if isinstance(item, str) and (
                item.startswith("/home/plumwsl/.g0h5-")
                or item.startswith("/home/plumwsl/.g0h5env-")
            ):
                directories.add(item)
        for item in value.values():
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)


for path in BASE.glob("*-summary.json"):
    collect(json.loads(path.read_text(encoding="utf-8")))
facts = [
    dict(expectedCreation=created, **P.J.own_fact(pid, created))
    for pid, created in sorted(windows)
]
code = """import json,os,sys
facts=[]
for pid,expected in json.loads(sys.argv[1]):
 try:
  text=open('/proc/'+str(pid)+'/stat').read(); tail=text.rsplit(')',1)[1].split()
 except FileNotFoundError:
  facts.append(dict(pid=pid,expectedStarttime=expected,exists=False))
 else:
  facts.append(dict(pid=pid,expectedStarttime=expected,starttime=tail[19],state=tail[0],sameIdentity=tail[19]==expected,exists=True))
print(json.dumps(dict(identities=facts,paths=[dict(path=p,exists=os.path.exists(p)) for p in json.loads(sys.argv[2])])))
"""
guest = json.loads(
    subprocess.check_output(
        P.W.guest(
            "-c", code, json.dumps(sorted(linux)), json.dumps(sorted(directories))
        ),
        text=True,
    )
)
result = dict(
    windows=facts,
    linux=guest["identities"],
    directories=guest["paths"],
    remainingWindows=[
        fact for fact in facts if fact.get("sameIdentity") and fact["exists"]
    ],
    unknownWindows=[fact for fact in facts if fact["exists"] is None],
    remainingLinux=[
        fact
        for fact in guest["identities"]
        if fact.get("sameIdentity") and fact["state"] != "Z"
    ],
    remainingDirectories=[fact for fact in guest["paths"] if fact["exists"]],
)
(BASE / "cleanup-final.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            windows=len(windows),
            linux=len(linux),
            directories=len(directories),
            **{
                key: result[key]
                for key in (
                    "remainingWindows",
                    "unknownWindows",
                    "remainingLinux",
                    "remainingDirectories",
                )
            },
        )
    )
)
assert not any(
    result[key]
    for key in (
        "remainingWindows",
        "unknownWindows",
        "remainingLinux",
        "remainingDirectories",
    )
)
