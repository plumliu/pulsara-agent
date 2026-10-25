"""Copy tests only to ext4; keep the formally installed Python package first."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

repository = Path("/mnt/c/python_workspace/pulsara-agent")
output = repository / "output/windows-g0/pg_admission_r3/provider-acceptance"
directory = Path(tempfile.mkdtemp(prefix=".provider-accept-", dir="/home/plumwsl/workspaces"))
shutil.copytree(repository / "tests", directory / "tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copy2(output / "run_terminal_wsl.py", directory / "run_terminal_wsl.py")
scratch = directory / "scratch"
scratch.mkdir(mode=0o700)
python = "/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/python/bin/python3"
check = "import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/');print('Installed package: '+pulsara_agent.__file__);sys.path.insert(0,sys.argv.pop(1));sys.argv=[sys.argv[1],'--check-imports'];runpy.run_path(sys.argv[0],run_name='__main__')"
environment = dict(os.environ)
environment.pop("PYTHONPATH", None)
checked = subprocess.run([python, "-c", check, str(directory), str(directory / "run_terminal_wsl.py")], cwd=directory, env=environment, capture_output=True, text=True)
facts = {"directory": str(directory), "python": python, "script": str(directory / "run_terminal_wsl.py"),
         "scratch_root": str(scratch), "uid": os.getuid(),
         "imports": {"exit": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr},
         "saved_settings_read": False, "database_connected": False, "provider_called": False}
(output / "installed-harness-preparation.json").write_text(json.dumps(facts, indent=2))
print(json.dumps(facts))
assert checked.returncode == 0
