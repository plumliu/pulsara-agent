import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

configuration = json.loads(Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/phase-b-pg/config.json").read_text())
directory, home = Path(configuration["directory"]), Path(configuration["home"])
assert directory.parent == Path("/home/plumwsl") and directory.name.startswith(".g0bpg-")
assert directory.resolve() == directory and directory.stat().st_uid == os.getuid()
assert home.parent == directory
result = subprocess.run(["/home/plumwsl/workspaces/pulsara_agent/desktop/resources/postgres/bin/pg_ctl", "status", "-D", str(home / "desktop/postgres")], capture_output=True, text=True)
assert result.returncode == 3
descriptors = []
try:
    for name in ("kernel.lock", "pg-lifetime.lock"):
        descriptor = os.open(home / "desktop" / name, os.O_RDWR | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    shutil.rmtree(directory)
finally:
    for descriptor in descriptors:
        os.close(descriptor)
print(json.dumps({"passed": True, "status": result.returncode, "both_locks_available": True, "directory_removed": not directory.exists()}))
