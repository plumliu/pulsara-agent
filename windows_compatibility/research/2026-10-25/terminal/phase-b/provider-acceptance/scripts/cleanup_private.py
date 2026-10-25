"""Retire only the exact prepared test snapshot and now-empty scratch root."""

import json
import os
from pathlib import Path
import shutil

base = Path(
    "/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/provider-acceptance"
)
prepared = json.loads((base / "prepared.json").read_text())
source, scratch = Path(prepared["directory"]), Path(prepared["scratch_root"])
assert source.parent == Path("/home/plumwsl/workspaces") and source.name.startswith(
    ".provider-tasks-"
)
assert scratch.parent == Path("/home/plumwsl") and scratch.name.startswith(".pvt-")
for path in (source, scratch):
    assert path.resolve(strict=True) == path and path.stat().st_uid == os.getuid()
assert not list(scratch.iterdir()), list(scratch.iterdir())
matching = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdecimal() or int(entry.name) == os.getpid():
        continue
    try:
        argv = (entry / "cmdline").read_bytes()
        if os.fsencode(source) in argv or os.fsencode(scratch) in argv:
            matching.append(
                dict(
                    pid=int(entry.name),
                    starttime=(entry / "stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()[19],
                )
            )
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
assert not matching, matching
# Preserve the exact reviewed test factory actually imported by these runs.
shutil.copyfile(source / "pg_terminal_adapter.py", base / "pg_terminal_adapter.py")
shutil.rmtree(source)
scratch.rmdir()
print(
    json.dumps(
        dict(
            removed=[str(source), str(scratch)],
            matchingLiveProcesses=matching,
            retainedExactAdapter=str(base / "pg_terminal_adapter.py"),
        )
    )
)
