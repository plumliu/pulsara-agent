"""Read the one GUI sleep witness and compare the original two user histories."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
import pulsara_agent
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepared', 'after-wake', 'after-message'])
    args = parser.parse_args()
    output = Path(__file__).resolve().parent
    target = output / f'session-{args.stage}.json'
    assert not target.exists()
    home = require_pulsara_home()
    assert home == Path('/home/plumwsl/.pulsara')
    assert pulsara_agent.__version__ == '0.1.0a10'
    assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/')
    baseline = json.loads(Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/backend/upgrade-after.json').read_text(encoding='utf-8'))
    with psycopg.connect(f'host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin', row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        identity = dict(connection.execute("SELECT system_identifier::text,pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()").fetchone())
        assert identity['system_identifier'] == baseline['identity']['system_identifier']
        sessions = connection.execute("SELECT id,title,workspace_id,model_call_binding,writer_generation,writer_lease_owner_id,writer_lease_expires_at::text,writer_lease_expires_at > clock_timestamp() AS lease_valid FROM pulsara_v3.sessions WHERE id LIKE 'session:qha3722i%' ").fetchall()
        assert len(sessions) == 1
        session = dict(sessions[0])
        sid = session['id']
        turns = list(map(dict, connection.execute("SELECT id,status,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (sid,)).fetchall()))
        entries = list(map(dict, connection.execute("SELECT e.id,e.turn_id,e.entry_sequence,e.entry_kind,convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content FROM pulsara_v3.transcript_entries e LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id WHERE e.session_id=%s ORDER BY e.entry_sequence", (sid,)).fetchall()))
        blocks = list(map(dict, connection.execute("SELECT block_kind,tool_name,tool_arguments FROM pulsara_v3.assistant_message_blocks WHERE session_id=%s AND block_kind='TOOL_CALL' ORDER BY assistant_entry_id,block_ordinal", (sid,)).fetchall()))
        queues = list(map(dict, connection.execute("SELECT command_id,client_submission_id,status,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence", (sid,)).fetchall()))
        old = [row['id'] for row in baseline['sessions']]
        prior_sessions = list(map(dict, connection.execute("SELECT id,title,workspace_id,memory_domain_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=ANY(%s) ORDER BY id", (old,)).fetchall()))
        prior_turns = list(map(dict, connection.execute("SELECT id,session_id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=ANY(%s) ORDER BY session_id,id", (old,)).fetchall()))
        prior_entries = list(map(dict, connection.execute("SELECT e.id,e.session_id,e.turn_id,e.entry_sequence,e.entry_kind,e.conversation_scope_kind,e.content_codec,convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content FROM pulsara_v3.transcript_entries e LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id WHERE e.session_id=ANY(%s) ORDER BY e.session_id,e.entry_sequence", (old,)).fetchall()))
    expected = 2 if args.stage == 'after-message' else 1
    comparisons = {
        'originalSessionsPreserved': prior_sessions == baseline['sessions'],
        'originalTurnsPreserved': prior_turns == baseline['turns'],
        'originalEntriesPreserved': prior_entries == baseline['entries'],
        'expectedCompletedTurns': len(turns) == expected and all(row['status'] == 'COMPLETED' for row in turns),
        'singleTerminalLaunch': len([row for row in blocks if row['tool_name'] == 'terminal']) == 1,
        'uniqueConsumedPrompts': len(queues) == expected and len({row['client_submission_id'] for row in queues}) == expected and all(row['status'] == 'CONSUMED' for row in queues),
        'preciseTurnEntryConsumption': {row['consumed_entry_id'] for row in queues} == {row['initial_entry_id'] for row in turns},
        'writerLeaseValidAtObservation': session['lease_valid'] is True,
    }
    if args.stage != 'prepared':
        before = json.loads((output / 'session-prepared.json').read_text(encoding='utf-8'))
        comparisons['noToolReplayOrNewTool'] = blocks == before['tool_calls']
    facts = {'stage': args.stage, 'at': datetime.now(timezone.utc).isoformat(),
             'identity': identity, 'session': session, 'turns': turns, 'entries': entries,
             'tool_calls': blocks, 'queues': queues, 'comparisons': comparisons,
             'passed': all(comparisons.values()), 'readOnly': True}
    scrub = LocalSettingsStore(home / 'local-settings.yaml').read().credential_scrub_set()
    payload = json.dumps(scrub.scrub_json(facts), ensure_ascii=False, indent=2)
    assert not scrub.contains(payload)
    target.write_text(payload, encoding='utf-8')
    print(json.dumps({'stage': args.stage, 'session': sid, 'passed': facts['passed'], 'comparisons': comparisons}))
    assert facts['passed']


if __name__ == '__main__':
    main()
