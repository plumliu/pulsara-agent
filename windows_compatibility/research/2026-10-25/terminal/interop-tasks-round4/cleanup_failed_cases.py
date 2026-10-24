"""Exact teardown of named failed research scopes, preserving all failed records."""

import json
import subprocess
import sys

import windows_probe as W

results = []
for label in sys.argv[1:]:
    summary = json.loads((W.BASE / (label + "-summary.json")).read_text())
    windows = [
        W.J.own_fact(row["pid"], row["creation"])
        for scope in summary["cleanupWindows"]
        for row in scope["held"]
    ]
    assert all(
        row["exists"] is False or row.get("sameIdentity") is False for row in windows
    )
    config = json.loads((W.BASE / (label + ".manager-config.json")).read_text())
    manager = W.J.rows(W.BASE / (label + ".manager.jsonl"))
    identities = [
        (row["pid"], row["starttime"])
        for row in manager
        if row["event"] == "manager-started"
    ]
    for scope in config["scopes"]:
        for path in (scope["windows_relay_record"], scope["windows_anchor_record"]):
            identities += [
                (row["pid"], row["starttime"])
                for row in W.J.rows(W.Path(path))
                if "starttime" in row
            ]
    code = """import json,os,select,signal,sys
from pathlib import Path
identities=json.loads(sys.argv[1]); directories=json.loads(sys.argv[2]); audit=[]
for pid,expected in set(map(tuple,identities)):
 p=Path('/proc')/str(pid)/'stat'; actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 row=dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected)
 if actual==expected:
  fd=os.pidfd_open(pid)
  assert p.read_text().rsplit(')',1)[1].split()[19]==expected
  signal.pidfd_send_signal(fd,signal.SIGTERM); row['exactFailedFixtureTeardown']=True
  row['pidfdSignalled']=bool(select.select([fd],[],[],5)[0]); os.close(fd)
  assert row['pidfdSignalled']
 audit.append(row)
removed=[]
for item in directories:
 d=Path(item); assert d.parent==Path('/home/plumwsl') and d.name.startswith('.g0t4-')
 if d.exists():
  assert {p.name for p in d.iterdir()} <= {'control','interop'}
  for name in ['interop','control']:
   p=d/name
   if os.path.lexists(p): p.unlink(); removed.append(str(p))
  d.rmdir()
print(json.dumps(dict(identities=audit,removed=removed,remainingDirectories=[p for p in directories if Path(p).exists()])))
"""
    completed = subprocess.run(
        W.guest(
            "-c",
            code,
            json.dumps(identities),
            json.dumps([scope["directory"] for scope in config["scopes"]]),
        ),
        env=W.ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    results.append(
        dict(label=label, windows=windows, linux=json.loads(completed.stdout))
    )
(W.BASE / ("failed-cases-cleanup-" + W.uuid.uuid4().hex[:8] + ".json")).write_text(
    json.dumps(results, indent=2), encoding="utf-8"
)
print(json.dumps(results))
