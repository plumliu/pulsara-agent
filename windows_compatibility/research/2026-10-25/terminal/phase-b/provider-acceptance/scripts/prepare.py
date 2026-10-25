"""Private tests-only snapshot; import the installed package before test paths."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

repo = Path("/mnt/c/python_workspace/pulsara-agent")
output = repo / "output/phase-b/terminal/provider-acceptance"
directory = Path(
    tempfile.mkdtemp(prefix=".provider-tasks-", dir="/home/plumwsl/workspaces")
)
scratch = Path(tempfile.mkdtemp(prefix=".pvt-", dir="/home/plumwsl"))
shutil.copytree(
    repo / "tests",
    directory / "tests",
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
)
shutil.copyfile(output / "run_remaining_wsl.py", directory / "run_remaining_wsl.py")
shutil.copyfile(
    repo / "output/windows-g0/pg_admission_r3/provider-acceptance/run_terminal_wsl.py",
    directory / "pg_terminal_adapter.py",
)
python = "/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/python/bin/python3"
bootstrap = "import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/'); print('Installed package: '+pulsara_agent.__file__,flush=True);sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
environment = dict(os.environ)
environment.pop("PYTHONPATH", None)
checked = subprocess.run(
    [
        python,
        "-B",
        "-c",
        bootstrap,
        str(directory),
        str(directory / "run_remaining_wsl.py"),
        "--kind",
        "subagent",
        "--check-imports",
    ],
    cwd=directory,
    env=environment,
    capture_output=True,
    text=True,
)
result = dict(
    directory=str(directory),
    scratch_root=str(scratch),
    python=python,
    script=str(directory / "run_remaining_wsl.py"),
    bootstrap=bootstrap,
    uid=os.getuid(),
    check=dict(exit=checked.returncode, stdout=checked.stdout, stderr=checked.stderr),
    settings_read=False,
    pg_connected=False,
    provider_called=False,
)
(output / "prepared.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False))
assert checked.returncode == 0
