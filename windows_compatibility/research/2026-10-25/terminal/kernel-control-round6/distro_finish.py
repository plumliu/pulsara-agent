"""Unregister only the independently guarded test target and remove exact files."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parent
config = json.loads((BASE / "distro-state.json").read_text(encoding="utf-8"))
folder = Path(config["folder"]).resolve()
assert folder.is_relative_to((BASE / "disposable-distro").resolve())
assert folder.name == config["name"] and config["name"].startswith("PulsaraG0-kernel6-")
metadata = BASE / "distro-official-metadata"
metadata.mkdir(exist_ok=True)
for name in ("SHA256SUMS", "SHA256SUMS.gpg"):
    shutil.copyfile(folder / name, metadata / name)
management = subprocess.run(
    [sys.executable, str(BASE / "distro_manage.py"), "unregister"],
    capture_output=True,
    text=True,
    timeout=120,
)
assert management.returncode == 0, (management.stdout, management.stderr)
instance = Path(config["install"]).resolve()
assert instance.parent == folder
if instance.exists():
    assert not list(instance.iterdir()), (
        "Unregister must remove its VHD before directory cleanup."
    )
    instance.rmdir()
for name in ("SHA256SUMS", "SHA256SUMS.gpg", "ubuntu-26.04.1-wsl-amd64.wsl"):
    target = folder / name
    assert target.resolve().parent == folder
    target.unlink()
assert not list(folder.iterdir())
folder.rmdir()
listed = subprocess.run(
    ["C:/Windows/System32/wsl.exe", "--list", "--verbose"],
    capture_output=True,
    check=True,
)
listing = listed.stdout.decode("utf-16-le")
assert config["name"] not in listing
assert "Ubuntu" in listing and "Running" in listing
facts = dict(
    management=[json.loads(line) for line in management.stdout.splitlines()],
    ownRegistrationAbsent=True,
    ownFolderAbsent=not folder.exists(),
    sharedUbuntuStillRunning=True,
    listing=listing,
    freeBytes=shutil.disk_usage(BASE.anchor).free,
)
(BASE / "distro-cleanup.json").write_text(
    json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(facts))
