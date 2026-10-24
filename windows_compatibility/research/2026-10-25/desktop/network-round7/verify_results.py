"""Read existing raw results; never drive the manual power gate."""

import json
from datetime import datetime, timezone
from pathlib import Path

base = Path(__file__).resolve().parent
chosen = base / "n7-4e98c96a-54f9-4e9e-88a3-77cd9cfcc7dc-results.json"
r = json.loads(chosen.read_text(encoding="utf-8"))
assert not r.get("fatal") and not r.get("cleanupError")
assert r["carrierExit"] == dict(code=0, signal=None)
assert len(r["tests"]) == 2 and all(t["passed"] for t in r["tests"])
n, t = [v["detail"] for v in r["tests"]]
expiry = (
    datetime.strptime(r["validTo"], "%b %d %H:%M:%S %Y GMT")
    .replace(tzinfo=timezone.utc)
    .timestamp()
    * 1000
)
assert n["before"]["last"]["timeMs"] < expiry < n["after"]["last"]["timeMs"]
assert all(v["time"] > expiry for v in n["newContextChecks"])
assert n["after"]["last"]["pid"] == n["before"]["last"]["pid"] == r["ready"]["pid"]
assert n["after"]["count"] > n["before"]["count"] and n["after"]["opens"] == 1
assert n["newContextChecks"] and all(
    v["same"] and v["allow"] for v in n["newContextChecks"]
)
assert n["guestSpawns"] == 1 and n["clockChanged"] is False
assert t["before"]["stages"] and t["before"]["imports"][0]["bytes"] > 0
assert all(f["mode"] == "0o600" for f in t["before"]["imports"])
assert t["after"]["imports"] == t["after"]["stages"] == []
assert (
    all(s["closed"] for s in t["settled"][:4])
    and t["settled"][4]["state"] == "cancelled"
)
assert max(v["bytes"] for v in r["downloadUpdates"]) > 0
assert all(t["rejected"]) and t["webContentsDestroyed"] and t["oneGuest"]
opened = [v for v in r["guestEvents"] if v["event"] == "network-handler-opened"]
closed = [v for v in r["guestEvents"] if v["event"] == "network-handler-closed"]
assert sorted(v["kind"] for v in opened) == [
    "download",
    "pending",
    "pending",
    "pending",
    "sse",
]
assert {v["token"] for v in opened} == {v["token"] for v in closed}
assert all(v["transportClosing"] for v in closed)
assert [v["event"] for v in r["events"]] == [
    "retire-admission-sealed",
    "webcontents-destroyed",
    "connections-closed",
]
assert (
    sum(v["url"].endswith("/api/connections/owner/import-file") for v in r["requests"])
    == 1
)
assert any(
    v["event"] == "network-private-home-cleaned" and not v["exists"]
    for v in r["guestEvents"]
)
first = json.loads(
    (base / "n7-60330538-65ea-46f2-97e0-af03b829a1bc-results.json").read_text()
)
second = json.loads(
    (base / "n7-c8877c9b-b180-4d6e-9d80-f630c6794ef9-results.json").read_text()
)
assert "AssertionError" in first["fatal"] and "retired requests" in second["fatal"]
assert first.get("cleanupError") and second.get("cleanupError")
manual_file = base / "manual-ready.json"
manual = json.loads(Path(json.loads(manual_file.read_text())["results"]).read_text())
summary = dict(
    passed=True,
    automaticRaw=chosen.name,
    originalFailures=[first["label"], second["label"]],
    manualLabel=manual["label"],
    manualComplete=bool(manual.get("ended")),
    manualTests=[v["name"] for v in manual["tests"]],
    powers=manual["powers"],
)
if manual.get("ended"):
    assert not manual.get("fatal") and not manual.get("cleanupError")
    assert manual["carrierExit"] == dict(code=0, signal=None)
    assert any(
        v["name"] == "actual powerMonitor suspend and resume observed" and v["passed"]
        for v in manual["tests"]
    )
    assert manual["guestSpawns"] == 1
    assert [v["event"] for v in manual["powers"]].count("suspend") >= 1
    assert [v["event"] for v in manual["powers"]].count("resume") >= 1
    resume = next(v["time"] for v in manual["powers"] if v["event"] == "resume")
    suspend = next(v["time"] for v in manual["powers"] if v["event"] == "suspend")
    assert suspend < resume
    recovered = next(
        v["detail"]
        for v in manual["tests"]
        if v["name"] == "actual powerMonitor suspend and resume observed"
    )
    assert recovered["state"]["last"]["pid"] == manual["ready"]["pid"]
    assert recovered["state"]["last"]["timeMs"] > resume
    assert recovered["state"]["opens"] == 2 and recovered["state"]["errors"] == 1
    assert any(
        v["allow"] and v["url"].endswith("/healthz") and v["time"] > resume
        for v in manual["requests"]
    )
    assert (
        sum(
            v["url"].endswith("/api/connections/owner/import-file")
            for v in manual["requests"]
        )
        == 1
    )
    retired = manual["tests"][-1]["detail"]
    assert retired["after"]["stages"] == retired["after"]["imports"] == []
    assert all(retired["rejected"]) and all(v["closed"] for v in retired["settled"][:4])
    assert any(
        v["event"] == "network-private-home-cleaned" and not v["exists"]
        for v in manual["guestEvents"]
    )
    summary["powerEventIntervalMs"] = resume - suspend
    summary["sseReadOnlyReconnect"] = dict(
        opens=recovered["state"]["opens"], errors=recovered["state"]["errors"]
    )
(base / "verification.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(summary, ensure_ascii=False))
