import json
from pathlib import Path
import shutil
import tempfile

repo = Path('/mnt/c/python_workspace/pulsara-agent')
root = Path(tempfile.mkdtemp(prefix='.hostlease-source-', dir='/home/plumwsl'))
for name in ('src', 'tests'):
    shutil.copytree(repo / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
(repo / 'output/phase-b/terminal/writer-loss-fix/source-path.txt').write_text(str(root))
print(json.dumps(dict(snapshot=str(root))))
