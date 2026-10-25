"""R2: exact shared suites with disposable ext4 home and no service credentials."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path('/home/plumwsl/workspaces/pulsara_agent')
OUTPUT = Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/terminal')
os.chdir(REPO)
os.umask(0o077)
root = Path(tempfile.mkdtemp(prefix='.wsr-term-', dir='/home/plumwsl'))
home = root / 'home'
home.mkdir(mode=0o700)
started = datetime.now(timezone.utc).isoformat()
command = [sys.executable, '-m', 'pytest', '-q', '--basetemp', str(root/'pytest'),
           '--junitxml', str(OUTPUT/'r2.xml'),
           'tests/test_posix_process_group_settlement.py',
           'tests/test_round9_2_hook_subsystem.py',
           'tests/test_mcp_transport_lifecycle.py',
           'tests/test_windows_interop_failures.py',
           'tests/test_windows_interop_relay.py']
source = importlib.util.find_spec('pulsara_agent').origin
assert Path(source).resolve() == REPO/'src/pulsara_agent/__init__.py', source
facts = dict(started=started, repo=str(REPO), python=sys.executable, source=source,
             commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
             dirty=subprocess.check_output(['git','status','--short'], text=True),
             privateRoot=str(root), home=str(home), command=command, CI='true',
             windowsHelperUsed=False, formalPgOperation='none', settingsOperation='none')
(OUTPUT/'r2-command.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
environment = dict(os.environ, CI='true', PULSARA_HOME=str(home))
environment.pop('PYTHONPATH', None)
try:
    with (OUTPUT/'r2.log').open('w') as log:
        result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
    facts.update(exitCode=result.returncode, finished=datetime.now(timezone.utc).isoformat())
finally:
    remaining = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry/'cmdline').read_bytes()
            if str(root).encode() in argv:
                remaining.append(dict(pid=int(entry.name), starttime=(entry/'stat').read_text().split()[21]))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    facts['remainingExactOwned'] = remaining
    if not remaining:
        assert root.parent == Path('/home/plumwsl') and root.name.startswith('.wsr-term-')
        assert root.resolve() == root and root.stat().st_uid == os.getuid()
        shutil.rmtree(root)
    facts['privateRootRemoved'] = not root.exists()
    (OUTPUT/'r2-result.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
sys.exit(facts['exitCode'])
