"""Stopped-run checkpoint: read existing results and audit exact owned resources."""

import json
import subprocess

import windows_probe as P

cases = []
for path in sorted(P.BASE.glob("*-summary.json")):
    case = json.loads(path.read_text(encoding="utf-8"))
    case["sourceSummary"] = path.name
    if "hostResult" in case:
        case["hostResult"] = {
            key: value for key, value in case["hostResult"].items() if key != "rows"
        }
    cases.append(case)
hosts = [P.W.J.own_fact(case["hostPid"], case["hostCreation"]) for case in cases]
identities = {
    (row["pid"], row["starttime"])
    for case in cases
    for row in case.get("kernelRows", [])
    if row["event"] == "kernel-process-started"
}
homes = sorted({case["home"] for case in cases})
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
    P.W.guest("-c", code, json.dumps(sorted(identities)), json.dumps(homes)),
    env=P.W.ENV,
    capture_output=True,
    text=True,
    check=True,
    timeout=15,
)
guest = json.loads(completed.stdout)
remaining_hosts = [
    row for row in hosts if row["exists"] is True and row.get("sameIdentity")
]
unknown_hosts = [row for row in hosts if row["exists"] is None]
remaining_kernels = [
    row for row in guest["facts"] if row["sameIdentity"] and row["alive"]
]
assert all(
    case["cleanup"]["homeExists"] is False
    and not case["cleanup"]["forcedExactFixtureTeardown"]
    for case in cases
)
assert all(
    case["cleanup"]["pgCleanup"]["before"]["kernel"]["acquired"]
    and case["cleanup"]["pgCleanup"]["before"]["pg"]["acquired"]
    and not case["cleanup"]["pgCleanup"]["before"]["processes"]
    for case in cases
)
cleanup = dict(
    hostIdentityCount=len(hosts),
    kernelIdentityCount=len(identities),
    homeCount=len(homes),
    remainingExactHosts=remaining_hosts,
    currentHostPidUnobservable=unknown_hosts,
    remainingExactKernels=remaining_kernels,
    remainingHomes=guest["remainingHomes"],
    hostFacts=hosts,
    kernelFacts=guest["facts"],
    allCasePgStoppedAndBothLocksFreeBeforeRemoval=True,
    userStopHonored=True,
)
(P.BASE / "cleanup-checkpoint.json").write_text(
    json.dumps(cleanup, ensure_ascii=False, indent=2), encoding="utf-8"
)
(P.BASE / "checkpoint-matrix.json").write_text(
    json.dumps(
        dict(status="stopped-at-user-request", cases=cases),
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
assert (
    not remaining_hosts
    and not unknown_hosts
    and not remaining_kernels
    and not guest["remainingHomes"]
)
print(
    json.dumps(
        dict(
            cases=len(cases),
            observed=sum(bool(case.get("observed")) for case in cases),
            retainedHarnessFailures=sum(bool(case.get("failure")) for case in cases),
            remainingExactHosts=[],
            currentHostPidUnobservable=[],
            remainingExactKernels=[],
            remainingHomes=[],
        )
    )
)
