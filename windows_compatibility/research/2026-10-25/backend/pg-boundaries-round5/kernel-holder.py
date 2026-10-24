"""Hold the production kernel flock owner, without starting another kernel."""

import json
import os
from pathlib import Path
import sys

home = Path(sys.argv[1])
assert home.parent == Path("/home/plumwsl") and home.name.startswith(".g0p5-")
os.environ["PULSARA_HOME"] = str(home)
os.environ["G0_PG5_HOME"] = str(home)
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402 -- preserve executed research snapshot

fd = acquire_kernel_lock()
print(json.dumps({"type": "kernel-held", "pid": os.getpid()}), flush=True)
assert sys.stdin.read() == ""
os.close(fd)
