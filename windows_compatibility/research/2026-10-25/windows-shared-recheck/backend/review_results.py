"""Read saved settings only to check exact credentials and existing result files."""
import json
import os
from pathlib import Path

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

root = Path(__file__).resolve().parent
result_path = root / "review.json"
assert not result_path.exists()
settings = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
scrub = settings.credential_scrub_set()
report = json.loads((root / "r5-first/context/result.json").read_text(encoding="utf-8"))
assert report["status"] == "completed" and len(report["cases"]) == 1
assert report["cases"][0]["status"] == "passed"
checks, calls = report["prefix_checks"], report["provider_calls"]
assert len(checks) == len(calls) == 8
old = {}
compared = 0
encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
markers = ["ROOT_CONTEXT_MARKER_ONE_7C1A", "ROOT_CONTEXT_MARKER_TWO_9B4E", "MIDFLIGHT_GUIDANCE_MARKER_2D6F"]
marker_observations = []
for check, call in zip(checks, calls, strict=True):
    key = (check["session_id"], check["scope"], check["task_id"], check["epoch"])
    wire = call["provider_input"]
    messages = [encoded(value) for value in wire["messages"]]
    tools = encoded(wire["tools"])
    if key in old:
        prior_messages, prior_tools = old[key]
        assert tools == prior_tools and messages[:len(prior_messages)] == prior_messages
        compared += 1
    old[key] = messages, tools
    if check["scope"] == "SUBAGENT_TASK":
        text = encoded(wire)
        marker_observations.append({"sequence": call["sequence"], "markers_present": {m: m in text for m in markers}})
assert marker_observations[0]["markers_present"][markers[0]] and marker_observations[0]["markers_present"][markers[1]]
assert all(marker_observations[-1]["markers_present"].values())
case = report["cases"][0]["last_n_and_message"]
assert case["steer_statuses"] == ["PENDING", "PENDING"] and case["inter_agent_entry_count"] == 1
assert case["worker_status"] == "COMPLETED" and case["worker_result_source"] == "EXPLICIT"
files = [path for path in root.rglob("*") if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".xml", ".md"}]
for path in files:
    assert not scrub.contains(path.read_bytes()), f"actual credential present in {path}"
private = [report["test_home"], json.loads((root / "r5-first/result.json").read_text())["root"]]
remaining, inaccessible = [], []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        command = entry.joinpath("cmdline").read_bytes()
        environment = entry.joinpath("environ").read_bytes()
        if any(os.fsencode(value) in command or os.fsencode(value) in environment for value in private):
            remaining.append(int(entry.name))
    except PermissionError:
        inaccessible.append(int(entry.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
assert not remaining and all(not Path(value).exists() for value in private)
result = {"provider_calls": len(calls), "epochs": len(old), "exact_wire_prefix_comparisons": compared,
          "marker_observations": marker_observations, "two_pending": True, "mailbox_entries": 1,
          "actual_saved_credential_matches": 0, "credential_checked_files": len(files),
          "private_paths_absent": private, "remaining_exact_owned_accessible_processes": remaining,
          "same_uid_environ_inaccessible": inaccessible,
          "limits": "Independent read-only record check; inaccessible process environments are not claimed empty. Random context does not independently identify the new post-accept retirement branch; R1 supplies its deterministic proof."}
result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
