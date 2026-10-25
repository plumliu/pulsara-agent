"""Read exact original process/lock facts; never starts/stops a product owner."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

home=Path('/home/plumwsl/.pulsara')
def identity(pid):
    p=Path('/proc')/str(pid)
    if not p.exists():return {'pid':pid,'exists':False}
    fields=(p/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'exists':True,'state':fields[0],'starttime':fields[19],
            'namespace':os.readlink(p/'ns/pid'),'exe':os.readlink(p/'exe')}

locks=[]
for name in ('kernel.lock','pg-lifetime.lock'):
    fd=os.open(home/'desktop'/name,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        facts=os.fstat(fd);busy=False
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:busy=True
        locks.append({'name':name,'inode':facts.st_ino,'busy':busy})
    finally:os.close(fd)
candidates=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit():continue
    try:
        if p.stat().st_uid!=os.getuid():continue
        command=(p/'cmdline').read_bytes().split(b'\0')
        if b'pulsara_agent.desktop_runtime' not in command or b'--wait-owner' in command:continue
        environment=dict(s.split(b'=',1) for s in (p/'environ').read_bytes().split(b'\0') if b'=' in s)
        if environment.get(b'PULSARA_HOME')==os.fsencode(home):candidates.append(identity(int(p.name)))
    except (FileNotFoundError,ProcessLookupError,PermissionError):pass
print(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'originalKernel':identity(468),
    'originalPostgres':identity(498),'locks':locks,'formalKernelCandidates':candidates,
    'diagnosticsSocketExists':(home/'desktop/diagnostics.sock').exists(),
    'settingsRead':False,'pgConnected':False,'productLifecycleAction':False},indent=2))
