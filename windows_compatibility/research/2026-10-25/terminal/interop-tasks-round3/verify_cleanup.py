"""Read-only exact Windows/Linux identity audit, plus empty fixture removal."""

import importlib.util
import json
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("task_fixture", BASE / "task_probe.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

windows = set()
linux = set()
for path in BASE.glob("*.jsonl"):
    for row in T.J.rows(path):
        if row.get("event") == "pe-start":
            windows.add((row["pid"], row["creation"]))
        if row.get("event") in {"anchor-ready", "relay-ready"}:
            linux.add((row["pid"], row["starttime"]))
for path in BASE.glob("*-summary.json"):
    summary = json.loads(path.read_text(encoding="utf-8"))
    for row in summary.get("heldWaits", []):
        windows.add((row["pid"], row["creation"]))
    for row in summary.get("brokerJoinedHandles", []):
        windows.add((row["pid"], row["creation"]))

windows_facts = [T.J.own_fact(pid, created) for pid, created in sorted(windows)]
code = """import json,os,sys
from pathlib import Path
facts=[]
for pid,expected in json.loads(sys.argv[1]):
    p=Path('/proc')/str(pid)/'stat'
    actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
    facts.append(dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected))
root=Path('/home/plumwsl/workspaces/pulsara_agent/output/windows-g0/interop_tasks_r3/round3')
removed=[]
for kind in ['normal','cancel','input']:
    for io in ['pipe','pty']:
        d=root/('tasks-5fb9ff37-'+kind+'-'+io)
        assert d.parent==root
        if d.exists():
            assert d.is_dir() and not list(d.iterdir())
            d.rmdir()
            removed.append(str(d))
private=Path('/home/plumwsl/.g0tasks')
assert private.stat().st_mode&0o777==0o700
print(json.dumps(dict(facts=facts,removedExactEmptyFixtureDirectories=removed,privateRoot=str(private),privateChildren=[str(p) for p in private.iterdir()])))
"""
completed = subprocess.run(
    T.command("-c", code, json.dumps(sorted(linux))),
    env=T.ENV,
    capture_output=True,
    text=True,
    check=True,
    timeout=15,
)
linux_facts = json.loads(completed.stdout)
remaining_windows = [
    row for row in windows_facts if row.get("sameIdentity") and row["exists"]
]
unknown_windows = [row for row in windows_facts if row["exists"] is None]
remaining_linux = [row for row in linux_facts["facts"] if row["sameIdentity"]]
result = dict(
    windowsIdentityCount=len(windows),
    linuxIdentityCount=len(linux),
    windowsFacts=windows_facts,
    linuxAudit=linux_facts,
    remainingWindows=remaining_windows,
    unknownWindows=unknown_windows,
    remainingLinux=remaining_linux,
)
(BASE / "cleanup-final.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
assert not remaining_windows and not unknown_windows and not remaining_linux
assert linux_facts["privateChildren"] == []
print(
    json.dumps(
        dict(
            windowsIdentityCount=len(windows),
            linuxIdentityCount=len(linux),
            remainingWindows=[],
            unknownWindows=[],
            remainingLinux=[],
            privateChildren=[],
        )
    )
)
