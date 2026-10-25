"""Observe and interrupt only one exact dogfood harness, preserving its finally."""

import json
import os
from pathlib import Path
import select
import signal
import sys

marker = sys.argv[1]
assert marker.startswith(
    "/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/provider-acceptance/"
)
assert marker.endswith("/result.json")
matches = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdecimal():
        continue
    try:
        argv = (entry / "cmdline").read_bytes().split(b"\0")
        if marker.encode() not in argv or b"--execute" not in argv:
            continue
        assert entry.stat().st_uid == os.getuid()
        matches.append(entry)
    except (FileNotFoundError, ProcessLookupError):
        pass
assert len(matches) == 1, matches
entry = matches[0]
with os.fdopen(os.pidfd_open(int(entry.name)), "rb") as held:
    start = (entry / "stat").read_text().rsplit(")", 1)[1].split()[19]
    assert marker.encode() in (entry / "cmdline").read_bytes().split(b"\0")
    signal.pidfd_send_signal(held.fileno(), signal.SIGINT)
    exited = bool(select.select([held], [], [], 30)[0])
print(
    json.dumps(
        dict(
            pid=int(entry.name),
            starttime=start,
            sigintSent=True,
            exitedWithinObservation=exited,
        )
    )
)
