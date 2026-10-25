"""Read-only final target/UUID-DB/process/directory audit; remove own idle test copy."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

import psycopg
from psycopg.conninfo import make_conninfo

output=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/provider-acceptance')
target=json.loads((output/'preparation.json').read_text())
rows=[]
for kind in ('artifact','memory','history','interruption','crash'):
 value=json.loads((output/(kind+'-result.json')).read_text())
 assert value['status']=='completed' and value['undropped_databases']==[]
 paths=[Path(value['scratch']),Path(value['test_home'])]
 assert paths[0].is_relative_to(Path(target['scratch_root']))
 assert paths[1].parent==Path.home() and paths[1].name.startswith('.pa-')
 assert not any(path.exists() or path.is_symlink() for path in paths)
 rows.append({'kind':kind,'database':value['created_database'],'dropRecorded':value['dropped_databases'],'scratch':str(paths[0]),'scratchExists':False,'home':str(paths[1]),'homeExists':False,'carrier':json.loads((output/(kind+'-exit.json')).read_text())['exit']})
 assert rows[-1]['carrier']==0 and value['created_database'] in value['dropped_databases']
dsn=make_conninfo(host=target['socket'],port=target['port'],user=target['admin_role'],dbname=target['root_database'])
with psycopg.connect(dsn) as connection:
 connection.execute('SET TRANSACTION READ ONLY')
 facts=connection.execute("SELECT current_setting('data_directory'),current_setting('unix_socket_directories'),current_setting('listen_addresses'),system_identifier::text,inet_server_addr(),pg_postmaster_start_time() FROM pg_control_system()").fetchone()
 assert facts[:5]==(target['expected_data'],target['socket'],'',target['expected_system_id'],None)
 previous=json.loads((output.parents[2]/'windows-g0/pg_admission_r3/provider-acceptance/current-installed-pg.json').read_text())
 assert facts[5]==datetime.fromisoformat(previous['postmaster_started'])
 remaining=connection.execute('SELECT datname FROM pg_database WHERE datname=ANY(%s)',([row['database'] for row in rows],)).fetchall()
 assert not remaining
 gui=connection.execute('SELECT count(*) FROM pulsara_v3.sessions WHERE id=%s',('session:r3d6okwuq2trl3ribxzct6rbuy',)).fetchone()[0]
 assert gui==1
lost=json.loads((output/'crash-result.json').read_text())['host_loss']
try:
 body=Path(f'/proc/{lost["pid"]}/stat').read_text()
 same=body.rsplit(')',1)[1].split()[19]==lost['starttime']
except FileNotFoundError:same=False
assert not same
root=Path(target['directory'])
assert root.parent==Path.home() and root.name.startswith('.provider-files-') and root.resolve()==root and root.stat().st_uid==os.getuid()==1000
assert not any((root/'scratch').iterdir())
result={'passed':True,'testRows':rows,'remainingOwnedDatabases':remaining,'formalPostmasterUnchanged':True,'formalPostmasterStarted':facts[5].isoformat(),'systemid':facts[3],'formalGuiSessionPreserved':True,'rootDatabase':target['root_database'],'clusterStopResetCalls':0,'killedOwnChild':lost,'sameChildAlive':same,'privateHarnessDirectory':str(root),'privateHarnessRemoved':False}
# Preserve only the exact runner inputs used, rather than copying the whole tests tree.
selected=('run_artifact_export_dogfood.py','run_direct_memory_dogfood.py','run_session_content_query_dogfood.py','run_turn_interruption_history_dogfood.py','run_model_switch_handover_dogfood.py','run_content_revision_line_edit_dogfood.py','run_terminal_cognitive_dogfood.py','credentials.py')
snapshot=output/'original-runners';snapshot.mkdir(exist_ok=True)
for name in selected:shutil.copy2(root/'tests/dogfood'/name,snapshot/name)
shutil.copy2(root/'pg_adapter.py',output/'pg_adapter_used.py')
shutil.rmtree(root)
result['privateHarnessRemoved']=not root.exists()
(output/'cleanup-audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'passed':True,'ownedDatabasesRemaining':0,'privateHarnessRemoved':True,'formalPostmasterUnchanged':True,'formalGuiSessionPreserved':True}))
