"""Read-only exact audit; no shared-distro operation and no PID-only cleanup."""

import json
import subprocess

import windows_probe as P

windows = set()
linux = set()
directories = set()
for path in P.BASE.glob("*.jsonl"):
    for row in P.W.J.rows(path):
        if row.get("event") == "pe-start":
            windows.add((row["pid"], row["creation"]))
        if "carrierCreation" in row:
            windows.add((row["carrierPid"], row["carrierCreation"]))
        if row.get("event") == "windows-scope-joined":
            windows.update(
                (item["pid"], item["creation"]) for item in row["facts"]["held"]
            )
        if "pid" in row and "starttime" in row:
            linux.add((row["pid"], row["starttime"]))
for path in P.BASE.glob("*-summary.json"):
    case = json.loads(path.read_text(encoding="utf-8"))
    windows.update(
        (row["pid"], row["creation"]) for row in case["cleanupWindows"]["held"]
    )
for path in P.BASE.glob("*.scope-config.json"):
    directories.add(json.loads(path.read_text(encoding="utf-8"))["directory"])
facts = [P.W.J.own_fact(pid, created) for pid, created in sorted(windows)]
remaining_windows = [
    row for row in facts if row["exists"] is True and row.get("sameIdentity")
]
unknown_windows = [row for row in facts if row["exists"] is None]
code = """import json,os,select,sys
from pathlib import Path
rows=[]
for pid,expected in json.loads(sys.argv[1]):
 p=Path('/proc')/str(pid)/'stat'; actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 row=dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected,alive=False)
 if actual==expected:
  fd=os.pidfd_open(pid); assert p.read_text().rsplit(')',1)[1].split()[19]==expected
  row['alive']=not bool(select.select([fd],[],[],0)[0]); os.close(fd)
 rows.append(row)
directories=json.loads(sys.argv[2]); assert all(Path(p).parent==Path('/home/plumwsl') and Path(p).name.startswith('.g0m5-') for p in directories)
print(json.dumps(dict(facts=rows,remainingDirectories=[p for p in directories if Path(p).exists()])))
"""
completed = subprocess.run(
    P.W.guest("-c", code, json.dumps(sorted(linux)), json.dumps(sorted(directories))),
    env=P.W.ENV,
    capture_output=True,
    text=True,
    check=True,
    timeout=15,
)
guest = json.loads(completed.stdout)
remaining_linux = [
    row for row in guest["facts"] if row["sameIdentity"] and row["alive"]
]
result = dict(
    windowsIdentityCount=len(windows),
    linuxIdentityCount=len(linux),
    privateDirectoryCount=len(directories),
    remainingWindows=remaining_windows,
    unknownWindows=unknown_windows,
    remainingLinux=remaining_linux,
    remainingDirectories=guest["remainingDirectories"],
    windowsFacts=facts,
    linuxFacts=guest["facts"],
)
(P.BASE / "cleanup-final.json").write_text(
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
            key: result[key]
            for key in (
                "windowsIdentityCount",
                "linuxIdentityCount",
                "privateDirectoryCount",
                "remainingWindows",
                "unknownWindows",
                "remainingLinux",
                "remainingDirectories",
            )
        }
    )
)
