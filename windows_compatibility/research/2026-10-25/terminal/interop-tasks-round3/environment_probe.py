import json
import platform
import subprocess
import sys

import task_probe as T

CODE = """import json,os,socket,subprocess,sys
p='/home/plumwsl/workspaces/pulsara_agent/output/windows-g0/interop_tasks_r3/round3/tasks-5fb9ff37-normal-pipe/interop'
result=dict(python=sys.version,executable=sys.executable,gitHead=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),filesystem=subprocess.check_output(['findmnt','-T','/home/plumwsl/.g0tasks','-no','FSTYPE'],text=True).strip(),privateMode=oct(os.stat('/home/plumwsl/.g0tasks').st_mode&0o777),initialSocketPathBytes=len(p.encode()))
s=socket.socket(socket.AF_UNIX)
try: s.bind(p)
except OSError as exc: result['initialPathReproductionError']=repr(exc)
finally: s.close()
print(json.dumps(result))
"""
completed = subprocess.run(
    T.command("-c", CODE),
    capture_output=True,
    text=True,
    env=T.ENV,
    check=True,
    timeout=10,
)
result = dict(
    windowsPython=sys.version,
    windowsExecutable=sys.executable,
    windowsPlatform=platform.platform(),
    gitHead=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    linux=json.loads(completed.stdout),
)
(T.BASE / "environment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
