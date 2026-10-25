"""Launch one approved original provider runner through installed Python."""
import json
import os
from pathlib import Path
import subprocess
import sys

output=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/provider-acceptance')
target=json.loads((output/'preparation.json').read_text())
kind=sys.argv[1]
cases=sys.argv[2] if len(sys.argv)>2 else 'small,log,json,invalid_json,multi,snapshot,existing,readonly,no_terminal'
destination=output/(kind+'-result.json')
assert not destination.exists()
args=[target['python'],'-I','-c',"import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/');sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')",target['directory'],target['harness'],'--execute','--kind',kind,'--cases',cases,'--output',str(destination)]
for name in ('saved_home','connection_id','model','distribution','user','expected_uid','interop_helper','socket','port','admin_role','runtime_role','root_database','expected_data','expected_system_id','scratch_root'):
 args.extend(['--'+name.replace('_','-'),str(target[name])])
env=dict(os.environ);env.pop('PYTHONPATH',None)
with (output/(kind+'-stdout.log')).open('w') as stdout,(output/(kind+'-stderr.log')).open('w') as stderr:
 child=subprocess.run(args,cwd=target['directory'],env=env,stdout=stdout,stderr=stderr)
(output/(kind+'-exit.json')).write_text(json.dumps({'exit':child.returncode,'output':str(destination)},indent=2)+'\n')
print(json.dumps({'kind':kind,'exit':child.returncode,'output':str(destination)}))
raise SystemExit(child.returncode)
