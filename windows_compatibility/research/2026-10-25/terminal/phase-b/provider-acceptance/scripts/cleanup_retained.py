"""Remove only this runner's already settled retained test homes/workspaces."""

import json
import os
from pathlib import Path
import shutil
import sys

base = Path(sys.argv[1])
assert base.resolve() == Path(
    "/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/provider-acceptance"
)
scratch = Path("/home/plumwsl/.pvt-1vw1i3vp")
paths = set()
for result in base.glob("*/result.json"):
    report = json.loads(result.read_text())
    assert not report.get("undropped_databases"), result
    for key in ("scratch", "test_home"):
        value = report.get(key)
        if not value:
            continue
        path = Path(value)
        if key == "scratch":
            assert path.parent == scratch and path.name.startswith("Pulsara 中文 验收 ")
        else:
            assert path.parent == Path("/home/plumwsl") and path.name.startswith(".pa-")
        paths.add(path)

live = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdecimal() or int(entry.name) == os.getpid():
        continue
    try:
        argv = (entry / "cmdline").read_bytes()
        if any(os.fsencode(path) in argv for path in paths):
            live.append(
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
assert not live, live
removed, absent = [], []
for path in sorted(paths):
    if not path.exists():
        absent.append(str(path))
        continue
    assert path.resolve(strict=True) == path and path.stat().st_uid == os.getuid()
    # An extant scope directory is retained physical ownership, not garbage.
    assert not list(path.glob("desktop/i-*")), path
    shutil.rmtree(path)
    removed.append(str(path))
print(
    json.dumps(
        dict(removed=removed, alreadyAbsent=absent, matchingLiveProcesses=live),
        ensure_ascii=False,
    )
)
