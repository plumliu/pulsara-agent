"""Interrupt only this exact private harness; let its existing finally settle."""
import json
import os
from pathlib import Path
import signal

needle = b"/home/plumwsl/workspaces/.provider-accept-xqgw_1ur/run_terminal_wsl.py"
rows = []
for process in Path("/proc").iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    descriptor = None
    try:
        descriptor = os.pidfd_open(int(process.name))
        args = (process / "cmdline").read_bytes().split(b"\0")
        if process.stat().st_uid == os.getuid() and needle in args:
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
            rows.append(int(process.name))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
print(json.dumps({"sigint_exact_harness_pids": rows}))
