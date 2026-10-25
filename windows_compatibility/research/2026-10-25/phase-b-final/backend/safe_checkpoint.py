"""Verify and retire the first private, stopped setup before manual sleep."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat

output = Path(__file__).resolve().parent
config = json.loads((output / "pre-cancel/config.json").read_text())
home = Path(config["home"])
tools = Path(json.loads((output / "tool-home.json").read_text())["home"])
assert home == Path("/home/plumwsl/.pbfpg-efltozcl")
assert tools == Path("/home/plumwsl/.pbf-tools-jm2i9hih")
assert not home.joinpath("desktop/postgres").exists()
locks = {}
for name in ("kernel.lock", "pg-lifetime.lock"):
    descriptor = os.open(home / "desktop" / name, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks[name] = {"free": True, "inode": os.fstat(descriptor).st_ino}
    finally:
        os.close(descriptor)
remaining, inaccessible = [], []
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        command = proc.joinpath("cmdline").read_bytes()
        if os.fsencode(str(home)) in command or os.fsencode(str(output / "pre-cancel/config.json")) in command:
            remaining.append(int(proc.name))
    except PermissionError:
        inaccessible.append(int(proc.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
assert not remaining
saved = output / "pre-cancel/guest-records"
saved.mkdir(exist_ok=False)
for child in home.iterdir():
    if child.is_file() and (child.suffix in {".json", ".jsonl"} or child.name.startswith("gate-")):
        shutil.copyfile(child, saved / child.name)
for path, prefix in ((home, ".pbfpg-"), (tools, ".pbf-tools-")):
    info = path.lstat()
    assert path.parent == Path("/home/plumwsl") and path.name.startswith(prefix)
    assert path.resolve() == path and info.st_uid == os.getuid() == 1000 and stat.S_IMODE(info.st_mode) == 0o700
    shutil.rmtree(path)
result = {"safe_for_sleep": True, "known_setup_only": True, "target_mutator_never_submitted": True,
          "original_ts_reset_confirmed": True, "data_absent_before_home_removal": True,
          "locks": locks, "remaining_exact_owned_accessible": remaining, "same_uid_command_inaccessible": inaccessible,
          "removed_private_home": str(home), "removed_tools": str(tools),
          "qualification": "Initial deterministic-gate harness failed before the target operation. Original private PG had already stopped normally; original TS resetConfirmed removed stopped data. Known read-only helper/observer were signalled via verified pidfd; no unknown mutator was force-stopped. No claim about unrelated or inaccessible actors."}
(output / "safe-checkpoint.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
