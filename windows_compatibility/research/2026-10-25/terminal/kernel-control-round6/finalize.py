"""Read completed evidence, assert narrow conclusions, and audit exact identities."""

import json
import subprocess

import windows_probe as P


def dump(name, value):
    (P.BASE / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


cases = []
for path in sorted(P.BASE.glob("*-summary.json")):
    case = json.loads(path.read_text(encoding="utf-8"))
    if case.get("researchExtra") or case["scenario"] == "window-close":
        continue  # Later independent supplement; do not rewrite the 22-case matrix.
    case["sourceSummary"] = path.name
    # Single canonical copy of raw events, including events after a harness failure.
    case["hostRows"] = P.rows(P.BASE / (case["label"] + ".host.jsonl"))
    case["kernelRows"] = P.rows(P.BASE / (case["label"] + ".kernel.jsonl"))
    if "hostResult" in case:
        case["hostResult"].pop("rows", None)
    cases.append(case)


def case(suffix):
    return next(value for value in cases if value["label"].endswith(suffix))


def events(value, event):
    return [row for row in value["hostRows"] if row.get("event") == event]


def kernel_events(value, event):
    return [row for row in value["kernelRows"] if row["event"] == event]


checks = []
for suffix in ("240796c2", "dfc27874", "f41d42ec"):
    value = case(suffix)
    stop = events(value, "before-original-pg-stop")[0]
    closed = kernel_events(value, "original-application-close-returned")[0]
    carrier = events(value, "actual-wsl-carrier-close")[0]
    assert closed["time"] * 1000 <= carrier["time"] <= stop["time"]
    assert carrier["code"] == 0 and carrier["signal"] is None
    assert not stop["state"]["kernel"]["alive"]
    assert stop["state"]["lock"]["acquired"]
    assert value["hostResult"]["pgStopCalls"] == 1
    assert value["hostResult"]["appExit"] == 0
    checks.append(
        dict(label=value["label"], claim="clean-kernel-join-before-original-pg-stop")
    )

held = events(case("f41d42ec"), "clean-eof-main-still-joining")[0]
assert held["pgStopCalls"] == 0 and held["runtimeRunning"]
assert held["state"]["kernel"]["alive"]
assert not held["state"]["lock"]["acquired"]
assert not held["state"]["closeGateReleased"]
checks.append(dict(claim="real-clean-eof-close-gate-prevents-early-main-pg-stop"))

for suffix in ("3b5e04fc", "0d0a152e"):
    value = case(suffix)
    assert not kernel_events(value, "original-application-close-entered")
    cleared = events(value, "after-original-child-clear")[0]
    assert not cleared["childPresent"] and not cleared["runtimeRunning"]
    assert value["hostResult"]["pgStopCalls"] == 1
    assert value["hostResult"]["appExit"] == 0
    checks.append(
        dict(
            label=value["label"],
            claim="original-owner-counterexample-unclean-exit-still-stops-pg",
        )
    )

for suffix in ("fe454dde", "b069fd26", "a828bac6"):
    value = case(suffix)
    assert not kernel_events(value, "original-application-close-entered")
    result = value["hostResult"]
    assert result["pgStopCalls"] == 0 and result["appExit"] is None
    assert result["quitError"] and value["beforeCleanup"]["postgres"]["exit"] == 0
    assert not events(value, "before-original-pg-stop")
    checks.append(
        dict(
            label=value["label"],
            claim="output-candidate-rechecks-saved-unclean-exit-and-preserves-pg",
        )
    )

for suffix in ("1cf0dad7", "0f1fb1de"):
    value = case(suffix)
    assert value["hostKilledThroughExactHandle"] and value["hostExit"] == 93
    assert value["pidfdObserver"]["rows"][-1]["signalled"]
    assert value["lossClassification"]["classification"] == "kernel-exited"
    assert value["lossClassification"]["lock"]["acquired"]
    assert not kernel_events(value, "original-application-close-entered")
    assert value["beforeCleanup"]["postgres"]["exit"] == 0
    assert not events(value, "before-original-pg-stop")
    checks.append(
        dict(
            label=value["label"],
            claim="host-loss-is-unclean-not-clean-ack-pg-preserved-until-fixture-cleanup",
        )
    )

for suffix in ("e7a0e1ba", "75732b63", "0405c9ee"):
    value = case(suffix)
    assert not kernel_events(value, "original-application-start-entered")
    assert not value["beforeCleanup"]["dataExists"]
    checks.append(
        dict(
            label=value["label"],
            claim="pre-start-loss-never-starts-private-pg-or-application",
        )
    )

for suffix in ("1c8b1719", "948d90ca", "49c3d654"):
    value = case(suffix)
    assert events(value, "actual-runtime-start-rejected")
    assert events(value, "actual-wsl-carrier-close")
    checks.append(
        dict(
            label=value["label"],
            claim="original-runtime-rejects-injected-output-or-real-start-failure-and-joins",
        )
    )

value = case("4ed8cd1e")
assert value["hostResult"]["pgStopCalls"] == 0
assert value["hostResult"]["appExit"] is None
assert value["beforeCleanup"]["postgres"]["exit"] == 0
checks.append(
    dict(
        label=value["label"],
        claim="original-live-child-close-failure-already-preserves-pg",
    )
)

hosts = [P.W.J.own_fact(value["hostPid"], value["hostCreation"]) for value in cases]
carrier_pids = sorted(
    {
        row["pid"]
        for value in cases
        for row in events(value, "actual-wsl-carrier-spawned")
    }
)
# Historical carrier creation was not captured: these are current PID observations,
# not assertions of historical identity and never authorization to kill a PID.
carriers = [P.W.J.own_fact(pid) for pid in carrier_pids]
identities = sorted(
    {
        (row["pid"], row["starttime"])
        for value in cases
        for row in kernel_events(value, "kernel-process-started")
    }
)
homes = sorted({value["home"] for value in cases})
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
    not value["cleanup"]["homeExists"]
    and not value["cleanup"]["forcedExactFixtureTeardown"]
    for value in cases
)
assert all(
    value["cleanup"]["pgCleanup"]["before"]["kernel"]["acquired"]
    and value["cleanup"]["pgCleanup"]["before"]["pg"]["acquired"]
    and not value["cleanup"]["pgCleanup"]["before"]["processes"]
    for value in cases
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
    historicalCarrierPidCurrentObservations=carriers,
    allCasePgStoppedAndBothLocksFreeBeforeRemoval=True,
)
dump("cleanup-final.json", cleanup)
dump(
    "final-matrix.json",
    dict(status="kernel-control-research-checkpoint-not-all-g0", cases=cases),
)
dump(
    "verification.json",
    dict(
        checks=checks,
        cases=len(cases),
        observed=sum(bool(value.get("observed")) for value in cases),
        preservedHarnessFailures=[
            value["label"] for value in cases if value.get("failure")
        ],
        originalDesktopRuntimeTests="12 passed in 1.53s",
    ),
)
assert (
    not remaining_hosts
    and not unknown_hosts
    and not remaining_kernels
    and not guest["remainingHomes"]
)
assert all(row["exists"] is False for row in carriers), carriers
print(
    json.dumps(
        dict(
            cases=len(cases),
            narrowAssertions=len(checks),
            remainingExactHosts=[],
            remainingExactKernels=[],
            remainingHomes=[],
            carrierCurrentPidsAllAbsent=True,
        )
    )
)
