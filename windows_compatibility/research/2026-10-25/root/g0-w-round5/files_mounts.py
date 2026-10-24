"""Two real bind mounts in a private mount namespace; drop to selected user before admission."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import shutil

assert os.getuid() == 0
assert os.readlink("/proc/self/ns/mnt") != os.readlink("/proc/1/ns/mnt")
root = Path(
    tempfile.mkdtemp(prefix="g0-private-mount-", dir="/home/plumwsl/workspaces")
)
os.chown(root, 1000, 1000)
for name in ("source", "drvfs-target", "ext4-target"):
    p = root / name
    p.mkdir()
    os.chown(p, 1000, 1000)
subprocess.run(["mount", "--bind", "/mnt/c", str(root / "drvfs-target")], check=True)
subprocess.run(
    ["mount", "--bind", str(root / "source"), str(root / "ext4-target")], check=True
)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.environ["HOME"] = "/home/plumwsl"
here = Path(__file__).parent
spec = importlib.util.spec_from_file_location("files", here / "files_guest.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = {
    "uid": os.getuid(),
    "root": str(root),
    "privateMountNamespace": True,
    "cases": [],
}
try:
    for name in ("drvfs-target", "ext4-target"):
        path = root / name
        mount = json.loads(
            subprocess.check_output(
                ["findmnt", "-J", "-T", str(path), "-o", "TARGET,FSTYPE,SOURCE"],
                text=True,
            )
        )["filesystems"][0]
        try:
            _, fd, _ = module.bind(str(path))
            os.close(fd)
            raise AssertionError("mount substitution accepted")
        except ValueError as exc:
            result["cases"].append({"name": name, "mount": mount, "rejected": str(exc)})
finally:
    shutil.rmtree(module.HOME)
(here / "files-mounts.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result))
# The namespace is destroyed on exit; cleanup of the ordinary directories is
# performed later from a fresh user process, after verifying neither is mounted.
