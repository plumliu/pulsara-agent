"""Read only session metadata for the installed alpha9 acceptance run."""
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

assert sys.executable.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9/')
with psycopg.connect('host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin', row_factory=dict_row) as connection:
    connection.execute('SET TRANSACTION READ ONLY')
    sessions = connection.execute('SELECT id,title,workspace_id,writer_generation,writer_lease_owner_id,writer_lease_expires_at, writer_lease_expires_at > clock_timestamp() AS lease_valid FROM pulsara_v3.sessions ORDER BY id').fetchall()
    turns = connection.execute('SELECT id,session_id,status FROM pulsara_v3.turns ORDER BY id').fetchall()
result = {'sessions': sessions, 'turns': turns, 'readOnly': True}
destination = Path(sys.argv[1])
assert not destination.exists()
destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, default=str))
