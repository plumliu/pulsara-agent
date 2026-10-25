"""Retire only the configured private installed-package/PG test tree."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

output = Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/phase-b-pg")
config = json.loads((output / "lf-fresh-config.json").read_text())
directory = Path(config["directory"])
assert directory.parent == Path("/home/plumwsl") and directory.name.startswith(".pg-lf-")
assert directory.resolve() == directory and directory.stat().st_uid == os.getuid()
home = directory / "home"
data = home / "desktop/postgres"
command = ["/home/plumwsl/workspaces/pulsara_agent/desktop/resources/postgres/bin/pg_ctl", "status", "-D", str(data)]
status = subprocess.run(command, capture_output=True, text=True)
assert status.returncode == 3, (status.returncode, status.stdout, status.stderr)
descriptors = []
for name in ("kernel.lock", "pg-lifetime.lock"):
    descriptor = os.open(home / "desktop" / name, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    descriptors.append(descriptor)
shutil.rmtree(directory)
for descriptor in descriptors:
    os.close(descriptor)
print(json.dumps({"directory": str(directory), "status": status.returncode, "both_locks_available": True, "removed": not directory.exists()}))
