"""Select one review matrix and assert measured ordering, without rerunning tasks."""

from collections import Counter
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def event(case, name):
    return next(row for row in case["managerRows"] if row["event"] == name)


selected = load("final-matrix.json")["cases"]
assert len(selected) == 16

for case in selected:
    assert case["completedObservation"] and "failure" not in case
    assert case["managerProcess"]["exit"] == 0
    preparing = [
        row
        for row in case["managerRows"]
        if row["event"] == "preparing-under-real-reservation"
    ]
    assert preparing and all(
        sum(row["launching"].values()) >= 1 and row["decisionState"] == "PREPARING"
        for row in preparing
    )
    installed = [
        row
        for row in case["managerRows"]
        if row["event"] == "scope-installed-before-thread-start"
    ]
    assert all(
        not row["readerStarted"] and not row["watcherStarted"] for row in installed
    )
    released = event(case, "original-owner-release-and-join-returned")
    assert released["launching"] == {} and released["decisionCount"] == 0
    assert all(
        not row["groupAlive"]
        and not row["readerAlive"]
        and not row["watcherAlive"]
        and row["completion"]
        for row in released["states"]
    )
    assert max(Counter(released["completionPublications"]).values(), default=0) <= 1
    for scope in case["scopes"]:
        seals = [row for row in scope["relayRows"] if row["event"] == "relay-sealed"]
        assert seals and all(
            row["taskCount"] == row["writerCount"] == 0 for row in seals
        )
        assert not any(
            row["event"] == "bytes-forwarded" and row["time"] > seals[0]["time"]
            for row in scope["relayRows"]
        )
        joined = next(
            row
            for row in scope["windowsEvents"]
            if row["event"] == "windows-scope-joined"
        )
        assert (
            joined["facts"]["accounting"]["ActiveProcesses"] == 0
            and joined["facts"]["members"] == []
        )
        assert all(row["waitResult"] == 0 for row in joined["facts"]["held"])
        stops = [
            row
            for row in scope["windowsEvents"]
            if row["event"] in {"before-job-cancel", "before-normal-anchor-eof"}
        ]
        assert stops and seals[0]["time"] <= stops[0]["time"] <= joined["time"]
        retired = next(
            row
            for row in scope["relayRows"]
            if row["event"] == "retire-authorized-by-owner"
        )
        assert retired["time"] >= released["time"]
        if retired.get("socketExists"):
            assert (
                next(
                    row for row in scope["relayRows"] if row["event"] == "relay-retired"
                )["directoryExists"]
                is False
            )
        verified = [
            row
            for row in scope["windowsEvents"]
            if row["event"] == "host-verified-transferred-fd-job-binding"
        ]
        admissions = [
            row
            for row in case["managerRows"]
            if row["event"] == "scope-operation-admitted"
            and row["scope"] == scope["config"]["name"]
        ]
        if admissions:
            bound = next(
                row for row in scope["relayRows"] if row["event"] == "binding-complete"
            )
            assert (
                len(verified) == 1
                and bound["time"] <= verified[0]["time"] <= admissions[0]["time"]
            )
            assert all(
                row["time"] >= admissions[0]["time"] and row["inJob"]
                for row in scope["peRows"]
                if row["event"] == "pe-start"
            )
        else:
            assert not scope["peRows"] and seals[-1]["operationForwardedBytes"] == 0
    if case["gate"] == "half-forwarded":
        assert case["scopes"][0]["relayRows"][-2:]  # retained raw gate and retirement
        assert (
            next(
                row
                for row in case["scopes"][0]["relayRows"]
                if row["event"] == "relay-sealed"
            )["operationForwardedBytes"]
            == 1
        )
    if case["fault"]:
        fault = event(case, "actual-thread-start-injected-failure")
        extra = event(case, "rollback-or-cancel-extra-scope-joined")
        assert fault["time"] <= extra["time"] <= released["time"]
        assert not extra["completionBeforeOriginalJoin"]
        assert not extra["watcherStarted"]
        assert extra["readerStarted"] == (case["fault"] == "watcher-start")
    if case["scenario"] == "input":
        final = event(case, "input-final")
        assert (
            final["result"]["status"] == "success"
            and "ACK:héllo" in final["result"]["output"]
            and "EOF" in final["result"]["output"]
        )

normal = next(case for case in selected if case["label"] == "normal-pty-b1134615")
poll = event(normal, "fresh-poll-during-windows-tail")
assert (
    poll["result"]["status"] == "running"
    and not poll["completion"]
    and poll["liveCount"] == 1
)
assert (
    poll["rootReturncode"] == 0 and not poll["groupAlive"] and not poll["readerAlive"]
)
assert event(normal, "eight-slot-results")["statuses"] == ["running"] * 7 + ["blocked"]
assert event(normal, "normal-final")["completion"]
assert not any(
    row["event"] == "before-job-cancel" for row in normal["scopes"][0]["windowsEvents"]
)
assert event(normal, "relay-ack-remains-stable")["mode"] == "normal"
assert event(normal, "retained-socket-late-caller-rejected")["socketStillExists"]

tail = next(case for case in selected if case["scenario"] == "tail-cancel")
poll = event(tail, "fresh-poll-before-tail-cancel")
assert (
    poll["result"]["status"] == "running"
    and poll["rootReturncode"] == 0
    and not poll["groupAlive"]
)
killed = event(tail, "real-kill-returned")
assert (
    killed["result"]["status"] == "killed"
    and killed["completion"]
    and killed["liveCount"] == 0
)
child = next(
    row
    for row in tail["scopes"][0]["peRows"]
    if row["event"] == "pe-start" and row["role"] == "child"
)
before = next(
    row
    for row in tail["scopes"][0]["windowsEvents"]
    if row["event"] == "before-job-cancel"
)
assert any(
    row["pid"] == child["pid"]
    and row["creation"] == child["creation"]
    and row["waitResult"] == 258
    for row in before["facts"]["held"]
)

ab = next(case for case in selected if case["scenario"] == "ab")
peer = event(ab, "shared-B-after-A-cancel")
assert peer["liveCount"] == 1 and not peer["completion"]
facts = event(ab, "shared-B-exact-windows-after-A-cancel")["facts"]
assert any(row["waitResult"] == 258 for row in facts["held"])
assert event(ab, "normal-final")["result"]["status"] == "success"

matrix = dict(
    gitHead="adb3e1dec382b5531eface90b76d4968ec21c0b8",
    qualification="selected observed cases across explicitly different probe revisions; not a full rerun of final source",
    stages=dict(Counter(case["implementationStage"] for case in selected)),
    cases=selected,
)
(BASE / "final-matrix.json").write_text(
    json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
)
verification = dict(
    passed=True,
    selectedCaseCount=len(selected),
    stages=matrix["stages"],
    startupCases=4,
    upstreamCases=2,
    forwardCases=3,
    threadStartFaults=2,
    inputCases=2,
    sharedOwnerAB=1,
    finalNormalAndTailStatusCases=2,
)
(BASE / "verification.json").write_text(
    json.dumps(verification, indent=2), encoding="utf-8"
)
print(json.dumps(verification))
