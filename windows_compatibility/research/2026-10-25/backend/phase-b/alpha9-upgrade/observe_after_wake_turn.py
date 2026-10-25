"""Read only the two GUI-submitted sleep qualification turns and original history."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.capability.pulsara_home import require_pulsara_home

home = require_pulsara_home()
assert home == Path("/home/plumwsl/.pulsara")
output = Path(__file__).resolve().parent
baseline = json.loads((output / "alpha9-upgrade-after9.json").read_text(encoding="utf-8"))
session_id = "session:36omh67maz3ezaef3amghzb6ka"
now = datetime.now(timezone.utc)
result = {"stage": "after-wake-turn", "observed_at": now.isoformat(), "session_id": session_id}
with psycopg.connect(f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin", row_factory=dict_row) as connection:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    result["identity"] = dict(connection.execute(
        "SELECT system_identifier::text AS system_identifier,pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()"
    ).fetchone())
    result["session"] = dict(connection.execute("SELECT id,title,writer_generation FROM pulsara_v3.sessions WHERE id=%s", (session_id,)).fetchone())
    result["turns"] = list(map(dict, connection.execute(
        "SELECT id,status,conversation_scope_kind,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (session_id,)).fetchall()))
    result["entries"] = list(map(dict, connection.execute(
        "SELECT e.id,e.turn_id,e.entry_sequence,e.entry_kind,e.content_codec,convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content "
        "FROM pulsara_v3.transcript_entries e LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id "
        "WHERE e.session_id=%s ORDER BY e.entry_sequence", (session_id,)).fetchall()))
    result["assistant_blocks"] = list(map(dict, connection.execute(
        "SELECT a.assistant_entry_id,a.block_ordinal,a.block_kind,a.tool_name,a.content_codec,convert_from(COALESCE(a.inline_content,b.body),'UTF8') AS content "
        "FROM pulsara_v3.assistant_message_blocks a LEFT JOIN pulsara_v3.blobs b ON b.id=a.blob_id "
        "WHERE a.session_id=%s ORDER BY a.assistant_entry_id,a.block_ordinal", (session_id,)).fetchall()))
    result["commands"] = list(map(dict, connection.execute(
        "SELECT command_id,command_kind,target_turn_id,target_entry_id FROM pulsara_v3.session_commands WHERE session_id=%s ORDER BY accepted_at,command_id", (session_id,)).fetchall()))
    result["queue_items"] = list(map(dict, connection.execute(
        "SELECT id,queue_sequence,command_id,client_submission_id,delivery_mode,input_origin,status,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence", (session_id,)).fetchall()))
    original = baseline["session"]["id"]
    result["original_session"] = dict(connection.execute("SELECT id,title,workspace_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=%s", (original,)).fetchone())
    result["original_turns"] = list(map(dict, connection.execute(
        "SELECT id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (original,)).fetchall()))
    result["original_entry_counts"] = list(map(dict, connection.execute(
        "SELECT entry_kind,count(*) AS total FROM pulsara_v3.transcript_entries WHERE session_id=%s GROUP BY entry_kind ORDER BY entry_kind", (original,)).fetchall()))
counts = Counter(item["entry_kind"] for item in result["entries"])
result["entry_counts"] = dict(counts)
result["original_preserved"] = {
    "session": result["original_session"] == baseline["session"],
    "turns": result["original_turns"] == baseline["turns"],
    "entry_counts": result["original_entry_counts"] == baseline["transcript_entry_counts"],
}
result["complete"] = len(result["turns"]) == 2 and all(row["status"] == "COMPLETED" and row["conversation_scope_kind"] == "ROOT" for row in result["turns"])
result["no_duplicate_or_tool_entries"] = counts == {"USER_MESSAGE": 2, "ASSISTANT_MESSAGE": 2}
result["two_prompt_commands"] = len(result["commands"]) == 2 and all(row["command_kind"] == "QUEUE_PROMPT" for row in result["commands"])
result["exact_queue_consumption"] = (
    len(result["queue_items"]) == 2
    and all(row["status"] == "CONSUMED" and row["delivery_mode"] == "NEW_TURN" and row["input_origin"] == "HUMAN_MESSAGE" for row in result["queue_items"])
    and {row["command_id"] for row in result["queue_items"]} == {row["command_id"] for row in result["commands"]}
    and {row["consumed_entry_id"] for row in result["queue_items"]} == {row["initial_entry_id"] for row in result["turns"]}
    and len({row["client_submission_id"] for row in result["queue_items"]}) == 2
)
payload = json.dumps(result, ensure_ascii=False, indent=2)
scrub = LocalSettingsStore(home / "local-settings.yaml").read().credential_scrub_set()
assert scrub.scrub_text(payload) == payload, "refuse to export an actual saved credential"
target = output / ("after-wake-turn-verified.json" if result["complete"] else f"after-wake-pending-{now.strftime('%H%M%S')}.json")
assert not target.exists()
target.write_text(payload, encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
assert all(result["original_preserved"].values())
assert result["identity"]["system_identifier"] == baseline["identity"]["system_identifier"]
if result["complete"]:
    assert result["no_duplicate_or_tool_entries"] and result["two_prompt_commands"] and result["exact_queue_consumption"]
