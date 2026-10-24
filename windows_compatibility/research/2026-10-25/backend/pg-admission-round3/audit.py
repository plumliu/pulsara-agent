"""Inspect only this round's exact homes and process markers after cleanup."""

import json
import os
from pathlib import Path

directory = Path(__file__).resolve().parent
homes = json.loads((directory / "audit-input.json").read_text())
assert all(
    Path(home).parent == Path("/home/plumwsl") and Path(home).name.startswith(".g0p3-")
    for home in homes
)
remaining_homes = [home for home in homes if Path(home).exists()]
owned_processes = []
for process in Path("/proc").iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    try:
        commandline = (process / "cmdline").read_bytes()
        environment = (process / "environ").read_bytes()
        if (
            any(home.encode() in commandline for home in homes)
            or b"G0_HOOK_HOME=/home/plumwsl/.g0p3-" in environment
        ):
            owned_processes.append(
                {
                    "pid": int(process.name),
                    "cmdline": commandline.decode(errors="replace"),
                }
            )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
result = {
    "passed": not remaining_homes and not owned_processes,
    "checked_home_count": len(homes),
    "remaining_homes": remaining_homes,
    "owned_processes": owned_processes,
    "uid": os.getuid(),
}
print(json.dumps(result, indent=2))
raise SystemExit(0 if result["passed"] else 1)
