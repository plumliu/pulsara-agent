"""Read-only identity audit of this round's exact recorded process instances."""

import json
from pathlib import Path  # noqa: F401 -- preserve executed research snapshot
import race_probe as R

windows = set()
linux = set()
directories = set()


def collect(value):
    if isinstance(value, dict):
        if isinstance(value.get("pid"), int) and isinstance(value.get("creation"), int):
            windows.add((value["pid"], value["creation"]))
        if "carrierPid" in value and "carrierCreation" in value:
            windows.add((value["carrierPid"], value["carrierCreation"]))
        if isinstance(value.get("pid"), int) and "starttime" in value:
            linux.add((value["pid"], str(value["starttime"])))
        if isinstance(value.get("directory"), str) and value["directory"].startswith(
            R.GUEST_ROOT + "/"
        ):
            directories.add(value["directory"])
        for item in value.values():
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)


for path in R.BASE.iterdir():
    if path.name.startswith("cleanup-"):
        continue
    if path.suffix == ".json":
        collect(json.loads(path.read_text(encoding="utf-8")))
    elif path.suffix in (".jsonl", ".linux"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                collect(json.loads(line))
windows_facts = []
for pid, created in sorted(windows):
    fact = R.J.own_fact(pid, created)
    fact["expectedCreation"] = created
    windows_facts.append(fact)
linux_facts = R.guest(
    """import json,os,sys
facts=[]
for pid,expected in json.loads(sys.argv[1]):
 path='/proc/'+str(pid)+'/stat'
 try:
  text=open(path).read(); tail=text[text.rindex(')')+2:].split(); state=tail[0]; actual=tail[19]
 except FileNotFoundError:
  facts.append(dict(pid=pid,expectedStarttime=expected,exists=False))
 else:
  facts.append(dict(pid=pid,expectedStarttime=expected,starttime=actual,state=state,sameIdentity=actual==expected,exists=True))
print(json.dumps(facts))
""",
    json.dumps(sorted(linux)),
)
path_facts = R.guest(
    "import json,os,sys; print(json.dumps([dict(directory=p,exists=os.path.exists(p)) for p in json.loads(sys.argv[1])]))",
    json.dumps(sorted(directories)),
)
result = dict(
    windows=windows_facts,
    linux=linux_facts,
    privatePaths=path_facts,
    remainingWindows=[
        fact
        for fact in windows_facts
        if fact.get("exists") and fact.get("sameIdentity")
    ],
    unknownWindows=[fact for fact in windows_facts if fact.get("exists") is None],
    remainingLinux=[
        fact
        for fact in linux_facts
        if fact.get("sameIdentity") and fact.get("state") != "Z"
    ],
    remainingPaths=[fact for fact in path_facts if fact["exists"]],
)
path = R.BASE / "cleanup-final.json"
path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            windowsIdentities=len(windows),
            linuxIdentities=len(linux),
            privateDirectories=len(directories),
            **{
                key: result[key]
                for key in (
                    "remainingWindows",
                    "unknownWindows",
                    "remainingLinux",
                    "remainingPaths",
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
        "remainingPaths",
    )
)
