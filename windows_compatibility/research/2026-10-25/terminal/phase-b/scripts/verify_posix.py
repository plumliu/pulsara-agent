"""Test immutable Windows-source snapshots in private WSL ext4 directories."""

import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
code = r"""
import json,os,shutil,subprocess,tempfile
from pathlib import Path
source=Path('/mnt/c/python_workspace/pulsara-agent')
root=Path(tempfile.mkdtemp(prefix='.pulsara-terminal-fix-',dir='/home/plumwsl'))
assert root.parent==Path('/home/plumwsl') and root.name.startswith('.pulsara-terminal-fix-')
python='/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python'
results=[]
try:
 for phase in ('baseline','candidate'):
  snapshot=root/phase; snapshot.mkdir()
  shutil.copytree(source/'src',snapshot/'src')
  shutil.copytree(source/'tests',snapshot/'tests')
  shutil.copyfile(source/'pyproject.toml',snapshot/'pyproject.toml')
  # A Windows checkout vendors rg.exe; this Linux snapshot needs the existing
  # locked Linux resource, copied read-only from the Linux development checkout.
  rg=Path('/home/plumwsl/workspaces/pulsara_agent/src/pulsara_agent/_vendor/ripgrep/rg')
  assert rg.is_file()
  (snapshot/'src/pulsara_agent/_vendor/ripgrep').mkdir(parents=True,exist_ok=True)
  shutil.copy2(rg,snapshot/'src/pulsara_agent/_vendor/ripgrep/rg')
  if phase=='baseline':
   original=subprocess.run(['git','-C',str(source),'show','HEAD:src/pulsara_agent/terminal_process/manager.py'],capture_output=True,check=True).stdout
   (snapshot/'src/pulsara_agent/terminal_process/manager.py').write_bytes(original)
  env={**os.environ,'PYTHONPATH':str(snapshot/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
  origin=subprocess.run([python,'-B','-c','import pulsara_agent.terminal_process.manager as m; print(m.__file__)'],cwd=snapshot,env=env,capture_output=True,text=True,check=True)
  assert str(snapshot/'src') in origin.stdout
  tests=['tests/test_terminal_startup_cleanup.py']
  if phase=='candidate': tests+=['tests/test_round2_terminal_output.py','tests/test_round2_terminal_monitor.py','tests/test_round2_terminal_environment.py']
  done=subprocess.run([python,'-B','-m','pytest',*tests,'-q'],cwd=snapshot,env=env,capture_output=True,text=True)
  results.append(dict(phase=phase,exit=done.returncode,origin=origin.stdout,stdout=done.stdout,stderr=done.stderr))
finally:
 shutil.rmtree(root)
print(json.dumps(dict(results=results,snapshotRemoved=not root.exists())))
"""
if "--candidate-only" in sys.argv:
    code = code.replace(
        "for phase in ('baseline','candidate'):", "for phase in ('candidate',):"
    )
completed = subprocess.run(
    [
        "C:/Windows/System32/wsl.exe",
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--cd",
        "/home/plumwsl/workspaces/pulsara_agent",
        "--exec",
        "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
        "-B",
        "-c",
        code,
    ],
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=180,
)
assert completed.returncode == 0, completed.stderr
result = json.loads(completed.stdout)
(
    BASE
    / (
        "posix-results-final.json"
        if "--candidate-only" in sys.argv
        else "posix-results.json"
    )
).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
for row in result["results"]:
    print(
        json.dumps(
            dict(
                phase=row["phase"],
                exit=row["exit"],
                stdout=row["stdout"][-2000:],
                stderr=row["stderr"],
            )
        )
    )
if "--candidate-only" not in sys.argv:
    assert result["results"][0]["exit"] != 0
assert result["results"][-1]["exit"] == 0
assert result["snapshotRemoved"]
