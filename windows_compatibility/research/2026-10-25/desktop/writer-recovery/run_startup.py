import json
import os
from pathlib import Path
import subprocess

root=Path.cwd();out=root/'output/phase-b/desktop/writer-recovery'
environment={k:v for k,v in os.environ.items() if k.upper()!='ELECTRON_RUN_AS_NODE'}
result=subprocess.run([str(root/'desktop/node_modules/electron/dist/electron.exe'),
    str(root/'desktop/scripts/startup-cover-smoke.cjs')],capture_output=True,text=True,encoding='utf-8',env=environment)
(out/'startup-real-stdout.log').write_text(result.stdout,encoding='utf-8')
(out/'startup-real-stderr.log').write_text(result.stderr,encoding='utf-8')
(out/'startup-real-result.json').write_text(json.dumps({'exitCode':result.returncode,
    'passed':result.returncode==0 and 'PASS:' in result.stdout,'originalSmoke':True,'formalAppStarted':False},indent=2)+'\n')
print(result.stdout);print(result.stderr)
assert result.returncode==0 and 'PASS:' in result.stdout
