"""Verify actual owner fields and physical observations from preserved runs."""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent


def load(name):
    result = json.loads((OUT / name).read_text())
    assert result["failures"] == [], name
    for case in result["cases"]:
        assert case["cleanup"]["home_removed"]
        assert all(wait["wait"] == 0 for wait in case["carrier_waits"])
        assert case["events"][0]["node"] == "v22.23.3"
    return result["cases"]


normal = load("normal.results.json")[0]
reset = next(
    event for event in normal["events"] if event["event"] == "owner-reset-running"
)
assert reset["before"]["status"]["exit"] == 0
assert reset["reset"]["status"]["exit"] == 4
assert not reset["reset"]["data_exists"] and reset["reset"]["socket_exists"]
assert reset["reset"]["outside_sentinel"] == "outside-sentinel"
assert reset["reset"]["pg"]["inode"] == reset["before"]["pg"]["inode"]
assert reset["owner"]["startedHere"] is False
assert any(
    event["event"] == "owner-reused" and event["startedHere"] is False
    for event in normal["events"]
)

protocol = load("protocol.results.json")
for case in protocol:
    owner = next(event for event in case["events"] if event["event"] == "owner-result")
    if case["scenario"] in ("before-ready-eof", "ready-eof", "ready-cancel"):
        attempt = next(
            event
            for event in case["events"]
            if event["event"] == "carrier-spawn"
            and event["method"] == "run:pg_ctl:start"
        )
        events = [event for event in case["events"] if event.get("id") == attempt["id"]]
        assert not any(event["event"] == "operation-written" for event in events)
        assert not any(event["event"] == "stdin-error" for event in events)
        assert any(
            event["event"] == "helper-frame"
            and event["frame"]["type"] == "no-operation"
            for event in events
        )
        closed = next(event for event in events if event["event"] == "carrier-close")
        assert closed["code"] == 0 and not closed["sent"] and closed["noOperation"]
        assert closed["time"] <= owner["time"]
        assert owner["failure"]["code"] == "PG_NOT_EXECUTED"
        assert owner["owner"]["startedHere"] is False
        assert (
            owner["state"]["status"]["exit"] == 3 and owner["state"]["pg"]["acquired"]
        )
    elif case["scenario"] == "operation-eof":
        assert owner["failure"] is None and owner["owner"]["startedHere"]
    else:
        assert owner["failure"]["code"] == "PG_OUTCOME_UNKNOWN"
        assert owner["owner"]["startedHere"] and owner["mainQuitWouldStop"]
        if case["scenario"] == "unterminated-result":
            assert any(
                event["event"] == "partial-final-frame" for event in case["events"]
            )

deaths = load("death.results.json")
for case in deaths:
    assert case["after_loss_query"]["exit"] == 0
    if case["scenario"] == "node-death":
        state = case["after_node_death"]
    else:
        state = next(
            event["state"]
            for event in case["events"]
            if event["event"] in ("after-helper-death", "carrier-terminated")
        )
    assert state["kernel"]["acquired"] and not state["pg"]["acquired"]
    assert state["status"]["exit"] == 3 and state["data_exists"]

foreign = load("foreign-baseline.results.json")[0]
baseline = next(
    event for event in foreign["events"] if event["event"] == "owner-result"
)
assert baseline["failure"]["code"] == "PG_NOT_EXECUTED"
assert baseline["owner"]["startedHere"] and baseline["mainQuitWouldStop"]
candidate = load("candidate.results.json")
for case in candidate:
    result = next(event for event in case["events"] if event["event"] == "owner-result")
    assert not result["owner"]["startedHere"] and not result["mainQuitWouldStop"]
    assert result["failure"]["postgresExecution"] in ("not-submitted", "unknown")
    assert result["state"]["status"]["exit"] == 0 and result["state"]["data_exists"]
    if case["scenario"] == "foreign-before-readback":
        starts = [
            event
            for event in case["events"]
            if event["event"] == "carrier-spawn"
            and event["method"] == "run:pg_ctl:start"
        ]
        assert len(starts) == 2
        assert not any(
            event["event"] == "operation-written" and event.get("id") == starts[0]["id"]
            for event in case["events"]
        )
        assert any(
            event["event"] == "operation-written" and event.get("id") == starts[1]["id"]
            for event in case["events"]
        )

assert json.loads((OUT / "transport-spawn-error.json").read_text())["passed"]
summary = {
    "passed": True,
    "normalCases": 1,
    "protocolCases": len(protocol),
    "deathCases": len(deaths),
    "foreignBaselineCases": 1,
    "typedCandidateCases": len(candidate),
    "finalPhysicalCases": 13,
    "preAdmissionOperationWritten": 0,
    "spawnErrorSelfCheck": True,
}
(OUT / "verified-results.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
