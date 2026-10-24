"""Read-only exact identity audit over successful and failed round-four probes."""

import json
import subprocess

import windows_probe as W

windows = set()
linux = set()
corrupt = []
directories = set()
for path in W.BASE.glob("*.jsonl"):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            corrupt.append(dict(path=path.name, line=number, error=str(error)))
            continue
        if row.get("event") == "pe-start":
            windows.add((row["pid"], row["creation"]))
        if "carrierCreation" in row:
            windows.add((row["carrierPid"], row["carrierCreation"]))
        if "starttime" in row and "pid" in row:
            linux.add((row["pid"], row["starttime"]))
        if row.get("event") == "windows-scope-joined":
            windows.update(
                (item["pid"], item["creation"]) for item in row["facts"]["held"]
            )
for path in W.BASE.glob("*-summary.json"):
    summary = json.loads(path.read_text(encoding="utf-8"))
    windows.update(
        (item["pid"], item["creation"])
        for scope in summary.get("cleanupWindows", [])
        for item in scope["held"]
    )
for path in W.BASE.glob("*.manager-config.json"):
    config = json.loads(path.read_text(encoding="utf-8"))
    directories.update(scope["directory"] for scope in config["scopes"])
assert {item["path"] for item in corrupt} <= {"normal-pty-0f801224.manager.jsonl"}
windows_facts = [W.J.own_fact(pid, created) for pid, created in sorted(windows)]
remaining_windows = [
    row for row in windows_facts if row["exists"] is True and row.get("sameIdentity")
]
unknown_windows = [row for row in windows_facts if row["exists"] is None]
code = """import json,os,select,sys
from pathlib import Path
facts=[]
for pid,expected in json.loads(sys.argv[1]):
 p=Path('/proc')/str(pid)/'stat'; actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 row=dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected,alive=False)
 if actual==expected:
  fd=os.pidfd_open(pid)
  assert p.read_text().rsplit(')',1)[1].split()[19]==expected
  row['pidfdSignalled']=bool(select.select([fd],[],[],0)[0]); row['alive']=not row['pidfdSignalled']; os.close(fd)
 facts.append(row)
directories=json.loads(sys.argv[2])
assert all(Path(p).parent==Path('/home/plumwsl') and Path(p).name.startswith('.g0t4-') for p in directories)
print(json.dumps(dict(facts=facts,remainingDirectories=[p for p in directories if Path(p).exists()])))
"""
completed = subprocess.run(
    W.guest("-c", code, json.dumps(sorted(linux)), json.dumps(sorted(directories))),
    env=W.ENV,
    capture_output=True,
    text=True,
    check=True,
    timeout=15,
)
linux_facts = json.loads(completed.stdout)
remaining_linux = [
    row for row in linux_facts["facts"] if row["sameIdentity"] and row["alive"]
]
result = dict(
    windowsIdentityCount=len(windows),
    linuxIdentityCount=len(linux),
    privateDirectoryCount=len(directories),
    remainingWindows=remaining_windows,
    unknownWindows=unknown_windows,
    remainingLinux=remaining_linux,
    remainingDirectories=linux_facts["remainingDirectories"],
    retainedCorruptFixtureLines=corrupt,
    windowsFacts=windows_facts,
    linuxFacts=linux_facts["facts"],
)
(W.BASE / "cleanup-final.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
assert (
    not remaining_windows
    and not unknown_windows
    and not remaining_linux
    and not result["remainingDirectories"]
)
print(
    json.dumps(
        {
            key: value
            for key, value in result.items()
            if key not in {"windowsFacts", "linuxFacts", "retainedCorruptFixtureLines"}
        }
    )
)
