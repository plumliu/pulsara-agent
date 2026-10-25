from pathlib import Path
import json
import sys
import psycopg
from pulsara_agent.capability.pulsara_home import require_pulsara_home

stage = sys.argv[1]
assert stage in ('before', 'after')
home = Path(require_pulsara_home())
assert home == Path('/home/plumwsl/.pulsara')
session = 'session:r3d6okwuq2trl3ribxzct6rbuy'
dsn = f'host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin'
with psycopg.connect(dsn, autocommit=True) as conn:
    conn.execute('SET default_transaction_read_only = on')
    identity = conn.execute("SELECT system_identifier::text, current_setting('data_directory'), current_setting('unix_socket_directories'), current_setting('listen_addresses'), pg_postmaster_start_time()::text FROM pg_control_system()").fetchone()
    assert identity[0] == '7700523439330484737'
    assert identity[1:4] == (str(home/'desktop/postgres'), str(home/'desktop/socket'), '')
    row = conn.execute('SELECT id,title,workspace_id FROM pulsara_v3.sessions WHERE id=%s', (session,)).fetchone()
    assert row is not None
    turns = conn.execute('SELECT id,status::text FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id', (session,)).fetchall()
settings = (home/'local-settings.yaml').stat()
result = {'identity': identity, 'session': row, 'turns': turns,
          'data_inode': (home/'desktop/postgres').stat().st_ino,
          'settings_stat': {'inode': settings.st_ino, 'size': settings.st_size, 'mtime_ns': settings.st_mtime_ns}}
output = Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/root')
if stage == 'after':
    before = json.loads((output/'installed-restart-before.json').read_text())
    result['preserved'] = {key: json.loads(json.dumps(result[key])) == before[key]
                           for key in ('session','turns','data_inode','settings_stat')}
    assert all(result['preserved'].values()), result['preserved']
    assert identity[:4] == tuple(before['identity'][:4])
    assert identity[4] != before['identity'][4], 'Expected a real new postmaster start'
(output/f'installed-restart-{stage}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
