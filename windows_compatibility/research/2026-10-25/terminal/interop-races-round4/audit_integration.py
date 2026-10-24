"""Read-only independent assertions over the actual manager suite evidence."""

import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent


def only(rows, event):
    return [row for row in rows if row["event"] == event]


def check(case):
    assert case.get("completedObservation"), case["label"]
    assert not case.get("failure"), case["label"]
    manager = case["managerRows"]
    release = only(manager, "original-owner-release-and-join-returned")[-1]
    assert release["launching"] == {} and release["decisionCount"] == 0
    for state in release["states"]:
        assert state["completion"] and not any(
            state[name] for name in ("readerAlive", "watcherAlive", "groupAlive")
        )
    for polled in only(manager, "fresh-poll-during-windows-tail") + only(
        manager, "fresh-poll-before-tail-cancel"
    ):
        assert polled["result"]["status"] == "running" and not polled["completion"]
        assert (
            polled["liveCount"] >= 1
            and not polled["groupAlive"]
            and polled["rootReturncode"] == 0
        )
    if case["scenario"] == "tail-cancel":
        killed = only(manager, "real-kill-returned")[-1]
        assert killed["result"]["status"] == "killed" and killed["completion"]
    output = dict(
        label=case["label"],
        gate=case.get("gate"),
        fault=case.get("fault"),
        ownerReleasedWithZeroReservations=True,
        freshPhysicalStatusPolled=bool(
            only(manager, "fresh-poll-during-windows-tail")
            + only(manager, "fresh-poll-before-tail-cancel")
        ),
        scopes=[],
    )
    for scope in case["scopes"]:
        name = scope["config"]["name"]
        relay = scope["relayRows"]
        windows = scope["windowsEvents"]
        scoped = [row for row in manager if row.get("scope") == name]
        seals = only(relay, "relay-sealed")
        assert seals, (case["label"], name)
        for sealed in seals:
            assert sealed["taskCount"] == sealed["writerCount"] == 0
            assert not [
                row
                for row in only(relay, "bytes-forwarded")
                if row["time"] > sealed["time"]
            ], "forward after seal ACK"
        cancels = only(windows, "before-job-cancel")
        cancel_seals = [row for row in seals if row["mode"] == "cancel"]
        if cancels:
            assert cancel_seals and cancel_seals[0]["time"] <= cancels[0]["time"]
        normal_eof = only(windows, "before-normal-anchor-eof")
        if normal_eof:
            assert seals[0]["time"] <= normal_eof[0]["time"]
        joined = only(windows, "windows-scope-joined")[-1]
        facts = joined["facts"]
        assert facts["accounting"]["ActiveProcesses"] == 0 and facts["members"] == []
        assert all(row["waitResult"] == 0 for row in facts["held"])
        if case["scenario"] in ("normal", "input"):
            assert not facts["terminateJobCalled"]
        for admitted in only(scoped, "scope-operation-admitted"):
            binding = only(relay, "binding-complete")[-1]
            verified = only(windows, "host-verified-transferred-fd-job-binding")[-1]
            assert binding["time"] <= verified["time"] <= admitted["time"]
            assert (
                verified["sameCreation"]
                and verified["member"]
                and verified["waitResult"] == 258
            )
        if only(scope["peRows"], "pe-start"):
            admitted = only(scoped, "scope-operation-admitted")[-1]
            assert admitted["time"] <= only(scope["peRows"], "pe-start")[0]["time"]
        retired = only(relay, "relay-retired")[-1]
        assert retired["time"] >= release["time"] and not retired["directoryExists"]
        authorizations = only(relay, "retire-authorized-by-owner")
        if only(relay, "relay-ready"):
            assert all(row["socketExists"] for row in authorizations)
        output["scopes"].append(
            dict(
                name=name,
                sealMode=seals[-1]["mode"],
                accepted=seals[-1]["accepted"],
                operationAccepted=seals[-1]["operationAccepted"],
                operationForwardedBytes=seals[-1]["operationForwardedBytes"],
                bytesStableAfterAck=True,
                sealPrecedesWindowsStop=True,
                windowsExactJoin=True,
                retireAfterOwnerJoin=True,
                modelPeStarts=len(only(scope["peRows"], "pe-start")),
            )
        )
    return output


results = []
for source in sys.argv[1:]:
    path = Path(source)
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document if isinstance(document, list) else [document]
    for case in cases:
        results.append(check(case))
assert results
output = dict(kind="read-only-real-manager-evidence-review", passed=True, cases=results)
(BASE / "integration-review-results.json").write_text(
    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        dict(
            passed=True, cases=len(results), labels=[case["label"] for case in results]
        )
    )
)
