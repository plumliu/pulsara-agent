"""Finite external pidfd witness; observes only the exact private kernel/home."""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys

cfg=json.loads(Path(sys.argv[1]).read_text())
rows=[json.loads(line) for line in Path(cfg['kernelEvents']).read_text().splitlines()]
started=next(row for row in rows if row['kind']=='original-application-started')
pid=started['kernelPid']
pidfd=os.pidfd_open(pid)
assert Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]==started['kernelStart']
lock=os.open(str(Path(cfg['home'])/'desktop/kernel.lock'),os.O_RDONLY|os.O_CLOEXEC)
def busy():
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return True
    else:fcntl.flock(lock,fcntl.LOCK_UN);return False
before=busy();assert before
result=dict(pid=pid,start=started['kernelStart'],kernelLockBusyBefore=before,pidfdHeldBeforeFault=True)
print(json.dumps(dict(ready=True,**result)),flush=True)
try:
    assert select.select([pidfd],[],[],60)[0], 'exact kernel did not exit'
    assert not busy()
    result.update(exactPidfdExited=True,kernelLockFreeAfter=True)
    process=subprocess.run([cfg['installedRoot']+'/postgres/bin/psql','-XAt','-h',cfg['home']+'/desktop/socket','-U','pulsara_admin','-d','postgres','-c',"SELECT current_setting('data_directory'),system_identifier FROM pg_control_system()"],text=True,capture_output=True,check=True)
    result['privatePgStillLive']=process.stdout.strip()
    Path(sys.argv[2]).write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
finally:os.close(lock);os.close(pidfd)
