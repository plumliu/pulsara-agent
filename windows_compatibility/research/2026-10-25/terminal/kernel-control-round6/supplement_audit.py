"""Audit completed supplement scopes without confusing terminated PID namespaces."""

import json
import subprocess

import windows_probe as P

BASE = P.BASE
matrix = json.loads((BASE / "supplement-matrix.json").read_text(encoding="utf-8"))
cases = matrix["cases"]
assert matrix["sleep"] == "completed"
hosts = [P.W.J.own_fact(case["hostPid"], case["hostCreation"]) for case in cases]
remaining = [row for row in hosts if row["exists"] is True and row.get("sameIdentity")]
unobservable = [row for row in hosts if row["exists"] is None]
shared = [case for case in cases if "name" not in case]
identities = sorted(
    {
        (row["pid"], row["starttime"])
        for case in shared
        for row in case["kernelRows"]
        if row["event"] == "kernel-process-started"
    }
)
homes = sorted(case["home"] for case in shared)
code = """import json,os,select,sys
from pathlib import Path
facts=[]
for pid,expected in json.loads(sys.argv[1]):
 p=Path('/proc')/str(pid)/'stat'; actual=p.read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 row=dict(pid=pid,expectedStarttime=expected,actualStarttime=actual,sameIdentity=actual==expected,alive=False)
 if actual==expected:
  fd=os.pidfd_open(pid); assert p.read_text().rsplit(')',1)[1].split()[19]==expected
  row['alive']=not bool(select.select([fd],[],[],0)[0]); os.close(fd)
 facts.append(row)
homes=json.loads(sys.argv[2]); assert all(Path(p).parent==Path('/home/plumwsl') and Path(p).name.startswith('.g0p5-kernel6-') for p in homes)
print(json.dumps(dict(facts=facts,remainingHomes=[p for p in homes if Path(p).exists()])))
"""
completed = subprocess.run(
    P.W.guest("-c", code, json.dumps(identities), json.dumps(homes)),
    env=P.W.ENV,
    capture_output=True,
    text=True,
    timeout=15,
    check=True,
)
guest = json.loads(completed.stdout)
assert not remaining and not guest["remainingHomes"]
assert all(not row["alive"] for row in guest["facts"])
assert all(
    not case["cleanup"]["homeExists"]
    and not case["cleanup"]["forcedExactFixtureTeardown"]
    for case in cases
)
assert all(
    case["cleanup"]["pgCleanup"]["before"]["kernel"]["acquired"]
    and case["cleanup"]["pgCleanup"]["before"]["pg"]["acquired"]
    and not case["cleanup"]["pgCleanup"]["before"]["processes"]
    for case in cases
)
facts = dict(
    hostCount=len(hosts),
    sharedKernelHomeCount=len(shared),
    retiredDistroCaseCount=len(cases) - len(shared),
    remainingExactHosts=remaining,
    currentHostPidUnobservable=unobservable,
    hostFacts=hosts,
    sharedKernelFacts=guest["facts"],
    remainingSharedHomes=guest["remainingHomes"],
    retiredDistroCleanup=matrix["distroCleanup"],
    allCasePgBothLocksFreeAndNoProcessesBeforeHomeRemoval=True,
)
(BASE / "supplement-cleanup.json").write_text(
    json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
)
assert not unobservable, unobservable
print(
    json.dumps(
        dict(
            hostCount=len(hosts),
            remainingExactHosts=[],
            currentHostPidUnobservable=[],
            remainingSharedHomes=[],
            ownDistroRegistrationAndFolderAbsent=True,
        )
    )
)
