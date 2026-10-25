import json
from pathlib import Path

out=Path(__file__).resolve().parent
def read(name):return json.loads((out/name).read_text())
def lines(name):return [json.loads(line) for line in (out/name).read_text().splitlines()]
scopes=[read(name+'-result.json') for name in ('pin-before-send','received-before-bind','both-observers-lost','mcp-owner-lost','hook-owner-lost')]
assert all(s['status']=='passed' and not s['remainingScopePaths'] for s in scopes)
assert scopes[2]['scope']['joined'] is False and scopes[2]['physicalCompletion'] is False
assert scopes[2]['retainedCapacity']==1 and scopes[2]['externalCleanupOnly']
normal=lines('quit-final-kernel.jsonl');host=lines('quit-final-host.jsonl')
assert sum(row['kind']=='active-original-terminal' for row in normal)==1
before=next(row for row in normal if row['kind']=='original-application-close-enter')['states'][0]
after=next(row for row in normal if row['kind']=='original-application-close-returned')['states'][0]
assert not before['physicalCompletion'] and not before['scopeJoined']
assert after['physicalCompletion'] and after['scopeJoined'] and after['helperExit']==0 and after['directory'] is None
assert not any(after[key] for key in ('readerAlive','watcherAlive','workerAlive'))
kinds=[row['kind'] for row in host]
assert kinds.index('actual-carrier-close')<kinds.index('original-pg-stop-enter')<kinds.index('original-pg-stop-returned')<kinds.index('owned-exe-close')
assert read('quit-final-main-result.json')['result']['code']==0
fault=lines('host-eof-exact-kernel.jsonl');fh=lines('host-eof-exact-host.jsonl')
assert sum(row['kind']=='active-original-terminal' for row in fault)==1
assert sum(row['kind']=='original-application-started' for row in fault)==1
failed=next(row for row in fault if row['kind']=='original-application-close-failed')
assert 'unknown' in failed['error'] and not failed['states'][0]['scopeJoined'] and not failed['states'][0]['physicalCompletion']
assert next(row for row in fault if row['kind']=='original-runtime-system-exit')['code']==1
attempts=[row['frame']['type'] for row in fault if row['kind']=='original-runtime-emit-attempt']
assert 'failed' in attempts and 'stopped' in attempts
assert not any(row['kind'].startswith('original-pg-stop') for row in fh)
windows=read('host-eof-exact-windows-held.json');waits=read('host-eof-exact-windows-waits.json')
assert windows['jobHandleClosedBeforeFault'] and len(windows['held'])==len(waits['waits'])==3
assert waits['waits']==[0,0,0] and waits['signalSent'] is False
linux=read('host-eof-exact-witness.json');assert linux['pidfdHeldBeforeFault'] and linux['exactPidfdExited'] and linux['kernelLockFreeAfter']
clean=read('host-eof-exact-cleanup.json');assert clean['originalWaitOwnerCompleted'] and clean['originalMaintenanceRejected']['postgresExecution']=='not-submitted'
assert clean['before']['stdout'].strip()==linux['privatePgStillLive']
assert clean['originalResetCompleted'] and clean['cleanup']==dict(rootRemoved=True,workspaceRemoved=True,remainingExactOwned=[])
for name in ('quit-2','quit-3','quit-final','host-eof-final','host-eof-exact'):
    value=read(name+'-cleanup.json');assert value['originalResetCompleted'] and value['cleanup']['rootRemoved'] and value['cleanup']['workspaceRemoved'] and not value['cleanup']['remainingExactOwned']
assert all(row['removed'] for row in read('scope-cleanup.json'))
assert all(row['removed'] for row in read('windows-cleanup.json'))
summary=dict(logicalCases=7,allSelectedContractsPassed=True,installedVersion='0.1.0-alpha.10',productionChanges=False,
             scopeCases=[dict(case=s['case'],status=s['status'],scopeJoined=s['scope']['joined'],failure=s['scope']['failure']) for s in scopes],
             normalMain=dict(driverExit=0,exeExit=0,activeScopeJoinedBeforePgStop=True),
             hostEOF=dict(expectedUnknown=True,cleanShutdown=False,originalRuntimeExit=1,windowsHeldMembers=3,allHeldWaitZero=True,externalSignalSent=False,kernelPidfdExited=True,mainDidNotStopPg=True,maintenanceRejected=True),
             originalCleanupCompleted=True,noAutomaticReplay=True)
(out/'final-matrix.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary))
