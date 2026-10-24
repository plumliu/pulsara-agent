"""Inspect exact finished OAuth homes and the dedicated guest process path only."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
homes = json.loads((ROOT / "audit-input.json").read_text())
assert all(
    Path(home).parent == Path("/home/plumwsl")
    and Path(home).name.startswith(".g0oauth6-")
    for home in homes
)
remaining_homes = [home for home in homes if Path(home).exists()]
processes = []
needle = (str(ROOT / "guest.py")).encode()
for process in Path("/proc").iterdir():
    if process.name.isdigit() and int(process.name) != os.getpid():
        try:
            arguments = (process / "cmdline").read_bytes()
            if needle in arguments:
                processes.append({"pid": int(process.name), "guest_present": True})
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
result = {
    "passed": not remaining_homes and not processes,
    "checked_home_count": len(homes),
    "remaining_homes": remaining_homes,
    "observable_guest_processes": processes,
    "uid": os.getuid(),
}
print(json.dumps(result, indent=2))
raise SystemExit(0 if result["passed"] else 1)
