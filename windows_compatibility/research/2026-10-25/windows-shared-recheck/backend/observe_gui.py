"""Observe only the explicitly created alpha10 GUI witness and prior histories."""
from datetime import datetime, timezone
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

home = require_pulsara_home()
assert home == Path("/home/plumwsl/.pulsara")
output = Path(__file__).resolve().parent
before = json.loads((output / "upgrade-after.json").read_text(encoding="utf-8"))
workspace = "/home/plumwsl/workspaces/Windows 共享修复 alpha10 验收"
with psycopg.connect(f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin", row_factory=dict_row) as connection:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    identity = dict(connection.execute("SELECT system_identifier::text,pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()").fetchone())
    matches = connection.execute(
        "SELECT s.id,s.title,s.model_call_binding,s.workspace_id FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w "
        "ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id WHERE w.workspace_root=%s", (workspace,)).fetchall()
    assert len(matches) == 1
    session = dict(matches[0])
    session_id = session["id"]
    turns = list(map(dict, connection.execute("SELECT id,status,conversation_scope_kind,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (session_id,)).fetchall()))
    entries = list(map(dict, connection.execute(
        "SELECT e.id,e.turn_id,e.entry_kind,e.entry_sequence,convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content "
        "FROM pulsara_v3.transcript_entries e LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id WHERE e.session_id=%s ORDER BY e.entry_sequence", (session_id,)).fetchall()))
    blocks = list(map(dict, connection.execute(
        "SELECT block_kind,tool_name,tool_arguments FROM pulsara_v3.assistant_message_blocks WHERE session_id=%s AND block_kind='TOOL_CALL' ORDER BY assistant_entry_id,block_ordinal", (session_id,)).fetchall()))
    queues = list(map(dict, connection.execute("SELECT command_id,client_submission_id,status,consumed_entry_id,delivery_mode FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence", (session_id,)).fetchall()))
    commands = list(map(dict, connection.execute("SELECT command_id,command_kind FROM pulsara_v3.session_commands WHERE session_id=%s ORDER BY command_id", (session_id,)).fetchall()))
    old_ids = [row["id"] for row in before["sessions"]]
    prior_sessions = list(map(dict, connection.execute("SELECT id,title,workspace_id,memory_domain_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=ANY(%s) ORDER BY id", (old_ids,)).fetchall()))
    prior_turns = list(map(dict, connection.execute("SELECT id,session_id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=ANY(%s) ORDER BY session_id,id", (old_ids,)).fetchall()))
    prior_entries = list(map(dict, connection.execute(
        "SELECT e.id,e.session_id,e.turn_id,e.entry_sequence,e.entry_kind,e.conversation_scope_kind,e.content_codec,convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content "
        "FROM pulsara_v3.transcript_entries e LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id WHERE e.session_id=ANY(%s) ORDER BY e.session_id,e.entry_sequence", (old_ids,)).fetchall()))
ready = len(turns) == 1 and turns[0]["status"] == "COMPLETED"
result = {"observed_at": datetime.now(timezone.utc).isoformat(), "workspace": workspace,
          "identity": identity, "session": session, "turns": turns, "entries": entries,
          "tool_calls": blocks, "queue_items": queues, "commands": commands, "completed": ready,
          "prior_preserved": {"sessions": prior_sessions == before["sessions"], "turns": prior_turns == before["turns"], "entries": prior_entries == before["entries"]}}
assert identity["system_identifier"] == before["identity"]["system_identifier"]
assert identity["postmaster_start"] == before["identity"]["postmaster_start"]
assert all(result["prior_preserved"].values())
scrub = LocalSettingsStore(home / "local-settings.yaml").read().credential_scrub_set()
payload = json.dumps(scrub.scrub_json(result), ensure_ascii=False, indent=2)
assert not scrub.contains(payload)
target = output / ("gui-completed.json" if ready else "gui-pending-" + datetime.now(timezone.utc).strftime("%H%M%S") + ".json")
assert not target.exists()
target.write_text(payload, encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
if ready:
    assert len(blocks) == 1 and blocks[0]["tool_name"] == "terminal"
    assert len(commands) == len(queues) == 1 and commands[0]["command_kind"] == "QUEUE_PROMPT"
    assert queues[0]["status"] == "CONSUMED" and queues[0]["consumed_entry_id"] == turns[0]["initial_entry_id"]
