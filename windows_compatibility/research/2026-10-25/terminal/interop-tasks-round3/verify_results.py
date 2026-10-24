"""Assert the measured contracts without changing production assertions."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load(label):
    return json.loads((BASE / (label + "-matrix.json")).read_text(encoding="utf-8"))


def event(case, name):
    return next(row for row in case["ownerObservation"] if row["event"] == name)


def verify(baseline_label, patched_label, ab_label):
    baseline, patched, ab = map(load, (baseline_label, patched_label, ab_label))
    assert len(baseline) == len(patched) == 6 and len(ab) == 2
    for case in baseline + patched + ab:
        assert case["completedObservation"] and "failure" not in case
        assert case["cleanupAccounting"]["ActiveProcesses"] == 0
        assert case["cleanupMembers"] == []
        assert case["relayExactPidfdCleanup"]["code"] == 0
        assert case["privateSocketCleanup"]["code"] == 0
        assert all(row["waitResult"] == 0 for row in case["heldWaits"])

    for case in baseline:
        if case["scenario"] in ("normal", "cancel"):
            observed = event(case, case["scenario"] + "-owner-result")
            assert observed["completion"] and observed["liveCount"] == 0
            assert observed["physicalState"] == "PHYSICALLY_JOINED"
            child = next(
                row
                for row in case["exactPeAtOwnerObservation"]
                if row["role"] == "child"
            )
            assert child["fact"]["exists"] and child["fact"]["sameIdentity"]
        if case["scenario"] == "normal":
            assert [
                row["status"] for row in event(case, "eight-slot-admission")["results"]
            ] == ["running"] * 8

    for case in patched:
        assert case["brokerJoinedHandles"]
        assert all(row["waitResult"] == 0 for row in case["brokerJoinedHandles"])
        assert case["brokerTerminatedJob"] == (case["scenario"] == "cancel")
        observed = event(
            case,
            case["scenario"] + "-owner-result"
            if case["scenario"] != "input"
            else "input-eof-result",
        )
        assert observed["completion"] and observed["liveCount"] == 0
        assert all(
            row["fact"]["exists"] is False for row in case["exactPeAtOwnerObservation"]
        )
        if case["scenario"] == "normal":
            tail = event(case, "during-windows-tail")
            assert tail["physicalState"] == "TERMINALIZING" and not tail["completion"]
            assert (
                tail["rootReturncode"] == 0
                and not tail["readerAlive"]
                and tail["watcherAlive"]
            )
            assert tail["liveCount"] == 1
            assert [
                row["status"] for row in event(case, "eight-slot-admission")["results"]
            ] == ["running"] * 7 + ["blocked"]
            assert any(
                row["event"] == "pe-finished" and row.get("role") == "child"
                for row in case["peEvents"]
            )

    for case in baseline + patched:
        if case["scenario"] == "input":
            final = event(case, "input-eof-result")["result"]
            assert (
                final["status"] == "success"
                and "ACK:héllo" in final["output"]
                and "EOF" in final["output"]
            )

    a, b = ab
    a_start = event(a, "started")
    b_start = event(b, "started-in-shared-owner")
    assert a_start["managerPid"] == b_start["managerPid"]
    assert a_start["registryOwner"] == b_start["registryOwner"]
    peer = event(a, "peer-after-A-cancel")
    assert not peer["completion"] and peer["liveCount"] == 1
    b_child = next(
        row
        for row in b["peEvents"]
        if row["event"] == "pe-start" and row["role"] == "child"
    )
    assert any(
        row["pid"] == b_child["pid"]
        and row["creation"] == b_child["creation"]
        and row["waitResult"] == 258
        for row in peer["windows"]["held"]
    )
    assert a["brokerTerminatedJob"] and not b["brokerTerminatedJob"]
    assert event(b, "normal-owner-result")["result"]["status"] == "success"
    result = dict(
        baseline=baseline_label,
        patched=patched_label,
        sharedOwnerAB=ab_label,
        checkedCases=14,
        allMeasuredAssertionsPassed=True,
    )
    (BASE / "verified-results.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    import sys

    verify(*sys.argv[1:])
