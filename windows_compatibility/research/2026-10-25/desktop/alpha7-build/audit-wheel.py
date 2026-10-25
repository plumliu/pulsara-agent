"""Read-only alpha7 input/wheel/static/licence audit, with original recipe pins."""
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path
import subprocess
import sys

repo=Path('/mnt/c/python_workspace/pulsara-agent')
out=repo/'output/phase-b/desktop/alpha7-build'
stage=json.loads((out/'build-stage.json').read_text())
resources=Path(stage['resources']);snapshot=Path(stage['snapshot'])
package=resources/'python/lib/python3.12/site-packages/pulsara_agent'
runner=(package/'conversation_kernel/runner.py').read_text()
guard='if intent.cause is not None or ('
start=runner.index('            while True:',runner.index('hook_context_reservation.commit()'))
excerpt=runner[start:start+650]
assert guard in excerpt and 'current_task.cancelling()' in excerpt and 'raise asyncio.CancelledError' in excerpt
assert 'except PreparedRootInputHeadStale:' in runner
assert 'class PreparedRootInputHeadStale' in (package/'conversation_kernel/repository_errors.py').read_text()
assert (package/'conversation_kernel/runner.py').read_text()==(snapshot/'src/pulsara_agent/conversation_kernel/runner.py').read_text()
fragments=[]
for path in (package/'desktop_app/static/assets').glob('index-*.js'):
 text=path.read_text()
 for found in re.finditer('Ctrl',text):
  position=found.start();fragment=text[max(0,position-90):position+125]
  if 'darwin' in fragment and ('⌘' in fragment or '\\u2318' in fragment):
   fragments.append({'path':str(path),'fragment':fragment})
assert fragments
sys.path.insert(0,str(snapshot/'desktop/scripts'))
import prepare_resources as recipe
licenses=resources/'licenses'
required=('PostgreSQL-COPYRIGHT','ParadeDB-AGPL-3.0','pgvector-LICENSE','Python-LICENSE.txt','database-sources.txt','paradedb-0.26.1-source.tar.gz','prepare_resources.py')
assert all((licenses/name).is_file() and (licenses/name).stat().st_size>0 for name in required)
assert hashlib.sha256((licenses/'ParadeDB-AGPL-3.0').read_bytes()).hexdigest()==recipe.SEARCH_LICENSE_SHA256
with (licenses/'paradedb-0.26.1-source.tar.gz').open('rb') as stream:
 assert hashlib.file_digest(stream,'sha256').hexdigest()==recipe.SEARCH_SOURCE_SHA256
code="import importlib.metadata,json,pulsara_agent;print(json.dumps({'runtime':pulsara_agent.__version__,'distribution':importlib.metadata.version('pulsara-agent'),'python':__import__('platform').python_version()}))"
versions=json.loads(subprocess.check_output([str(resources/'python/bin/python3'),'-I','-c',code],text=True))
assert versions=={'runtime':'0.1.0a7','distribution':'0.1.0a7','python':'3.12.15'}
resource_versions=json.loads((resources/'versions.json').read_text())
assert resource_versions['postgresql']=='17.9' and resource_versions['pg_search']=='0.26.1' and resource_versions['vector']=='0.8.1'
result={'passed':True,'snapshot':{'start':stage['snapshotStarted'],'finish':stage['snapshotFinished'],'source':stage['sourceRepository']},'versions':versions,'resourceVersions':resource_versions,
 'rootFix':{'wheelSource':str(package/'conversation_kernel/runner.py'),'sameTextAsCopiedFrozenSource':True,'cancelBeforeReprepareGuard':excerpt,'typedHeadStaleClass':True},
 'staticCtrl':fragments,'licenses':{'requiredPresent':list(required),'files':len(list(licenses.iterdir())),'pinnedUpstreamAgplAndCorrespondingSourceVerified':True},
 'recipe':'prepare_wsl_payload.package called existing prepare_resources.validate_resources before tar; native dependency resources reused, current application wheel rebuilt',
 'databaseConnected':False,'installedRuntimeModified':False}
(out/'wheel-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'runtime':versions['runtime'],'cancelGuard':True,'staticCtrlFragments':len(fragments),'licences':True}))
