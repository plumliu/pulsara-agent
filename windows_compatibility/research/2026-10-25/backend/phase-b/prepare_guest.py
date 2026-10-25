"""Prepare only a private source snapshot and disposable PG home."""
import json
import os
from pathlib import Path
import shutil
import tempfile

directory = Path(tempfile.mkdtemp(prefix=".g0bpg-", dir="/home/plumwsl"))
source = directory / "source"
source.mkdir(mode=0o700)
for name in ("desktop_guest_ops.py",):
    shutil.copy2(Path("/mnt/c/python_workspace/pulsara-agent/src/pulsara_agent") / name, source / name)
(source / "desktop_app").mkdir(mode=0o700)
shutil.copy2(Path("/mnt/c/python_workspace/pulsara-agent/src/pulsara_agent/desktop_app/linux_storage.py"), source / "desktop_app/linux_storage.py")
home = directory / "home"
home.mkdir(mode=0o700)
print(json.dumps({"directory": str(directory), "source": str(source), "home": str(home), "uid": os.getuid()}))
