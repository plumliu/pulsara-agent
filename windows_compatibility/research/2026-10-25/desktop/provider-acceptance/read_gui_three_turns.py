"""Read the one authorized GUI session; never open/resume or change its owner."""
import json
import os
from pathlib import Path
import stat
import psycopg
from psycopg.rows import dict_row
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

output=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/provider-acceptance')
root_output=output.parents[1]/'root'
home=require_pulsara_home();assert home==Path('/home/plumwsl/.pulsara')
file=home/'local-settings.yaml';before=file.stat()
saved=LocalSettingsStore().read();scrub=saved.credential_scrub_set()
session_id='session:r3d6okwuq2trl3ribxzct6rbuy'
with psycopg.connect('host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin',row_factory=dict_row) as connection:
 connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
 physical=connection.execute("SELECT current_setting('data_directory') AS data,current_setting('unix_socket_directories') AS socket,system_identifier::text AS systemid,inet_server_addr() AS address FROM pg_control_system()").fetchone()
 assert physical=={'data':'/home/plumwsl/.pulsara/desktop/postgres','socket':'/home/plumwsl/.pulsara/desktop/socket','systemid':'7700523439330484737','address':None}
 session=connection.execute('SELECT id,title,workspace_id FROM pulsara_v3.sessions WHERE id=%s',(session_id,)).fetchone();assert session
 turns=connection.execute('SELECT t.id,t.status,t.initial_entry_id,t.final_entry_id,e.entry_sequence AS initial_sequence FROM pulsara_v3.turns t JOIN pulsara_v3.transcript_entries e ON e.id=t.initial_entry_id AND e.session_id=t.session_id WHERE t.session_id=%s ORDER BY e.entry_sequence',(session_id,)).fetchall()
 messages=connection.execute("SELECT entry_sequence,entry_kind,turn_id,content_media_type,content_codec,inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind IN ('USER_MESSAGE','ASSISTANT_MESSAGE') ORDER BY entry_sequence",(session_id,)).fetchall()
 tools=connection.execute("""SELECT q.entry_sequence,b.tool_name,b.tool_arguments,r.result_state,e.inline_content
 FROM pulsara_v3.assistant_message_blocks b JOIN pulsara_v3.transcript_entries q ON q.session_id=b.session_id AND q.id=b.assistant_entry_id
 JOIN pulsara_v3.tool_results r ON r.session_id=b.session_id AND r.tool_call_entry_id=b.assistant_entry_id AND r.tool_call_id=b.tool_call_id
 JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id
 WHERE b.session_id=%s AND b.block_kind='TOOL_CALL' ORDER BY q.entry_sequence,b.block_ordinal""",(session_id,)).fetchall()
 for row in messages:row['content_utf8']=bytes(row.pop('inline_content')).decode('utf-8')
 for row in tools:
  text=bytes(row.pop('inline_content')).decode('utf-8')
  try:row['result']=json.loads(text)
  except json.JSONDecodeError:row['result']=text
assert len(turns)>=3 and all(row['status']=='COMPLETED' for row in turns[:3])
assert len([row for row in messages if row['entry_kind']=='USER_MESSAGE'])>=3
workspace=Path('/home/plumwsl/workspaces/Windows 中文 验收 20261025')
products=[]
for name,size in [('smoke.txt',31),('preview.html',3018),('preview.png',17701),('preview.pdf',592)]:
 path=workspace/name;descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 try:info=os.fstat(descriptor);data=os.read(descriptor,info.st_size+1)
 finally:os.close(descriptor)
 assert stat.S_ISREG(info.st_mode) and info.st_uid==1000 and len(data)==size
 products.append({'path':str(path),'bytes':len(data),'uid':info.st_uid,'mode':oct(stat.S_IMODE(info.st_mode))})
metadata={name:json.loads((root_output/('installed-gui-'+name+'.json')).read_text()) for name in ('previews','image-download','file-import','file-drop')}
imports=[]
for row in metadata['file-drop']:
 path=Path(row['path']);facts=path.lstat();parent=path.parent.lstat()
 assert stat.S_ISREG(facts.st_mode) and not path.is_symlink() and facts.st_uid==1000
 assert facts.st_size==285 and stat.S_IMODE(facts.st_mode)==0o400 and stat.S_IMODE(parent.st_mode)==0o500
 imports.append({'path':str(path),'bytes':facts.st_size,'mode':oct(stat.S_IMODE(facts.st_mode)),'parentMode':oct(stat.S_IMODE(parent.st_mode)),'uid':facts.st_uid})
after=file.stat()
assert (before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns)
result={'readOnly':True,'productionSessionOwnerNotOpened':True,'postgres':physical,'session':session,'turns':turns,'messages':messages,'tools':tools,'files':products,'imports':imports,
 'savedSettings':{'resolver':'require_pulsara_home + LocalSettingsStore','home':str(home),'writeCalls':0,'inodeSizeMtimeUnchangedDuringRead':True,'connections':[{'id':row.id.value,'model':row.target.model_id,'wire_api':row.target.wire_api.value} for row in saved.model_connections]},
 'guiEvidenceCrossReferences':metadata,'linkCorrectionBoundary':'Original model Markdown links with unescaped spaces remain verbatim in messages. Correction is a third new user turn, not rewritten history.'}
encoded=json.dumps(scrub.scrub_json(result),ensure_ascii=False,indent=2)
assert not scrub.contains(encoded)
(output/'gui-three-turn-read-only.json').write_text(encoded+'\n')
print(json.dumps({'readOnly':True,'turns':len(turns),'toolCalls':len(tools),'files':[row['bytes'] for row in products],'imports':len(imports)}))
