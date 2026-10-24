"""Launch an owned hidden Electron probe; no system sleep or network mutation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import ctypes
from ctypes import wintypes

BASE = Path(__file__).resolve().parent
ROOT = next(parent for parent in BASE.parents if (parent / "pyproject.toml").exists())
electron = ROOT / "desktop/node_modules/electron/dist/electron.exe"
mode = "manual" if "--manual" in sys.argv else "automatic"
env = {
    key: value
    for key, value in os.environ.items()
    if key.upper() not in ("ELECTRON_RUN_AS_NODE", "WSLENV")
}
with (
    (BASE / (mode + "-host.stdout.log")).open("ab") as stdout,
    (BASE / (mode + "-host.stderr.log")).open("ab") as stderr,
):
    process = subprocess.Popen(
        [
            str(electron),
            str(BASE / "network_host.cjs"),
            *(["--manual-resume"] if mode == "manual" else []),
        ],
        env=env,
        stdout=stdout,
        stderr=stderr,
        creationflags=0x08000000,
    )
    times = [wintypes.FILETIME() for _ in range(4)]
    gettimes = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    gettimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    assert gettimes(int(process._handle), *(ctypes.byref(t) for t in times))
    creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    (BASE / (mode + "-host-process.json")).write_text(
        json.dumps(dict(pid=process.pid, creation=creation, executable=str(electron))),
        encoding="utf-8",
    )
    if mode == "automatic":
        code = process.wait(timeout=100)
        (BASE / "automatic-host-exit.json").write_text(
            json.dumps(dict(pid=process.pid, exit=code)), encoding="utf-8"
        )
        print(json.dumps(dict(mode=mode, pid=process.pid, exit=code)))
    else:
        print(
            json.dumps(
                dict(
                    mode=mode,
                    pid=process.pid,
                    readyFile=str(BASE / "manual-ready.json"),
                )
            )
        )
