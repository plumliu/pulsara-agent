from pathlib import Path
import json
import shutil
import subprocess
import tempfile

source = Path('/mnt/c/python_workspace/pulsara-agent/desktop')
repo = Path('/home/plumwsl/workspaces/pulsara_agent')
node = Path('/home/plumwsl/.local/share/mise/installs/node/22.23.3/bin/node')
output = Path(__file__).resolve().parent
work = Path(tempfile.mkdtemp(prefix='phase-b-desktop-', dir=repo / 'output'))
for name in ('src', 'scripts'):
    shutil.copytree(source / name, work / name)
shutil.copy2(source / 'tsconfig.json', work / 'tsconfig.json')
(work / 'node_modules').symlink_to(repo / 'desktop/node_modules', target_is_directory=True)
checks = []
for name, command in (
    ('build', [str(node), str(work/'node_modules/typescript/bin/tsc')]),
    ('tests', [str(node), '--test', *sorted(str(work/'dist'/p.with_suffix('.js').name) for p in (work/'src').glob('*.test.ts'))]),
):
    result = subprocess.run(command, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (output / f'desktop-linux-{name}.log').write_bytes(result.stdout)
    checks.append({'name': name, 'returncode': result.returncode})
    if result.returncode:
        print(result.stdout.decode('utf-8', errors='replace'))
        break
report = {'work':str(work),'checks':checks}
(output/'desktop-linux-result.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
raise SystemExit(checks[-1]['returncode'])
