"""Read-only review of actual discovery, finite installation and failure facts."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
discovery = json.loads((BASE / "discovery-results.json").read_text(encoding="utf-8"))
payload = json.loads((BASE / "payload-results.json").read_text(encoding="utf-8"))
post = json.loads((BASE / "post-publication-run-1.log").read_text(encoding="utf-8"))
assert (
    discovery["passed"]
    and discovery["target"]["uid"] == 1000
    and not discovery["windowsIsUserAnAdmin"]
)
assert discovery["rootRejectedBeforeLaunch"] == "ROOT_TARGET_REJECTED"
assert discovery["changedDefaultTest"]["kind"] == "input-only-no-system-default-change"
assert (
    payload["passed"]
    and post["passed"]
    and post["cleanupRootAbsent"]
    and post["oldStageAbsent"]
)
records = payload["records"]
artifact = records[0]["rows"][0]
transfer = next(row for row in records if row.get("event") == "windows-pipe-transfer")
assert (
    transfer["sent"] == artifact["bytes"]
    and transfer["producerExit"] == transfer["receiverExit"] == 0
)
assert transfer["rows"][0]["sha256"] == artifact["sha256"]
baseline_sentinels = artifact["sentinels"]
installs = [row for row in records if row.get("args", [None])[0] == "install"]
for record in installs:
    assert record["offlineNetworkNamespace"]
    result = record["rows"][-1]
    assert result["uid"] == 1000 and result["networkConnectErrno"] == 101
    assert (
        result["recipeDownloadCalls"] == []
        and result["sentinels"] == baseline_sentinels
    )
    if result["event"] == "installation-rejected":
        assert not result["published"]
missing = next(row for row in installs if row["args"][2] == "missing")["rows"][-1]
bad_digest = next(row for row in installs if row["args"][2] == "bad-digest")["rows"][-1]
full = next(row for row in installs if row["args"][2] == "enospc")["rows"][-1]
assert (
    missing["errno"] == 2
    and bad_digest["type"] == "RuntimeError"
    and "checksum mismatch" in bad_digest["message"]
)
assert full["errno"] == 28 and full["injected"]
reopened = [row for row in installs if row["args"][2] == "interrupt"]
assert (
    len(reopened) == 2
    and reopened[0]["exit"] == 2
    and reopened[0]["rows"][-1]["stageExists"]
    and reopened[1]["exit"] == 0
)
assert any(
    row.get("event") == "extract-helper-exactly-killed"
    and row["facts"]["pidfdKilled"]
    and row["carrierExit"] != 0
    for row in records
)
assert (
    payload["cleanup"]["exit"] == 0
    and payload["cleanup"]["rows"][-1]["exists"] is False
)
assert (
    post["offlineConnectErrno"] == 101
    and Path(post["imported"]["package"]).is_relative_to(post["published"])
    and Path(post["imported"]["rg"]).is_relative_to(post["published"])
)
assert post["pgVersion"].endswith("17.9") and post["rgVersion"].startswith(
    "ripgrep 15.2.0"
)
result = dict(
    passed=True,
    discoveryTarget=discovery["target"],
    artifactBytes=artifact["bytes"],
    offlineNamespaceReal=True,
    missingArtifactRejected=True,
    badDigestRejected=True,
    nativeEnospcAtInjectedTarWrite=True,
    interruptedPartialRejectedOnReopen=True,
    explicitCleanupThenFreshInstall=True,
    postPublishActualRuntime=True,
    sentinelsPreserved=True,
    privateRootsRemoved=True,
)
(BASE / "verified-results.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
print(json.dumps(result))
