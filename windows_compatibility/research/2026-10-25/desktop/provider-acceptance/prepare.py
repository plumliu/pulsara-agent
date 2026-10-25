"""Private ext4 copy of test runners; production is imported from installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

repository=Path('/mnt/c/python_workspace/pulsara-agent')
output=repository/'output/phase-b/desktop/provider-acceptance'
root=Path(tempfile.mkdtemp(prefix='.provider-files-',dir=Path.home()))
assert root.stat().st_uid==os.getuid()==1000
shutil.copytree(repository/'tests',root/'tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copy2(output/'run_files_wsl.py',root/'run_files_wsl.py')
shutil.copy2(output/'run_crash_recovery.py',root/'run_crash_recovery.py')
shutil.copy2(repository/'output/windows-g0/pg_admission_r3/provider-acceptance/run_terminal_wsl.py',root/'pg_adapter.py')
scratch=root/'scratch';scratch.mkdir(mode=0o700)
target=json.loads((repository/'output/windows-g0/pg_admission_r3/provider-acceptance/prepared-target.json').read_text())
target.update(directory=str(root),harness=str(root/'run_files_wsl.py'),scratch_root=str(scratch),
    connection_id='connection:oeeiipbn63ytbexaxw74nv5tgy',model='deepseek-flash')
env=dict(os.environ);env.pop('PYTHONPATH',None)
check=subprocess.run([target['python'],'-I','-c',"import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/');sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')",str(root),str(root/'run_files_wsl.py'),'--check-imports','--kind','artifact'],cwd=root,env=env,capture_output=True,text=True)
target['imports']={'exit':check.returncode,'stdout':check.stdout,'stderr':check.stderr}
(output/'preparation.json').write_text(json.dumps(target,indent=2)+'\n')
print(json.dumps({'directory':str(root),'imports':target['imports']}))
assert check.returncode==0
