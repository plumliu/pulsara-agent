"""Read-only audit of identities and directories from this branch's smoke rows."""
import json
import os
from pathlib import Path

output = Path('/mnt/c/python_workspace/pulsara-agent/output/windows-b/host-setup')
host = json.loads((output/'host-smoke-results.json').read_text())
session = json.loads((output/'session-smoke-results.json').read_text())
ready = session['ready']
homes = [Path(host['home']),Path(ready['home'])]
assert all(p.parent == Path.home() and p.name.startswith(('.pulsara-host-smoke-', '.pulsara-session-smoke-')) for p in homes)
directory_rows = [{'path':str(p),'exists':p.exists()} for p in homes]
try:
 stat = Path(f'/proc/{ready["pid"]}/stat').read_text()
 start = stat[stat.rfind(')')+2:].split()[19]
 same_process = start == ready['starttime']
except FileNotFoundError:
 same_process = False
stage = json.loads((output/'build-stage.json').read_text())
build = Path(stage['root'])
assert build.parent == Path.home() and build.name.startswith('.pulsara-wsl-build-')
result = {'directories':directory_rows,'sessionExactIdentityAlive':same_process,'sessionCarrier':session['carrierExit'],'sessionHost':json.loads((output/'session-host-exit.json').read_text()),'browserHost':json.loads((output/'browser-host-exit.json').read_text()),'buildDirectory':{'path':str(build),'exists':build.exists(),'uid':build.stat().st_uid,'retainedFor':'current candidate reproducible build, not a live application owner'},'globalChanges':False}
readiness=json.loads((output/'readiness-smoke-results.json').read_text())
readiness_rows=[]
for case in readiness['cases']:
 identity=case['identity'];home=Path(identity['home'])
 assert home.parent==Path.home() and home.name.startswith('.pulsara-readiness-')
 process=Path(f'/proc/{identity["pid"]}')
 try:
  body=(process/'stat').read_text()
  alive=body[body.rfind(')')+2:].split()[19]==identity['starttime'] and b'readiness-guest.py' in (process/'cmdline').read_bytes()
 except FileNotFoundError:alive=False
 assert not alive and not home.exists() and case['carrier']=={'code':0,'signal':None}
 readiness_rows.append({'case':case['name'],'pid':identity['pid'],'starttime':identity['starttime'],'sameProcessAlive':alive,'home':str(home),'homeExists':home.exists(),'carrier':case['carrier']})
result['readiness']=readiness_rows
result['readinessHost']=json.loads((output/'readiness-smoke-host-exit.json').read_text())
result['readinessFixtureDirectoriesRemaining']=[str(path) for path in Path.home().glob('.pulsara-readiness-*')]
assert not result['readinessFixtureDirectoriesRemaining']
assert not same_process and not any(row['exists'] for row in directory_rows)
assert build.stat().st_uid == os.getuid() == 1000
result['passed'] = True
print(json.dumps(result,indent=2))
