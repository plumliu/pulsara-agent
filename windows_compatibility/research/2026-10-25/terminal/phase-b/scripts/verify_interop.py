"""Actual built Windows PE + production Linux owner integration snapshot."""

import json
from pathlib import Path
import subprocess
import sys
import uuid

base = Path(__file__).resolve().parent
code = r'''
import json,os,shutil,subprocess,tempfile,venv
from pathlib import Path
root=Path(tempfile.mkdtemp(prefix='.pulsara-interop-test-',dir='/home/plumwsl'))
source=Path('/mnt/c/python_workspace/pulsara-agent')
try:
 shutil.copytree(source/'src',root/'src')
 shutil.copytree(source/'tests',root/'tests')
 shutil.copyfile(source/'pyproject.toml',root/'pyproject.toml')
 rg=root/'src/pulsara_agent/_vendor/ripgrep/rg';rg.parent.mkdir(parents=True,exist_ok=True)
 shutil.copy2('/home/plumwsl/workspaces/pulsara_agent/src/pulsara_agent/_vendor/ripgrep/rg',rg)
 venv.create(root/'venv',with_pip=False)
 site=next((root/'venv/lib').glob('python*/site-packages'))
 (site/'pulsara-test.pth').write_text(chr(10).join([str(root/'src'),'/home/plumwsl/workspaces/pulsara_agent/.venv/lib/python3.12/site-packages','']))
 home=root/'home';home.mkdir(mode=0o700)
 env={**os.environ,'PULSARA_HOME':str(home),'PYTHONDONTWRITEBYTECODE':'1',
      'PULSARA_TEST_INTEROP_HELPER':str(source/'output/phase-b/terminal/helper/pulsara-interop.exe'),
      'PULSARA_TEST_INTEROP_CHILD':str(source/'output/phase-b/terminal/helper/interop-child.exe')}
 done=subprocess.run([str(root/'venv/bin/python'),'-B','-m','pytest','tests/windows_interop_integration.py','-vv','-s'],cwd=root,env=env,capture_output=True,text=True)
 print(json.dumps(dict(exit=done.returncode,stdout=done.stdout,stderr=done.stderr)))
finally:
 shutil.rmtree(root)
'''
if len(sys.argv) > 1:
    code = code.replace("'-vv','-s'", "'-vv','-s','-k'," + repr(sys.argv[1]))
done = subprocess.run(["C:/Windows/System32/wsl.exe", "-d", "Ubuntu", "-u", "plumwsl", "--exec", "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python", "-B", "-c", code], capture_output=True, text=True, encoding="utf-8")
print(done.stdout)
print(done.stderr)
assert done.returncode == 0
result = json.loads(done.stdout)
(base / ("interop-" + uuid.uuid4().hex[:8] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
assert result["exit"] == 0
