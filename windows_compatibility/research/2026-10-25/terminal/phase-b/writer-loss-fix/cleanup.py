"""Verify only this probe's exact homes and snapshot, then remove the snapshot."""
import json
import os
from pathlib import Path
import shutil

evidence = Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/writer-loss-fix')
homes = []
for path in sorted(evidence.glob('*-result.json')):
    value = json.loads(path.read_text())
    if 'config' not in value:
        continue
    root = Path(value['config']['directory'])
    assert root.parent == Path('/home/plumwsl') and root.name.startswith('.hostlease-')
    item = dict(result=path.name, directory=str(root), exists=root.exists(),
                originalPgResetCompleted=value.get('pgResetCompleted', False))
    assert not item['exists'] and item['originalPgResetCompleted'], item
    homes.append(item)
snapshot = Path((evidence / 'source-path.txt').read_text())
assert snapshot.parent == Path('/home/plumwsl') and snapshot.name.startswith('.hostlease-source-')
assert snapshot.resolve() == snapshot and snapshot.stat().st_uid == os.getuid()
needles = [str(snapshot).encode(), *(item['directory'].encode() for item in homes)]
remaining = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        argv = (entry / 'cmdline').read_bytes()
        cwd = (entry / 'cwd').resolve()
        if any(needle in argv for needle in needles) or cwd == snapshot:
            remaining.append(dict(pid=int(entry.name), starttime=(entry/'stat').read_text().split()[21], argv=argv.decode(errors='replace')))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
assert not remaining, remaining
shutil.rmtree(snapshot)
result = dict(homes=homes, remainingExactOwned=remaining, snapshot=str(snapshot), snapshotRemoved=not snapshot.exists(), formalClusterOperation='none')
(evidence / 'cleanup-final.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result))
