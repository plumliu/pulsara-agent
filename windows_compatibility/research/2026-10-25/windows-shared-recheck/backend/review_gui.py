"""Offline qualification of the actual GUI observation, preserving its first failure."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
observed = json.loads((root / "gui-completed.json").read_text(encoding="utf-8"))
assert observed["completed"] and all(observed["prior_preserved"].values())
assert len(observed["turns"]) == 1 and observed["turns"][0]["status"] == "COMPLETED"
terminal = [item for item in observed["tool_calls"] if item["tool_name"] == "terminal"]
reads = [item for item in observed["tool_calls"] if item["tool_name"] == "find_files"]
assert len(terminal) == len(reads) == 1 and len(observed["tool_calls"]) == 2
assert reads[0]["tool_arguments"] == {"glob": "AGENTS.md", "path": ".", "limit": 10}
assert terminal[0]["tool_arguments"]["command"] == "printf 'WINDOWS_ALPHA10_GUI_OK\\n'; echo \"EXIT_CODE=$?\""
assert terminal[0]["tool_arguments"]["workdir"] == observed["workspace"]
results = [json.loads(item["content"]) for item in observed["entries"] if item["entry_kind"] == "TOOL_RESULT"]
assert len(results) == 2
term_result = next(item for item in results if "exit_code" in item)
read_result = next(item for item in results if "files" in item)
assert term_result["exit_code"] == 0 and term_result["status"] == "success"
assert term_result["output"] == "WINDOWS_ALPHA10_GUI_OK\nEXIT_CODE=0\n"
assert read_result["files"] == [] and read_result["status"] == "ok"
commands, queues = observed["commands"], observed["queue_items"]
assert len(commands) == len(queues) == 1 and commands[0]["command_kind"] == "QUEUE_PROMPT"
assert queues[0]["command_id"] == commands[0]["command_id"] and queues[0]["status"] == "CONSUMED"
assert queues[0]["consumed_entry_id"] == observed["turns"][0]["initial_entry_id"]
result = {"source": "gui-completed.json", "session_id": observed["session"]["id"],
          "completed_roots": 1, "unique_queue_prompt": True, "terminal_calls": 1,
          "additional_read_only_find_files": 1, "terminal_exit": 0, "terminal_output": term_result["output"],
          "prior_preserved": observed["prior_preserved"],
          "qualification": "Actual simple terminal roundtrip passed; model also searched AGENTS.md and printed the exit status. This is not an exactly-one-total-tool or literal-printf-only result. Initial observer assertion failure is retained; no message rerun or production change."}
target = root / "gui-verified.json"
assert not target.exists()
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
