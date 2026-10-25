"""Two explicit read-only snapshots; never invokes sleep or application lifecycle."""
import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import base64

OUT=Path(__file__).resolve().parent
REPO=OUT.parents[3]
GUEST='/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/installed-sleep/guest_observe.py'
PYTHON='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/python/bin/python3'
NODE=Path('C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe')


def native_identity(pid,image,expected):
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.GetProcessTimes.argtypes=[w.HANDLE,*[c.POINTER(w.FILETIME)]*4]
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.OpenProcess(0x1000|0x100000,False,pid)
    if not handle:raise c.WinError(c.get_last_error())
    try:
        times=[w.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle,*[c.byref(value) for value in times])
        creation=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        # WMI's creation timestamp has microsecond, not FILETIME 100ns, precision.
        assert creation//10==int(expected)//10
        buffer=c.create_unicode_buffer(32768);size=w.DWORD(len(buffer))
        assert kernel.QueryFullProcessImageNameW(handle,0,buffer,c.byref(size))
        assert Path(buffer.value).resolve()==image.resolve()
        alive=kernel.WaitForSingleObject(handle,0)==258
        assert alive
        return {'pid':pid,'creationFileTime':creation,'image':buffer.value,'alive':alive}
    finally:kernel.CloseHandle(handle)


def windows(port,bookmark):
    image=Path(os.environ['LOCALAPPDATA'])/'Programs/pulsara/pulsara.exe'
    literal=str(image).replace("'","''")
    script=f"""$ErrorActionPreference='Stop'
$items=@(Get-CimInstance Win32_Process -Filter "name='pulsara.exe'" | Where-Object {{$_.ExecutablePath -ieq '{literal}'}} | ForEach-Object {{[pscustomobject]@{{pid=$_.ProcessId;parent=$_.ParentProcessId;auxiliary=[bool]($_.CommandLine -match '--type=');maintenance=[bool]($_.CommandLine -match '--pulsara-maintenance-check');creation=[string]$_.CreationDate.ToUniversalTime().ToFileTimeUtc()}}}})
$ids=@($items | ForEach-Object {{$_.pid}})
$tcp=@(Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {{$ids -contains $_.OwningProcess -and ($_.RemotePort -eq {port} -or $_.LocalPort -eq {port}) -and ($_.RemoteAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '127.0.0.1')}} | Select-Object OwningProcess,LocalAddress,LocalPort,RemoteAddress,RemotePort,State)
$last=(Get-WinEvent -LogName System -MaxEvents 1).RecordId
$power=@()
if ({bookmark} -gt 0) {{
 $query="*[System[(EventRecordID > {bookmark}) and (Provider[@Name='Microsoft-Windows-Kernel-Power'] or Provider[@Name='Microsoft-Windows-Power-Troubleshooter']) and (EventID=1 or EventID=42 or EventID=107 or EventID=506 or EventID=507)]]"
 $power=@(Get-WinEvent -LogName System -FilterXPath $query -ErrorAction SilentlyContinue | ForEach-Object {{[pscustomobject]@{{id=$_.Id;recordId=$_.RecordId;provider=$_.ProviderName;utc=$_.TimeCreated.ToUniversalTime().ToString('o')}}}})
}}
[pscustomobject]@{{processes=$items;loopback=$tcp;eventBookmark=$last;powerEvents=$power}} | ConvertTo-Json -Depth 5 -Compress
"""
    encoded=base64.b64encode(script.encode('utf-16le')).decode()
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=json.loads(subprocess.check_output([str(powershell),'-NoProfile','-NonInteractive','-EncodedCommand',encoded],text=True,encoding='utf-8-sig'))
    identities=[]
    for value in result.pop('processes'):
        identities.append({**native_identity(value['pid'],image,value['creation']),'parent':value['parent'],'auxiliary':value['auxiliary'],'maintenance':value['maintenance']})
    mains=[value for value in identities if not value['auxiliary'] and not value['maintenance']]
    assert len(mains)==1
    return {**result,'main':mains[0],'processes':identities}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('before','after'))
    parser.add_argument('--port',type=int,required=True)
    args=parser.parse_args();assert sys.platform=='win32' and 1<=args.port<=65535
    destination=OUT/(args.stage+'.json');assert not destination.exists(),'Never overwrite original observation'
    before=json.loads((OUT/'before.json').read_text(encoding='utf-8')) if args.stage=='after' else None
    if before:assert before['port']==args.port
    environment={key:value for key,value in os.environ.items() if key.upper()!='WSLENV'}
    guest=subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/wsl.exe'),'-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl','--exec',PYTHON,'-I',GUEST,'--port',str(args.port)],env=environment,capture_output=True,text=True,encoding='utf-8',check=True)
    linux=json.loads(guest.stdout)
    certificate=before['linux']['http']['certificatePem'] if before else linux['http']['certificatePem']
    probe=subprocess.run([str(NODE),str(OUT/'https_observe.cjs')],input=json.dumps({'origin':linux['http']['origin'],'certificatePem':certificate}),capture_output=True,text=True,encoding='utf-8')
    http=json.loads(probe.stdout)
    host=windows(args.port,int(before['windows']['eventBookmark']) if before else 0)
    result={'stage':args.stage,'utc':datetime.now(timezone.utc).isoformat(),'port':args.port,'linux':linux,'windows':host,'http':http,'httpExit':probe.returncode,'readOnly':True,'sleepInvoked':False,'settingsRead':False,'businessOperationReplayed':False}
    if before:
        result['comparisons']={
            'sameWindowsMain':all(host['main'][key]==before['windows']['main'][key] for key in ('pid','creationFileTime','image')),
            'sameKernel':linux['kernel']==before['linux']['kernel'],
            'samePostgres':linux['postgres']==before['linux']['postgres'] and linux['pgFacts']==before['linux']['pgFacts'],
            'sameNamespaceAndBoot':linux['bootId']==before['linux']['bootId'],
            'sameBusyLocks':linux['locks']==before['linux']['locks'],
            'sameControlSocket':linux['diagnosticsSocketInode']==before['linux']['diagnosticsSocketInode'],
            'samePublicCertificate':linux['http']['certificatePem']==before['linux']['http']['certificatePem'],
            'sameHttpListener':linux['http']['listenInode']==before['linux']['http']['listenInode'],
            'sameOriginalSession':linux['session']==before['linux']['session'] and linux['turns']==before['linux']['turns'],
            'validatedOriginalCertificateAndHealth':probe.returncode==0 and http.get('authorized') and http.get('fullCertificateSame') and http.get('health')=={'status':'ready'},
        }
        result['allComparisonsPassed']=all(result['comparisons'].values())
    else:result['beforeReady']=probe.returncode==0 and http.get('authorized') and http.get('fullCertificateSame') and http.get('health')=={'status':'ready'}
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert result.get('allComparisonsPassed',result.get('beforeReady')),str(destination)
    print(json.dumps({'stage':args.stage,'file':str(destination),'checksPassed':True,'powerEvents':len(host['powerEvents'])}))


if __name__=='__main__':main()
