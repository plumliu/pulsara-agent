"""Launch actual Windows Electron with an isolated profile; no UI automation."""

import json
import os
from pathlib import Path
import subprocess
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
mode = sys.argv[1] if len(sys.argv) > 1 else "mock"
assert mode in ("mock", "real")
assert mode != "real" or "--approved" in sys.argv
executable = ROOT / "desktop/node_modules/electron/dist/electron.exe"
environment = dict(os.environ)
environment.pop("ELECTRON_RUN_AS_NODE", None)
command = [str(executable), str(OUT / "electron.cjs"), mode]
if mode == "real":
    command.append("--approved")
result = subprocess.run(
    command, env=environment, capture_output=True, text=True, encoding="utf-8"
)
(OUT / (mode + ".windows.log")).write_text(result.stdout, encoding="utf-8")
# Electron/system diagnostics are not an OAuth data channel; record only size.
(OUT / (mode + ".launch.json")).write_text(
    json.dumps(
        {
            "argv": command,
            "exit": result.returncode,
            "stderrBytes": len(result.stderr.encode("utf-8")),
        },
        indent=2,
    ),
    encoding="utf-8",
)
print(
    json.dumps(
        {
            "mode": mode,
            "exit": result.returncode,
            "stdoutLog": str(OUT / (mode + ".windows.log")),
        }
    )
)
raise SystemExit(result.returncode)
