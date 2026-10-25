from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile

source = Path('/mnt/c/python_workspace/pulsara-agent')
output = source / 'output/phase-b/root'
work = Path(tempfile.mkdtemp(prefix='.phase-b-cache-', dir=Path.home()))
for name in ('mise.toml', 'pyproject.toml', 'uv.lock', '.python-version',
             'desktop/scripts/prepare_resources.py', 'desktop/scripts/test_build_cache.py'):
    target = work / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / name, target)
(work / '.venv').symlink_to('/home/plumwsl/workspaces/pulsara_agent/.venv', target_is_directory=True)
env = dict(os.environ)
env['PATH'] = str(Path.home() / '.local/bin') + ':' + env.get('PATH', '')
uv = subprocess.check_output([str(Path.home() / '.local/bin/mise'), 'which', 'uv'],
                             cwd=source, env=env, text=True).strip()
env['PATH'] = str(Path(uv).parent) + ':' + env['PATH']
with (output / 'build-cache-current.log').open('wb') as log:
    run = subprocess.run([str(work / '.venv/bin/python'), '-m', 'pytest', '-q',
                          'desktop/scripts/test_build_cache.py'], cwd=work, env=env,
                         stdout=log, stderr=subprocess.STDOUT)
result = {'work': str(work), 'returncode': run.returncode,
          'source': str(source), 'scope': 'current recipe; real mise/uv with existing fixture native actions'}
(output / 'build-cache-current.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result))
raise SystemExit(run.returncode)
