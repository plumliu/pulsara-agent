from pathlib import Path
import json
import os
import shutil

out=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/desktop/writer-recovery')
root=Path(json.loads((out/'test-stage.json').read_text())['root'])
assert root.parent==Path('/home/plumwsl') and root.name.startswith('.desktop-recovery-')
assert root.resolve()==root and root.stat().st_uid==os.getuid()==1000
live=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name)==os.getpid():continue
    try:
        if p.stat().st_uid!=os.getuid():continue
        if os.readlink(p/'cwd').startswith(str(root)) or os.fsencode(root) in (p/'cmdline').read_bytes():live.append(int(p.name))
    except (FileNotFoundError,ProcessLookupError,PermissionError):pass
assert not live,live
shutil.rmtree(root)
(out/'cleanup.json').write_text(json.dumps({'privateTestSnapshot':str(root),'removed':not root.exists(),'liveSnapshotUsers':live,'formalAppOrPgLifecycleChanged':False,'linuxMainCloneModified':False},indent=2)+'\n')
print('Own inactive ext4 test snapshot removed; no product lifecycle actions.')
