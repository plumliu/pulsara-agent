"""Exact cleanup of the retained receiver-loss fault; never shared-distro cleanup."""

import json
import subprocess

import windows_probe as W

label = "watcher-start-8a547091"
summary = json.loads((W.BASE / (label + "-summary.json")).read_text())
windows = [
    W.J.own_fact(row["pid"], row["creation"])
    for row in summary["cleanupWindows"][0]["held"]
]
assert all(
    row["exists"] is False or row.get("sameIdentity") is False for row in windows
)
identities = [
    (row["pid"], row["starttime"])
    for row in W.J.rows(W.BASE / (label + ".manager.jsonl"))
    if row["event"] == "manager-started"
]
identities += list(
    {
        (row["pid"], row["starttime"])
        for row in W.J.rows(W.BASE / (label + "-A.relay.jsonl"))
    }
)
code = """import json,os,select,signal,sys,time
from pathlib import Path
label=sys.argv[1]; identities=json.loads(sys.argv[2]); exact=[]
for pid,expected in identities:
 p=Path('/proc')/str(pid)/'stat'
 actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 exact.append(dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected))
assert not any(row['sameIdentity'] for row in exact),exact
matches=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit() or int(p.name)==os.getpid(): continue
 try:
  command=(p/'cmdline').read_bytes()
  if label.encode() not in command: continue
  fd=os.pidfd_open(int(p.name)); current=(p/'stat').read_text().rsplit(')',1)[1].split()[19]
  assert label.encode() in (p/'cmdline').read_bytes()
  signal.pidfd_send_signal(fd,signal.SIGKILL)
  exited=bool(select.select([fd],[],[],5)[0]); os.close(fd)
  assert exited
  matches.append(dict(pid=int(p.name),starttime=current,pidfdSignalled=True))
 except (FileNotFoundError,ProcessLookupError): pass
d=Path('/home/plumwsl/.g0t4-watcher-start-8a547091-A')
assert d.parent==Path('/home/plumwsl') and d.name=='.g0t4-'+label+'-A'
removed=[]
if d.exists():
 assert {p.name for p in d.iterdir()} <= {'control','interop'}
 for name in ['interop','control']:
  p=d/name
  if os.path.lexists(p): p.unlink(); removed.append(str(p))
 d.rmdir()
print(json.dumps(dict(exactIdentities=exact,remainingMatchingProcesses=[],exactPidfdCleanup=matches,removed=removed,directoryExists=d.exists())))
"""
completed = subprocess.run(
    W.guest("-c", code, label, json.dumps(identities)),
    capture_output=True,
    text=True,
    check=True,
    env=W.ENV,
    timeout=15,
)
result = dict(windows=windows, linux=json.loads(completed.stdout))
(W.BASE / "failed-watcher-cleanup.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
print(json.dumps(result))
