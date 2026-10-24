"""Audit only this round's exact homes and observable same-user tagged processes."""

import json
import os
from pathlib import Path

directory = Path(__file__).resolve().parent
homes = json.loads((directory / "audit-input.json").read_text())
assert all(
    Path(home).parent == Path("/home/plumwsl") and Path(home).name.startswith(".g0p5-")
    for home in homes
)
remaining = [home for home in homes if Path(home).exists()]
processes = []
for process in Path("/proc").iterdir():
    if process.name.isdigit() and int(process.name) != os.getpid():
        try:
            arguments = (process / "cmdline").read_bytes()
            environment = (process / "environ").read_bytes()
            if any(home.encode() in arguments for home in homes) or any(
                ("G0_PG5_HOME=" + home).encode() + b"\0" in environment
                for home in homes
            ):
                processes.append(
                    {
                        "pid": int(process.name),
                        "cmdline": arguments.decode(errors="replace"),
                    }
                )
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            pass
result = {
    "passed": not remaining and not processes,
    "checked_homes": len(homes),
    "remaining_homes": remaining,
    "observable_processes": processes,
    "uid": os.getuid(),
}
print(json.dumps(result, indent=2))
raise SystemExit(0 if result["passed"] else 1)
