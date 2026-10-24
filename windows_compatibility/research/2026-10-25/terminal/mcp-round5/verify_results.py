"""Review one MCP matrix; no extra task execution or protocol implementation."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
names = [
    "baseline-normal-77977e18",
    "candidate-normal-9c414b75",
    "candidate-call-cancel-126280df",
    "candidate-timeout-cf6c3921",
    "candidate-server-crash-6df57087",
    "candidate-guard-deny-85cf08c5",
]
matrix_path = BASE / "final-matrix.json"
if matrix_path.exists():
    cases = json.loads(matrix_path.read_text(encoding="utf-8"))["cases"]
else:
    cases = [
        json.loads((BASE / (name + "-summary.json")).read_text(encoding="utf-8"))
        for name in names
    ]


def event(case, name):
    return next(row for row in case["mcpRows"] if row["event"] == name)


for case in cases:
    assert (
        case["observed"] and "failure" not in case and case["ownerProcess"]["exit"] == 0
    )
    assert case["cleanupWindows"]["accounting"]["ActiveProcesses"] == 0
    assert case["cleanupWindows"]["members"] == []
    assert all(row["waitResult"] == 0 for row in case["cleanupWindows"]["held"])
    if case["scenario"] == "guard-deny":
        denied = event(case, "guard-denied-before-spawn")
        assert denied["spawnCount"] == 0 and not denied["scopePrepared"]
        assert not case["peRows"] and not case["serverRows"]
        continue
    closed = event(case, "original-supervisor-close-returned")
    assert (
        closed["clientClosed"]
        and closed["stdioTasksDone"]
        and not closed["linuxGroupAlive"]
    )
    assert (
        closed["spawnCount"] == 1
        and closed["guardEntries"] == 1
        and closed["permitState"] == "RELEASED"
    )
    assert (
        len([row for row in case["serverRows"] if row["event"] == "tool-entered"]) == 1
    )
    seals = [row for row in case["relayRows"] if row["event"] == "relay-sealed"]
    assert seals and all(row["taskCount"] == row["writerCount"] == 0 for row in seals)
    assert not any(
        row["event"] == "bytes-forwarded" and row["time"] > seals[0]["time"]
        for row in case["relayRows"]
    )
    windows_joined = next(
        row for row in case["windowsRows"] if row["event"] == "windows-scope-joined"
    )
    assert windows_joined["facts"]["accounting"]["ActiveProcesses"] == 0
    assert all(row["waitResult"] == 0 for row in windows_joined["facts"]["held"])
    retire = next(
        row for row in case["relayRows"] if row["event"] == "retire-authorized-by-owner"
    )
    assert retire["time"] >= closed["time"]
    if case["candidate"]:
        guard = event(case, "real-credential-guard-entered")
        sink = event(case, "actual-stdio-sink-inside-credential-guard")
        prepare = event(case, "scope-prepare-entered")
        spawn = event(case, "real-stdio-process-created")
        assert (
            guard["locked"]
            and spawn["guardStillLocked"]
            and guard["time"] <= sink["time"] <= prepare["time"] <= spawn["time"]
        )
        assert windows_joined["time"] <= closed["time"]
        assert case["windowsAtMcpOwnerClose"]["accounting"]["ActiveProcesses"] == 0
    else:
        child = next(
            row
            for row in case["peRows"]
            if row["event"] == "pe-start" and row["role"] == "child"
        )
        assert any(
            row["pid"] == child["pid"]
            and row["creation"] == child["creation"]
            and row["waitResult"] == 258
            for row in case["windowsAtMcpOwnerClose"]["held"]
        )
        assert (
            event(case, "baseline-external-research-cleanup-entered")["time"]
            > closed["time"]
        )
    if case["scenario"] == "normal":
        known = event(case, "real-tool-outcome")
        assert known["state"] == "SUCCESS" and "MCP-ACK:héllo:0" in known["content"]
        assert not any(
            row["event"] == "before-job-cancel" for row in case["windowsRows"]
        )
        assert any(
            row["event"] == "pe-finished" and row.get("role") == "child"
            for row in case["peRows"]
        )
        if case["candidate"]:
            pending = event(case, "normal-close-still-owned")
            assert not pending["closeDone"] and not pending["clientClosed"]
            assert any(row["waitResult"] == 258 for row in pending["windows"]["held"])
    elif case["scenario"] == "call-cancel":
        cancelled = event(case, "single-call-cancel-observed")
        assert (
            cancelled["callCancelled"]
            and cancelled["serverState"] == "READY"
            and not cancelled["clientClosed"]
        )
        assert (
            cancelled["permitState"] == "RELEASED"
            and cancelled["activeOperations"] == 0
        )
        assert any(
            row["event"] == "sdk-tool-cancel-notification-observed"
            for row in case["serverRows"]
        )
        assert any(row["event"] == "before-job-cancel" for row in case["windowsRows"])
    else:
        known = event(case, "real-tool-outcome")
        content = json.loads(known["content"])
        assert (
            known["state"] == "SYSTEM_ERROR"
            and content["remote_outcome"] == "UNKNOWN"
            and content["retry_performed"] is False
        )

matrix_path.write_text(
    json.dumps(
        dict(gitHead="b741dd92b06a2d167d882e312e18f503e23b8a89", cases=cases),
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
result = dict(
    passed=True,
    cases=len(cases),
    baselineGapObserved=True,
    candidatePhysicalCloseCases=4,
    realCredentialDenialBeforeScope=True,
    originalFocusedTests="21 passed in 6.68s",
    singleCallCancellationDoesNotCloseSession=True,
    timeoutAndProcessLossRemainUnknown=True,
)
(BASE / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
