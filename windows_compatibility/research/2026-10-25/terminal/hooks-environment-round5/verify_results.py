"""Offline assertions over original Hook/environment owners and physical facts."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
names = (
    "baseline-7274d990-results.json",
    "hooks-9cad5e67-results.json",
    "environment-ef9ce490-results.json",
)
guard = next(path for path in BASE.glob("guard-*-results.json"))
sources = [BASE / names[0], BASE / names[1], BASE / names[2], guard]
verified = []


def rows(values, event):
    return [row for row in values if row["event"] == event]


for source in sources:
    for case in json.loads(source.read_text(encoding="utf-8")):
        assert (
            case.get("observed")
            and not case.get("failure")
            and case["guest"]["exit"] == 0
        )
        cfg = case["config"]
        owner = case["ownerRows"]
        if cfg["scenario"] == "guard":
            result = rows(owner, "real-hook-result")[-1]
            assert (
                result["failureCode"] == "API_KEY_VALUE_PRESENT"
                and result["exit"] is None
            )
            assert (
                not case["peRows"]
                and not rows(owner, "hook-workload-admitted")
                and not case["relayRows"]
            )
            verified.append(
                dict(label=case["label"], guardRejectedBeforeScopeAndSpawn=True)
            )
            continue
        verified_binding = rows(
            case["windowsEvents"], "host-verified-transferred-fd-job-binding"
        )[-1]
        assert (
            verified_binding["sameCreation"]
            and verified_binding["member"]
            and verified_binding["waitResult"] == 258
        )
        for admitted in rows(owner, "hook-workload-admitted"):
            assert admitted["credentialEnvAbsent"] and admitted["stdinCredentialAbsent"]
            assert verified_binding["time"] <= admitted["time"]
        if cfg["kind"] == "hook":
            result = (
                rows(owner, "real-hook-result")
                or rows(owner, "real-hook-task-cancelled")
            )[-1]
            assert result["processes"] == 0
            if result["event"] == "real-hook-result":
                assert result["syncSlots"] == 16 and result["backgroundSlots"] == 8
            if cfg["patched"] and cfg["scenario"] in ("normal", "nonzero"):
                assert (
                    result["exit"] == (7 if cfg["scenario"] == "nonzero" else 0)
                    and result["failureCode"] is None
                )
                assert "中文" in result["stdout"] and "✓" in result["stderr"]
                assert result["parsedType"] == (
                    "HandlerFailure"
                    if cfg["scenario"] == "nonzero"
                    else "ValidHandlerContribution"
                )
            if cfg["patched"]:
                for stdin in rows(case["peRows"], "public-stdin-received"):
                    assert stdin["credentialMarker"] and stdin["publicUnicode"]
        else:
            result = rows(owner, "real-environment-result")[-1]
            assert result["attempts"] == 0
            if cfg["patched"]:
                assert (
                    result["snapshotUsed"] is False
                    and result["error"] == "PROBE_TIMEOUT"
                    and result["cache"] == 0
                )
        details = dict(
            label=case["label"],
            kind=cfg["kind"],
            scenario=cfg["scenario"],
            patched=cfg["patched"],
        )
        if not cfg["patched"]:
            observation = (
                rows(owner, "baseline-windows-at-hook-return")
                or rows(owner, "baseline-windows-at-environment-return")
            )[-1]
            child = next(
                row
                for row in case["peRows"]
                if row["event"] == "pe-start" and row["role"] == "child"
            )
            held = next(
                row
                for row in observation["facts"]["held"]
                if row["pid"] == child["pid"] and row["creation"] == child["creation"]
            )
            assert held["waitResult"] == 258
            details["exactWindowsChildAliveAtOriginalOwnerReturn"] = True
        else:
            joined = rows(case["windowsEvents"], "windows-scope-joined")[-1]
            facts = joined["facts"]
            assert (
                facts["accounting"]["ActiveProcesses"] == 0 and facts["members"] == []
            )
            assert all(row["waitResult"] == 0 for row in facts["held"])
            assert joined["time"] <= result["time"]
            if cfg["scenario"] in ("normal", "nonzero"):
                assert not facts["terminateJobCalled"]
            seals = rows(case["relayRows"], "relay-sealed")
            for seal in seals:
                assert seal["taskCount"] == seal["writerCount"] == 0
                assert not [
                    row
                    for row in rows(case["relayRows"], "bytes-forwarded")
                    if row["time"] > seal["time"]
                ]
            for stop in rows(case["windowsEvents"], "before-job-cancel"):
                assert any(
                    seal["mode"] == "cancel" and seal["time"] <= stop["time"]
                    for seal in seals
                )
            details["windowsJoinedBeforeOriginalOwnerReturn"] = True
        retired = rows(case["relayRows"], "relay-retired")[-1]
        assert retired["time"] >= result["time"] and not retired["directoryExists"]
        verified.append(details)
output = dict(passed=True, cases=verified)
(BASE / "verified-results.json").write_text(
    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(dict(passed=True, cases=len(verified))))
