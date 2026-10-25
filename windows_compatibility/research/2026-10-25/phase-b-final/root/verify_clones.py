"""Compare this turn's two checkouts without introducing content fingerprints."""
import json
from pathlib import Path
import subprocess

windows = Path('/mnt/c/python_workspace/pulsara-agent')
linux = Path('/home/plumwsl/workspaces/pulsara_agent')
head = subprocess.check_output(['git', '-C', str(linux), 'rev-parse', 'HEAD'], text=True).strip()
assert head == '0ea98e63d90043773f42bd9bb9bbb56951fca8b4'
paths = subprocess.check_output(['git', '-C', str(linux), 'diff', '--name-only'], text=True).splitlines()
assert set(paths) == {'desktop/package.json', 'desktop/package-lock.json', 'frontend/package.json',
                      'frontend/package-lock.json', 'pyproject.toml', 'uv.lock',
                      'src/pulsara_agent/__init__.py', 'mise.toml',
                      'windows_compatibility/README.md',
                      'windows_compatibility/PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md'}
for relative in paths:
    assert (windows / relative).read_bytes().replace(b'\r\n', b'\n') == (linux / relative).read_bytes().replace(b'\r\n', b'\n'), relative
archives = {}
for name in ('windows-shared-recheck', 'phase-b-final'):
    relative = Path('windows_compatibility/research/2026-10-25') / name
    a, b = windows / relative, linux / relative
    files = sorted(path.relative_to(a) for path in a.rglob('*') if path.is_file())
    assert files == sorted(path.relative_to(b) for path in b.rglob('*') if path.is_file())
    for file in files:
        assert (a / file).read_bytes() == (b / file).read_bytes(), str(file)
    archives[name] = {'files': len(files), 'exactBytesEqual': True}
result = {'head': head, 'trackedChanges': paths, 'trackedChangesEqualAfterCheckoutNewlineNormalization': True,
          'archives': archives, 'contentHashesCreated': False, 'passed': True}
Path(__file__).with_name('clone-sync.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
