"""Audit retained real-provider evidence without another provider call."""
import json
from pathlib import Path
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

output = Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/provider-acceptance")
saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
scrub = saved.credential_scrub_set()
summaries = {}
for name in ("terminal-real-provider.json", "terminal-short-home-real-provider.json", "terminal-input-closure-real-provider.json", "terminal-input-closure-clarified-real-provider.json", "terminal-toolchain-real-provider.json", "terminal-toolchain-explicit-node-real-provider.json"):
    path = output / name
    if not path.exists():
        continue
    raw = path.read_bytes()
    report = json.loads(raw)
    calls = report["provider_calls"]
    pairs = list(zip(calls, calls[1:]))
    tools = all(a["provider_input"]["tools"] == b["provider_input"]["tools"] for a, b in pairs)
    messages = all(b["provider_input"]["messages"][:len(a["provider_input"]["messages"])] == a["provider_input"]["messages"] for a, b in pairs)
    systems = [[message for message in call["provider_input"]["messages"] if message["role"] == "system"] for call in calls]
    system = all(value == systems[0] for value in systems)
    secret_found = scrub.contains(raw)
    summary = {"status": report["status"], "provider_calls": len(calls), "system_equal": system,
               "tools_equal": tools, "messages_append_only": messages, "configured_secret_found": secret_found,
               "undropped_databases": report.get("undropped_databases"), "scratch_removed": report.get("scratch_removed"),
               "test_home_removed": report.get("test_home_removed"), "cases": [],
               "reasoning": sorted({json.dumps(call["reasoning"], sort_keys=True) for call in calls})}
    offset = 0
    for case in report["cases"]:
        count = case.get("tool_call_count")
        trace = report["trace"][offset:offset + (count or 0)]
        offset += count or 0
        summary["cases"].append({"case": case["case"], "tool_calls": count,
                                 "result_states": [item["result_state"] for item in trace]})
    if name == "terminal-short-home-real-provider.json":
        cases = {item["case"]: item for item in report["cases"]}
        trace = report["trace"]
        assert [item["result"]["output"].strip() for item in trace[:3]] == ["alpha", "beta", "alpha"]
        assert "inside" in trace[3]["result"]["output"] and "inside" not in trace[4]["result"]["output"]
        assert cases["monitor_long_wake"]["manual_followup_turns"] == 0
        assert cases["monitor_long_wake"]["wake_elapsed_seconds"] >= 120
        capacity = trace[-cases["capacity"]["tool_call_count"]:]
        starts = [item for item in capacity if item["tool_name"] == "terminal"]
        assert len(starts) == 9
        ids = {item["result"]["process_id"] for item in starts[:8]}
        assert len(ids) == 8 and all(item["result"]["status"] == "running" for item in starts[:8])
        assert starts[8]["result"]["reason"] == "PROCESS_CAPACITY_EXHAUSTED" and starts[8]["result"]["process_id"] is None
        killed = [item for item in capacity if item["tool_name"] == "terminal_process" and item["arguments"].get("action") == "kill"]
        assert len(killed) == 8 and {item["arguments"]["process_id"] for item in killed} == ids
        assert all(item["result"]["status"] == "killed" for item in killed)
        summary["capacity_eight_exact_kills"] = True
        summary["monitor_autonomous_120s"] = True
    if report["status"] == "completed":
        assert system and tools and messages and not secret_found
        assert report["undropped_databases"] == [] and report["scratch_removed"] and report["test_home_removed"]
    assert not secret_found
    summaries[name] = summary
(output / "provider-records-review.json").write_text(json.dumps(summaries, indent=2))
print(json.dumps({name: {key: value[key] for key in ("status", "provider_calls", "system_equal", "tools_equal", "messages_append_only", "configured_secret_found")}
                  for name, value in summaries.items()}))
