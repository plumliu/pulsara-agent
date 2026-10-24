"""Read-only final assertions for the one untouched crash recovery chain."""

import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "r6_windows", BASE.parent / "round6/windows_probe.py"
)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
case = json.loads((BASE / "final-result.json").read_text(encoding="utf-8"))
assert case["observed"] and "failure" not in case
before, unmodified, recovered = (
    case["beforeTerminate"],
    case["untouchedAfterExplicitRestart"],
    case["recovered"],
)
assert before["pidfileHex"] == unmodified["pidfileHex"]
assert before["pgDataInode"] == unmodified["pgDataInode"] == recovered["pgDataInode"]
assert "in production" in unmodified["control"]["stdout"]
assert before["canonicalRow"] == recovered["canonicalRow"]
assert before["systemIdentifier"] == recovered["systemIdentifier"]
assert case["canonicalCreate"]["status"] == 201
assert case["canonicalReadAfter"]["status"] == 200
assert case["heldCarrier"]["waitAfterTerminate"] == 0
assert "automatic recovery in progress" in case["postgresRecoveryLog"]
first, second = case["phases"]
assert (
    first["hostResult"]["remoteOutcome"] == "unknown"
    and first["hostResult"]["pgStopCalls"] == 0
)
assert second["hostResult"]["pgStopCalls"] == 1 and second["hostResult"]["appExit"] == 0
events = second["hostRows"]


def find(event):
    return [row for row in events if row.get("event") == event]


waited = find("original-wait-before-pg-start-returned")[0]
pg_calls = [
    row
    for row in events
    if row.get("event") == "original-pg-client"
    and row["row"]["event"] == "carrier-spawn"
]
assert pg_calls and waited["time"] <= pg_calls[0]["time"]
assert not any(
    row["row"]["method"] in ("run:initdb", "removeDataDirectory") for row in pg_calls
)
control = find("actual-kernel-control-write")
assert any(json.loads(row["text"]).get("initialize") is False for row in control)
closed = next(
    row
    for row in second["kernelRows"]
    if row["event"] == "original-application-close-returned"
)
carrier = find("actual-wsl-carrier-close")[0]
stop = find("before-original-pg-stop")[0]
assert closed["time"] * 1000 <= carrier["time"] <= stop["time"]
assert carrier["code"] == 0 and carrier["signal"] is None
assert stop["state"]["lock"]["acquired"] and not stop["state"]["kernel"]["alive"]
assert (
    not case["cleanup"]["homeExists"]
    and not case["cleanup"]["forcedExactFixtureTeardown"]
)
pg_cleanup = case["cleanup"]["pgCleanup"]["before"]
assert (
    pg_cleanup["kernel"]["acquired"]
    and pg_cleanup["pg"]["acquired"]
    and not pg_cleanup["processes"]
)
retired = json.loads((BASE / "distro-cleanup.json").read_text(encoding="utf-8"))
assert retired["ownRegistrationAbsent"] and retired["ownFolderAbsent"]
management = [
    json.loads(line)
    for line in (BASE / "distro-lifecycle.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
commands = [row["args"] for row in management if row["event"] == "management-command"]
assert all(args[0] != "--shutdown" for args in commands)
assert all(
    args[1] == case["target"]
    for args in commands
    if args[0] in ("--terminate", "--unregister", "--import")
)
host_facts = [
    P.W.J.own_fact(phase["hostPid"], phase["hostCreation"]) for phase in case["phases"]
]
assert all(phase["hostExit"] == 0 for phase in case["phases"])
assert not any(row["exists"] is True and row.get("sameIdentity") for row in host_facts)
assert not any(row["exists"] is None for row in host_facts)
facts = dict(
    passed=True,
    actualRecoveryChainCount=1,
    hostFacts=host_facts,
    remainingExactHosts=[],
    currentHostPidUnobservable=[],
    homeRemovedBeforeUnregister=True,
    ownRegistrationAndFolderAbsent=True,
    originalRegistryAndDefaultUnchanged=True,
    sharedUbuntuStateAtCleanup=retired["listing"],
    noSharedTerminateShutdownUnregisterInRecordedCommands=True,
)
(BASE / "verification-cleanup.json").write_text(
    json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(facts))
