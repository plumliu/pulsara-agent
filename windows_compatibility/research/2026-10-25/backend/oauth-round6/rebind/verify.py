"""Offline assertions for the one physical stale-callback rebind case."""

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

root = Path(__file__).resolve().parent
document = json.loads((root / "rebind.host-results.json").read_text(encoding="utf-8"))
records = document["records"]
guest = [row["record"] for row in records if row["event"] == "guest-event"]
(retired,) = [row for row in guest if row["type"] == "original-flow-retired"]
assert retired == {
    "type": "original-flow-retired",
    "state": "cancelled",
    "listener_closed": True,
    "pending_count": 0,
}
(result,) = [row for row in guest if row["type"] == "case-result"]
assert result["state"] == "cancelled" and not result["grant_present"]
assert result["token_requests"] == 0 and result["pending_count"] == 0
assert result["listener_closed"] and result["foreign_listener_closed"]
assert (
    result["foreign_callback_requests"] == 1 and result["foreign_success_status"] == 200
)
assert result["old_callback_parameters_matched"]
assert any(
    row["event"] == "listener-present-before-open"
    and row["reachable"]
    and row["status"] == 404
    for row in records
)
assert any(
    row["event"] == "foreign-listener-present"
    and row["reachable"]
    and row["status"] == 200
    for row in records
)
assert any(row["event"] == "foreign-stale-navigation-opened" for row in records)
assert any(
    row["event"] == "listener-after-settlement" and not row["reachable"]
    for row in records
)
assert records[-1]["event"] == "guest-carrier-closed" and records[-1]["code"] == 0
assert not records[-1]["partialFrame"] and records[-1]["stderrBytes"] == 0
assert all(row["removed"] for row in guest if row["type"] == "guest-cleaned")

prohibited = {
    "url",
    "authorization_url",
    "access_token",
    "refresh_token",
    "client_secret",
    "code_verifier",
    "stale_parameters",
}


def inspect(value):
    if isinstance(value, dict):
        assert not prohibited.intersection(value)
        for item in value.values():
            inspect(item)
    elif isinstance(value, list):
        for item in value:
            inspect(item)
    elif isinstance(value, str):
        assert not re.search(
            r"[?&](?:code|state|code_challenge|client_secret|access_token)=", value
        )
        if value.startswith(("http://", "https://")):
            assert not urlsplit(value).query


checked = []
for pattern in (
    "*.host-results.json",
    "*.windows.log",
    "*.live.jsonl",
    "*.launch.json",
):
    for path in root.glob(pattern):
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            inspect(json.loads(raw))
        else:
            for line in raw.splitlines():
                if line.strip():
                    inspect(json.loads(line))
        checked.append(path.name)
summary = {
    "passed": True,
    "cases": 1,
    "original_flow_remains_cancelled": True,
    "foreign_stale_callback_http_status": 200,
    "token_requests": 0,
    "grant_present": False,
    "both_listeners_closed": True,
    "host_structure_and_authquery_scan_passed": True,
    "checked_files": sorted(checked),
}
(root / "verified-results.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary))
