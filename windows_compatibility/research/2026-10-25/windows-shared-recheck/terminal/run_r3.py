"""Selected real PE scopes on the production owners, with an isolated home."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

REPO = Path('/home/plumwsl/workspaces/pulsara_agent')
OUTPUT = Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/terminal')
HELPER = Path('/mnt/c/python_workspace/pulsara-agent/desktop/resources/windows-interop/pulsara-interop.exe')
CHILD = OUTPUT/'interop-child.exe'
os.chdir(REPO)
os.umask(0o077)
assert HELPER.is_file() and CHILD.is_file()
root = Path(tempfile.mkdtemp(prefix='.wsr-pe-', dir='/home/plumwsl'))
home = root/'home'
home.mkdir(mode=0o700)
nodes = [
    'test_normal_no_pipe_descendant_retains_live_slot',
    'test_cancel_after_linux_root_exits',
    'test_input_and_eof',
    'test_same_registry_ab_isolation',
    'test_hook_normal_and_timeout',
    'test_mcp_sdk_call_cancel_preserves_session_then_close',
    'test_real_mcp_supervisor_closes_its_stdio_scope',
]
command = [sys.executable, '-m', 'pytest', '-vv', '-s', '--basetemp', str(root/'pytest'),
           '--junitxml', str(OUTPUT/'r3.xml'),
           *(f'tests/windows_interop_integration.py::{name}' for name in nodes)]
source = importlib.util.find_spec('pulsara_agent').origin
assert Path(source).resolve() == REPO/'src/pulsara_agent/__init__.py', source
assert HELPER.is_file() and CHILD.is_file()
facts = dict(started=datetime.now(timezone.utc).isoformat(), repo=str(REPO),
             python=sys.executable, source=source,
             commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
             dirty=subprocess.check_output(['git','status','--short'], text=True),
             privateRoot=str(root), home=str(home), command=command, CI='true',
             helper=str(HELPER), child=str(CHILD), formalPgOperation='none', settingsOperation='none')
(OUTPUT/'r3-command.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
environment = dict(os.environ, CI='true', PULSARA_HOME=str(home),
                   PULSARA_TEST_INTEROP_HELPER=str(HELPER), PULSARA_TEST_INTEROP_CHILD=str(CHILD))
environment.pop('PYTHONPATH', None)
try:
    with (OUTPUT/'r3.log').open('w') as log:
        result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
    facts.update(exitCode=result.returncode, finished=datetime.now(timezone.utc).isoformat())
    xml = ElementTree.parse(OUTPUT/'r3.xml').getroot()
    facts['suites'] = [dict(node.attrib) for node in xml.iter('testsuite')]
    scope_facts = []
    for line in (OUTPUT/'r3.log').read_text().splitlines():
        start = line.find('{"case":')
        if start >= 0:
            scope_facts.append(json.JSONDecoder().raw_decode(line[start:])[0])
    facts['scopes'] = scope_facts
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
    facts['remainingScopePaths'] = [str(p) for p in home.glob('desktop/i-*')]
    # An unjoined scope is evidence/owned state, not permission to delete it.
    if not remaining and not facts['remainingScopePaths']:
        assert root.parent == Path('/home/plumwsl') and root.name.startswith('.wsr-pe-')
        assert root.resolve() == root and root.stat().st_uid == os.getuid()
        shutil.rmtree(root)
    facts['privateRootRemoved'] = not root.exists()
    (OUTPUT/'r3-result.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
sys.exit(facts['exitCode'])
