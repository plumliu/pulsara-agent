"""Read-only final audit for the exact homes recorded by this probe."""

import json
import os
from pathlib import Path

root = Path(__file__).parent
homes = json.loads((root / "audit-input.json").read_text())
assert homes and all(
    (h.startswith("/tmp/g0d2-") and Path(h).parent == Path("/tmp"))
    or (
        h.startswith("/home/plumwsl/.g0d2-") and Path(h).parent == Path("/home/plumwsl")
    )
    for h in homes
)
remaining = [h for h in homes if Path(h).exists()]
processes = []
for p in Path("/proc").iterdir():
    if not p.name.isdigit():
        continue
    try:
        cmd = (p / "cmdline").read_bytes()
        env = (p / "environ").read_bytes()
        hits = [
            h
            for h in homes
            if h.encode() in cmd or ("G0_HOOK_HOME=" + h).encode() in env
        ]
        if hits:
            processes.append(
                {
                    "pid": int(p.name),
                    "homes": hits,
                    "cmdline": cmd.decode(errors="replace").replace("\0", " "),
                }
            )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
result = {
    "uid": os.getuid(),
    "homes": homes,
    "remaining_homes": remaining,
    "owned_processes": processes,
    "passed": not remaining and not processes,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
assert result["passed"], result
