"""Explicit alpha10 active-process before/after snapshots only; no sleep or lifecycle actions."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

OUT=Path(__file__).resolve().parent
COMMON=OUT
spec=importlib.util.spec_from_file_location('native_sleep_observer',COMMON/'native_observe.py')
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
PYTHON='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3'


def tun():
    script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $a=@(Get-NetAdapter -IncludeHidden | Where-Object {$_.Name -eq 'tun0' -or $_.InterfaceDescription -eq 'sing-tun'} | Select-Object Name,InterfaceDescription,ifIndex,Status); ConvertTo-Json -InputObject $a -Compress"
    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    return json.loads(subprocess.check_output([str(shell),'-NoProfile','-NonInteractive','-Command',script],text=True,encoding='utf-8-sig'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['before','after'])
    parser.add_argument('--port',required=True,type=int)
    parser.add_argument('--session-id',required=True)
    parser.add_argument('--main-pid',required=True,type=int)
    parser.add_argument('--kernel-pid',required=True,type=int)
    parser.add_argument('--kernel-start')
    parser.add_argument('--pg-pid',type=int)
    parser.add_argument('--pg-start')
    args=parser.parse_args()
    destination=OUT/(args.stage+'.json');assert not destination.exists(),'Keep original observation'
    before=json.loads((OUT/'before.json').read_text(encoding='utf-8')) if args.stage=='after' else None
    target={'port':args.port,'sessionId':args.session_id,'mainPid':args.main_pid,'kernelPid':args.kernel_pid,
        'kernelStart':args.kernel_start,'pgPid':args.pg_pid,'pgStart':args.pg_start}
    if before:assert target==before['target']
    environment={k:v for k,v in os.environ.items() if k.upper()!='WSLENV'}
    guest_path='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/root/active-sleep/guest_observe.py'
    guest=subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/wsl.exe'),'-d','Ubuntu','-u','plumwsl',
        '--cd','/home/plumwsl','--exec',PYTHON,'-I',guest_path,'--port',str(args.port),
        '--session-id',args.session_id,'--kernel-pid',str(args.kernel_pid)],capture_output=True,text=True,encoding='utf-8',env=environment)
    (OUT/f'guest-{args.stage}.stdout.log').write_text(guest.stdout,encoding='utf-8')
    (OUT/f'guest-{args.stage}.stderr.log').write_text(guest.stderr,encoding='utf-8')
    guest.check_returncode();linux=json.loads(guest.stdout)
    assert linux['kernel']['pid']==args.kernel_pid and linux['version']=='0.1.0a10'
    if args.kernel_start:assert linux['kernel']['starttime']==args.kernel_start
    if args.pg_pid:assert linux['postgres']['pid']==args.pg_pid
    if args.pg_start:assert linux['postgres']['starttime']==args.pg_start
    certificate=before['linux']['http']['certificatePem'] if before else linux['http']['certificatePem']
    http_probe=subprocess.run([str(native.NODE),str(COMMON/'https_observe.cjs')],
        input=json.dumps({'origin':linux['http']['origin'],'certificatePem':certificate}),
        capture_output=True,text=True,encoding='utf-8')
    (OUT/f'http-{args.stage}.stdout.log').write_text(http_probe.stdout,encoding='utf-8')
    (OUT/f'http-{args.stage}.stderr.log').write_text(http_probe.stderr,encoding='utf-8')
    http=json.loads(http_probe.stdout)
    host=native.windows(args.port,int(before['windows']['eventBookmark']) if before else 0)
    assert host['main']['pid']==args.main_pid
    tunnels=tun()
    facts={'stage':args.stage,'utc':datetime.now(timezone.utc).isoformat(),'target':target,
        'linux':linux,'windows':host,'http':http,'httpExit':http_probe.returncode,'tun':tunnels,
        'readOnly':True,'settingsRead':False,'sleepInvoked':False,'businessOperationReplayed':False}
    valid=http_probe.returncode==0 and http.get('authorized') and http.get('fullCertificateSame') and http.get('health')=={'status':'ready'}
    if before:
        previous=before['linux'];old_writer=previous['writer'];writer=linux['writer']
        facts['comparisons']={
            'sameWindowsMain':all(host['main'][key]==before['windows']['main'][key] for key in ('pid','creationFileTime','image')),
            'sameKernel':linux['kernel']==previous['kernel'],
            'samePostgres':linux['postgres']==previous['postgres'] and linux['pgFacts']==previous['pgFacts'],
            'sameBoot':linux['bootId']==previous['bootId'],
            'sameBusyLockInodes':linux['locks']==previous['locks'],
            'sameControlSocket':linux['diagnosticsSocketInode']==previous['diagnosticsSocketInode'],
            'sameHttpListenerAndCertificate':linux['http']==previous['http'],
            'sameSessionAndWorkspace':linux['session'][0]==previous['session'][0] and linux['session'][2]==previous['session'][2],
            'savedTurnsRemain':all(row in linux['turns'] for row in previous['turns']),
            'writerGenerationNondecreasing':writer['generation']>=old_writer['generation'],
            'currentWriterLeaseValid':writer['leaseValid'] is True,
            'sameGenerationRetainsOwner':writer['generation']!=old_writer['generation'] or writer['owner']==old_writer['owner'],
            'advancedGenerationChangesOwner':writer['generation']==old_writer['generation'] or writer['owner']!=old_writer['owner'],
            'originalCertificateAndHealthValid':bool(valid),
            'tunStillUp':bool(tunnels) and all(value['Status']=='Up' for value in tunnels),
            'sleepEventsRecorded':len(host['powerEvents'])>=2,
        }
        facts['writerObservation']='generation advanced' if writer['generation']>old_writer['generation'] else 'same generation valid at observation'
        facts['beforeLeaseExpiredByAfterObservation']=bool(old_writer['leaseExpires']) and datetime.fromisoformat(old_writer['leaseExpires'])<datetime.fromisoformat(writer['databaseObservedAt'])
        facts['allComparisonsPassed']=all(facts['comparisons'].values())
    else:
        facts['beforeReady']=bool(valid) and linux['writer']['leaseValid'] is True and bool(tunnels) and all(value['Status']=='Up' for value in tunnels) and all(row[1]!='RUNNING' for row in linux['turns'])
    destination.write_text(json.dumps(facts,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert facts.get('allComparisonsPassed',facts.get('beforeReady')),str(destination)
    print(json.dumps({'stage':args.stage,'checksPassed':True,'file':str(destination),'powerEvents':len(host['powerEvents'])}))


if __name__=='__main__':main()
