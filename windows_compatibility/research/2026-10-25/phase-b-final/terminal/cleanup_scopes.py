import json
import os
from pathlib import Path
import shutil

out=Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal')
records=[]
for name in ('pin-before-send','received-before-bind','both-observers-lost','mcp-owner-lost','hook-owner-lost'):
    value=json.loads((out/(name+'-result.json')).read_text())
    assert value['status']=='passed' and value['remainingScopePaths']==[]
    root=Path(value['root'])
    assert root.parent==Path('/home/plumwsl') and root.name.startswith('.bfin-') and root.resolve()==root and root.stat().st_uid==os.getuid()
    remaining=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name)==os.getpid():continue
        try:
            if str(root).encode() in (entry/'cmdline').read_bytes():remaining.append(int(entry.name))
        except (FileNotFoundError,PermissionError,ProcessLookupError):pass
    assert not remaining,remaining
    assert not list((root/'home').glob('desktop/i-*'))
    shutil.rmtree(root)
    records.append(dict(case=name,root=str(root),removed=not root.exists(),remainingExactOwned=remaining,externalCleanupOnly=value.get('externalCleanupOnly',False)))
(out/'scope-cleanup.json').write_text(json.dumps(records,indent=2))
print(json.dumps(records))
