"""Small archival index: keep raw records once, not every temporary file."""
import json
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
repository = root.parents[2]
assert repository.name == 'pulsara-agent'
destination = repository / 'windows_compatibility/research/2026-10-25/phase-b-final/backend'
assert not destination.exists()
cases = ('pre-cancel', 'start-loss', 'stop-loss', 'init-loss', 'readiness', 'first-pre-cancel', 'first-init-loss')
for name in cases:
    records = root / name / 'guest-records'
    bundle = {str(path.relative_to(records)): path.read_text(encoding='utf-8') for path in sorted(records.rglob('*')) if path.is_file()}
    (root / name / 'guest-records.json').write_text(json.dumps(bundle, indent=2), encoding='utf-8')
paths = [path for path in root.iterdir() if path.is_file() and path.name not in {'tool-home.json'}]
for name in cases:
    paths.extend(path for path in (root / name).iterdir() if path.is_file())
relative = sorted(str(path.relative_to(root)).replace('\\', '/') for path in paths)
relative.append('archive-files.txt')
(root / 'archive-files.txt').write_text('\n'.join(sorted(relative)) + '\n', encoding='utf-8')
for name in relative:
    source, target = root / name, destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
print(json.dumps({'archived_files': len(relative), 'destination': str(destination)}))
