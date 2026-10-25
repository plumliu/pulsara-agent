"""Remove only the recorded private Electron data directories after owned exits."""
import json
from pathlib import Path
import shutil

out=Path(__file__).resolve().parent
rows=[]
for name in ('quit','quit-2','quit-3','quit-final','host-eof-final','host-eof-exact'):
    cfg=json.loads((out/(name+'-config.json')).read_text())
    target=Path(cfg['windowsHome'])
    assert target.parent==out and target.name.startswith('.bmain-') and target.name.endswith('-electron')
    if target.exists():
        assert target.resolve()==target and not target.is_symlink()
        assert not any(p.is_symlink() or p.is_junction() for p in target.rglob('*'))
        shutil.rmtree(target)
    rows.append(dict(label=name,directory=str(target),removed=not target.exists()))
(out/'windows-cleanup.json').write_text(json.dumps(rows,indent=2))
print(json.dumps(rows))
