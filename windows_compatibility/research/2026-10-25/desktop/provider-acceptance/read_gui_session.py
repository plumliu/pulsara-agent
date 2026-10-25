"""Targeted read-only cross-check of the user-authorized installed GUI session."""
import json
import os
from pathlib import Path
import stat

import psycopg
from psycopg.rows import dict_row
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

home=require_pulsara_home()
assert home==Path('/home/plumwsl/.pulsara')
scrub=LocalSettingsStore().read().credential_scrub_set()
dsn='host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin'
with psycopg.connect(dsn,row_factory=dict_row) as connection:
 connection.execute('SET TRANSACTION READ ONLY')
 physical=connection.execute("SELECT current_setting('data_directory') AS data,current_setting('unix_socket_directories') AS socket,system_identifier::text AS systemid,inet_server_addr() AS address FROM pg_control_system()").fetchone()
 assert physical=={'data':'/home/plumwsl/.pulsara/desktop/postgres','socket':'/home/plumwsl/.pulsara/desktop/socket','systemid':'7700523439330484737','address':None}
 sessions=connection.execute("SELECT id,title,workspace_id FROM pulsara_v3.sessions WHERE id LIKE %s",('%r3d6okwu%',)).fetchall()
 assert len(sessions)==1,sessions
 session=sessions[0]
 rows=connection.execute("""SELECT b.tool_name,b.tool_arguments,r.result_state,e.inline_content
  FROM pulsara_v3.assistant_message_blocks b
  JOIN pulsara_v3.tool_results r ON r.session_id=b.session_id AND r.tool_call_entry_id=b.assistant_entry_id AND r.tool_call_id=b.tool_call_id
  JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id
  WHERE b.session_id=%s AND b.block_kind='TOOL_CALL' ORDER BY b.assistant_entry_id,b.block_ordinal""",(session['id'],)).fetchall()
 tools=[]
 for row in rows:
  raw=bytes(row.pop('inline_content')).decode('utf-8')
  try:row['result']=json.loads(raw)
  except json.JSONDecodeError:row['result']=raw
  tools.append(row)
path=Path('/home/plumwsl/workspaces/Windows 中文 验收 20261025/smoke.txt')
descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
try:
 info=os.fstat(descriptor)
 assert stat.S_ISREG(info.st_mode) and info.st_uid==1000
 content=os.read(descriptor,info.st_size+1)
finally:os.close(descriptor)
result={'readOnly':True,'home':str(home),'postgres':physical,'session':session,'tools':tools,
 'file':{'path':str(path),'bytes':len(content),'text':content.decode('utf-8'),'mode':oct(stat.S_IMODE(info.st_mode)),'uid':info.st_uid,'CRLF':content.count(b'\r\n')},
 'assertions':{'writeSuccess':any(row['tool_name']=='write_file' and row['result_state']=='SUCCESS' for row in tools),'readSuccess':any(row['tool_name']=='read_file' and row['result_state']=='SUCCESS' for row in tools)}}
assert all(result['assertions'].values())
assert len(content)==31 and b'\r\n' not in content and stat.S_IMODE(info.st_mode)==0o600
public=scrub.scrub_json(result)
encoded=json.dumps(public,ensure_ascii=False,indent=2)
assert not scrub.contains(encoded)
output=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/provider-acceptance/gui-session-read-only.json')
output.write_text(encoded+'\n')
print(json.dumps({'session':session['id'],'tools':len(tools),'fileBytes':len(content),'readOnly':True}))
