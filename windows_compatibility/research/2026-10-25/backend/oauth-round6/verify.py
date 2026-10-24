"""Read stored safe host events; no OAuth request or credential is replayed."""

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

OUT = Path(__file__).resolve().parent
mock = json.loads((OUT / "mock.host-results.json").read_text(encoding="utf-8"))
real = json.loads((OUT / "real.host-results.json").read_text(encoding="utf-8"))


def guest(records, kind):
    return [
        row["record"]
        for row in records
        if row["event"] == "guest-event" and row["record"]["type"] == kind
    ]


cases = {row["case"]: row for row in guest(mock["records"], "case-result")}
assert set(cases) == {
    "success",
    "state",
    "issuer",
    "pkce",
    "reuse",
    "cancel",
    "port-conflict",
    "pipe-loss",
}
for case in cases.values():
    assert case["listener_closed"] and case["pending_count"] == 0
for name in ("success", "reuse"):
    assert cases[name]["state"] == "authorized" and cases[name]["grant_present"]
    assert cases[name]["pkce_valid"] and cases[name]["token_requests"] == 1
for name in ("state", "issuer", "cancel", "port-conflict", "pipe-loss"):
    assert not cases[name]["grant_present"] and cases[name]["token_requests"] == 0
assert cases["state"]["callback_statuses"] == [400]
assert cases["issuer"]["callback_outcomes"] == ["error"]
assert cases["reuse"]["callback_statuses"] == [400, 200]
assert (
    cases["pkce"]["pkce_valid"]
    and cases["pkce"]["state"] == "failed"
    and not cases["pkce"]["grant_present"]
)
assert any(row["event"] == "host-control-pipe-eof" for row in mock["records"])
after = [row for row in mock["records"] if row["event"] == "listener-after-settlement"]
assert len(after) == 8 and all(not row["reachable"] for row in after)
opened = [
    row for row in mock["records"] if row["event"] == "shell-open-external-returned"
]
assert len(opened) == 7 and all(row["case"] != "port-conflict" for row in opened)

(result,) = guest(real["records"], "real-result")
assert result["state"] == "authorized" and result["tool_count"] == 65
assert result["protocol_version"] == "2026-07-28" and not result["next_cursor_present"]
assert (
    result["listener_closed"]
    and result["pending_count"] == 0
    and result["local_grant_removed"]
)
assert result["business_tools_called"] is False
assert any(
    row["method"] == "POST" and row["path"] == "/token" and row["status"] == 200
    for row in guest(real["records"], "oauth-http")
)
assert any(
    row["event"] == "listener-present-before-open" and row["reachable"]
    for row in real["records"]
)
for document in (mock, real):
    assert document["records"][-1]["event"] == "guest-carrier-closed"
    assert (
        document["records"][-1]["code"] == 0
        and not document["records"][-1]["partialFrame"]
    )
    assert all(row["removed"] for row in guest(document["records"], "guest-cleaned"))

prohibited = {
    "url",
    "authorization_url",
    "access_token",
    "refresh_token",
    "client_secret",
    "code_verifier",
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
    for file in OUT.glob(pattern):
        raw = file.read_text(encoding="utf-8")
        assert not re.search(
            r"[?&](?:code|state|code_challenge|client_secret|access_token)=", raw
        ), file.name
        if file.suffix == ".json":
            inspect(json.loads(raw))
        else:
            for line in raw.splitlines():
                if line.strip():
                    inspect(json.loads(line))
        checked.append(file.name)
summary = {
    "passed": True,
    "mock_cases": 8,
    "real_authorized": True,
    "real_tool_count": result["tool_count"],
    "protocol_version": result["protocol_version"],
    "local_grant_removed": True,
    "remote_revocation_performed": False,
    "host_structure_and_authquery_scan_passed": True,
    "checked_files": sorted(checked),
    "exact_secret_scrub": "performed in Linux with observed values before diagnostics crossed host",
}
(OUT / "verified-results.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary))
