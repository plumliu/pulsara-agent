"""Read-only alpha9 input/wheel/static/licence audit, with original recipe pins."""
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path
import subprocess
import sys

repo=Path('/mnt/c/python_workspace/pulsara-agent')
out=repo/'output/phase-b/desktop/alpha9-build'
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
recovery_fix=[]
for relative in ('conversation_kernel/host.py','desktop_app/session_controller.py','desktop_app/renderer_bridge.py','desktop_app/http_server.py'):
 assert (package/relative).read_text()==(snapshot/'src/pulsara_agent'/relative).read_text()
 recovery_fix.append({'file':relative,'sameAsFrozenSnapshot':True})
assert 'def writer_unavailable' in (package/'conversation_kernel/host.py').read_text()
assert '_writer_renewal_failure' in (package/'conversation_kernel/host.py').read_text()
assert 'self._recover_writer_owner' in (package/'desktop_app/session_controller.py').read_text()
assert 'SESSION_WRITER_UNAVAILABLE' in (package/'desktop_app/renderer_bridge.py').read_text()
assert 'retryable=exc.public_code != "SESSION_RECOVERY_CLOSE_FAILED"' in (package/'desktop_app/http_server.py').read_text()
assert any('连接恢复未完成' in path.read_text() for path in (package/'desktop_app/static/assets').glob('index-*.js'))
shortcut_fix=[]
for path in (package/'desktop_app/static/assets').glob('index-*.js.map'):
 mapping=json.loads(path.read_text())
 for name,content in zip(mapping['sources'],mapping['sourcesContent']):
  if name.endswith(('/app/pulsara-app.tsx','/components/session-search-dialog.tsx')):
   relative='frontend/'+name.split('frontend/')[-1] if 'frontend/' in name else 'frontend/'+name.split('../')[-1]
   source=repo/relative
   assert source.is_file(),relative
   assert content.replace('\r\n','\n')==source.read_text().replace('\r\n','\n')
   assert 'keyCode === 229' in content and "getModifierState('AltGraph')" in content and '!event.altKey' in content
   shortcut_fix.append({'source':relative,'sameTextAsFrozenFrontend':True,'compositionAndAltGraphGuard':True})
assert len(shortcut_fix)==2,shortcut_fix
emitted_guards=[]
for path in (package/'desktop_app/static/assets').glob('index-*.js'):
 text=path.read_text()
 for found in re.finditer('AltGraph',text):
  fragment=text[max(0,found.start()-125):found.start()+100]
  assert 'altKey' in fragment
  emitted_guards.append(fragment)
assert len(emitted_guards)==2
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
assert versions=={'runtime':'0.1.0a9','distribution':'0.1.0a9','python':'3.12.15'}
resource_versions=json.loads((resources/'versions.json').read_text())
assert resource_versions['postgresql']=='17.9' and resource_versions['pg_search']=='0.26.1' and resource_versions['vector']=='0.8.1'
result={'passed':True,'snapshot':{'start':stage['snapshotStarted'],'finish':stage['snapshotFinished'],'source':stage['sourceRepository']},'versions':versions,'resourceVersions':resource_versions,
 'rootFix':{'wheelSource':str(package/'conversation_kernel/runner.py'),'sameTextAsCopiedFrozenSource':True,'cancelBeforeReprepareGuard':excerpt,'typedHeadStaleClass':True},
 'staticCtrl':fragments,'shortcutFix':shortcut_fix,'writerRecovery':recovery_fix,'shortcutFixEmittedJs':emitted_guards,'licenses':{'requiredPresent':list(required),'files':len(list(licenses.iterdir())),'pinnedUpstreamAgplAndCorrespondingSourceVerified':True},
 'recipe':'prepare_wsl_payload.package called existing prepare_resources.validate_resources before tar; native dependency resources reused, current application wheel rebuilt',
 'databaseConnected':False,'installedRuntimeModified':False}
(out/'wheel-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'runtime':versions['runtime'],'cancelGuard':True,'staticCtrlFragments':len(fragments),'licences':True}))
