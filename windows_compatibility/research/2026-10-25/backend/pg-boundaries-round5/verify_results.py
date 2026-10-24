"""Inspect stored physical evidence; do not rerun PostgreSQL."""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
groups = {}


def load(name):
    result = json.loads((OUT / name).read_text(encoding="utf-8"))
    assert result["failures"] == [], name
    for case in result["cases"]:
        assert case["cleanup"]["home_removed"], case["scenario"]
        assert all(item["wait"] == 0 for item in case["carrier_waits"])
        assert case["events"][0]["node"] == "v22.23.3"
        assert any(event["event"] == "case-complete" for event in case["events"])
    groups[name] = result["cases"]
    return result["cases"]


functional = load("functional.results.json")[0]
for event in functional["events"]:
    if event["event"] not in ("functional-queries", "moved-resource-queries"):
        continue
    rows = [json.loads(line) for line in event["result"]["stdout"].splitlines()]
    assert rows[0]["result"]["status"] == "verified"
    facts = next(row for row in rows if row["scenario"] == "pg-facts")
    assert facts["extensions"] == {"vector": "0.8.1", "pg_search": "0.26.1"}
    assert facts["facts"][2] == "17.9" and facts["facts"][5] == ""
    snippets = [row for row in rows if row["scenario"] == "bm25-snippet"]
    assert [row["query"] for row in snippets] == [
        "数据库",
        "running",
        "prepare_database",
    ]
    for snippet in snippets:
        document = snippet["rows"][0]
        source = document[1].encode("utf-8")
        spans = []
        for field in document[2:]:
            if field:
                for start, end in field:
                    assert 0 <= start < end <= len(source)
                    source[start:end].decode("utf-8", errors="strict")
                start, end = (
                    min(span[0] for span in field),
                    max(span[1] for span in field),
                )
                spans.append(source[start:end].decode("utf-8"))
        assert any(snippet["query"].casefold() in span.casefold() for span in spans)
    assert next(row for row in rows if row["scenario"] == "vector")["distance"] == 0
    if event["event"] == "moved-resource-queries":
        assert any(
            "/round5/moved PG/bin/postgres" in process.get("exe", "")
            for process in event["state"]["processes"]
        )

framing = load("framing.results.json") + load("framing-supplement.results.json")
for case in framing:
    result = next(
        event for event in case["events"] if event["event"] == "owner-fault-result"
    )
    assert result["owner"]["startedHere"] is False
    starts = [
        event
        for event in case["events"]
        if event["event"] == "carrier-spawn" and event["method"] == "run:pg_ctl:start"
    ]
    assert len(starts) == 1
    writes = [
        event
        for event in case["events"]
        if event["event"] in ("operation-written", "operation-written-before-ready")
        and event.get("id") == starts[0]["id"]
    ]
    assert len(writes) <= 1
    before = case["scenario"] in (
        "malformed-ready",
        "early-request",
        "duplicate-request",
        "malformed-request",
        "partial-request",
    )
    assert result["error"]["postgresExecution"] == (
        "not-submitted" if before else "unknown"
    )
    assert result["state"]["status"]["exit"] == (3 if before else 0)
    if case["scenario"] in (
        "early-request",
        "duplicate-request",
        "malformed-request",
        "partial-request",
    ):
        assert not any(
            event["event"] == "helper-frame"
            and event["method"] == "run:pg_ctl:start"
            and event["frame"]["type"] == "accepted"
            for event in case["events"]
        )

mutators = load("mutators.results.json")
for case in mutators:
    events = case["events"]
    result = next(
        (event for event in events if event["event"] == "owner-fault-result"), None
    )
    if case["scenario"].endswith("ready-eof"):
        expected = (
            "run:initdb"
            if case["scenario"].startswith("initdb")
            else "run:pg_ctl:stop"
            if case["scenario"].startswith("stop")
            else "removeDataDirectory"
        )
        attempt = next(
            event
            for event in events
            if event["event"] == "carrier-spawn" and event["method"] == expected
        )
        assert not any(
            event["event"] == "operation-written" and event.get("id") == attempt["id"]
            for event in events
        )
        assert result["error"]["postgresExecution"] == "not-submitted"
    if case["scenario"] in ("initdb-death", "stop-death"):
        state = next(
            event["state"] for event in events if event["event"] == "after-helper-death"
        )
        assert not state["kernel"]["acquired"]
        assert not state["pg"]["acquired"]
    if case["scenario"] in ("stop-ready-eof", "stop-reply-loss", "stop-death"):
        assert not any(
            event["event"] == "carrier-spawn"
            and event["method"] == "removeDataDirectory"
            for event in events
        )
        assert result["state"]["data_sentinel"] == "data-sentinel"
    if case["scenario"].startswith("remove-"):
        initdb = [
            event
            for event in events
            if event["event"] == "carrier-spawn" and event["method"] == "run:initdb"
        ]
        assert len(initdb) == 1  # Initial creation only; no retry/reinitialization.
    if case["scenario"] == "remove-reply-loss":
        assert (
            not result["state"]["data_exists"]
            and result["state"]["status"]["exit"] == 4
        )
        assert result["owner"]["startedHere"] is True
    if case["scenario"] == "concurrent-start":
        assert result["error"]["code"] == "PG_LIFETIME_BUSY"
        assert result["error"]["postgresExecution"] == "not-submitted"
        assert (
            result["state"]["status"]["exit"] == 0
            and result["state"]["data_sentinel"] == "data-sentinel"
        )
    if case["scenario"] == "readiness-fail":
        assert (
            result["error"]["code"] == 1 and "postgresExecution" not in result["error"]
        )
        assert result["owner"]["startedHere"] is True
        assert any(
            event["event"] == "readiness-failed-but-server-ready" for event in events
        )
    if case["scenario"] == "start-fail":
        assert result["error"]["code"] == 1 and result["owner"]["startedHere"] is False
    if case["scenario"] == "kernel-lock":
        event = next(
            event for event in events if event["event"] == "kernel-maintenance-refused"
        )
        assert (
            event["error"]["code"] == "PG_KERNEL_BUSY"
            and event["state"]["status"]["exit"] == 0
        )

load("extracted-client.results.json")
utf8 = load("utf8.results.json")[0]
assert (
    next(
        event
        for event in utf8["events"]
        if event["event"] == "utf8-codepoint-splits-verified"
    )["splits"]
    > 0
)
partial = load("partial-delete.results.json")[0]
result = next(
    event for event in partial["events"] if event["event"] == "owner-fault-result"
)
assert (
    result["error"]["postgresExecution"] == "unknown" and result["owner"]["startedHere"]
)
assert (
    result["state"]["data_exists"]
    and not result["state"]["partial_deletion"]["still_exists"]
)
assert result["state"]["kernel"]["acquired"] and result["state"]["pg"]["acquired"]
missing = load("readonly.results.json")[0]
event = next(
    event for event in missing["events"] if event["event"] == "readonly-known-missing"
)
assert event["error"]["code"] == "ENOENT" and "postgresExecution" not in event["error"]

summary = {
    "passed": True,
    "physical_cases": sum(len(cases) for cases in groups.values()),
    "groups": {name: len(cases) for name, cases in groups.items()},
    "utf8_original_byte_spans_verified": True,
    "real_nonzero_readback_verified": True,
}
(OUT / "verified-results.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary))
