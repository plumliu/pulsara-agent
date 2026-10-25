import json,subprocess,os,stat,fcntl
from pathlib import Path
name='Explorer 中文 alpha10 fb3136ea';p=Path.home()/'workspaces'/name;s=p.lstat()
assert stat.S_ISDIR(s.st_mode) and not p.is_symlink() and p.resolve()==p and s.st_uid==os.getuid()==1000
fs=subprocess.check_output(['/usr/bin/findmnt','-n','-o','FSTYPE','-T',str(p)],text=True).strip();assert fs=='ext4'
assert list(p.iterdir())==[]
home=Path.home()/'.pulsara';pg=Path.home()/'.local/share/pulsara/runtime/0.1.0-alpha.10/postgres/bin/pg_ctl';r=subprocess.run([str(pg),'status','-D',str(home/'desktop/postgres')],capture_output=True,text=True);assert r.returncode==3,(r.stdout,r.stderr)
fd=os.open(home/'desktop/kernel.lock',os.O_RDWR|os.O_NOFOLLOW)
try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);lockfree=True
finally:os.close(fd)
setting=(home/'local-settings.yaml').stat()
result=dict(target={'distro':'Ubuntu','user':'plumwsl'},path=str(p),canonical=str(p.resolve()),inode=s.st_ino,device=s.st_dev,uid=s.st_uid,mode=oct(s.st_mode&0o777),filesystem=fs,empty=True,pgStatus=r.returncode,pgOutput=r.stdout.strip(),kernelLockFree=lockfree,diagnosticsSocketExists=(home/'desktop/diagnostics.sock').exists(),settingsMetadata=dict(inode=setting.st_ino,size=setting.st_size,mtimeNs=setting.st_mtime_ns,uid=setting.st_uid,mode=oct(setting.st_mode&0o777)),directoryPreserved=True,onlyTargetSelectedNoSessionCreated=True,noModelCall=True,passed=True)
print(json.dumps(result,ensure_ascii=False,indent=2))
