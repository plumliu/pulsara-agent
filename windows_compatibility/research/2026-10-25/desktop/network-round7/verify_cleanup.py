"""Read-only exact recorded identities; no process termination or directory removal."""

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys

base = Path(__file__).resolve().parent
rows = [
    json.loads(p.read_text(encoding="utf-8")) for p in base.glob("n7-*-results.json")
]
identities = [
    dict(
        label=r["label"],
        pid=r["ready"]["pid"],
        start=r["ready"]["starttime"],
        home=r["ready"]["home"],
    )
    for r in rows
]
script = """
import json,sys,os
from pathlib import Path
rows=[]
for value in json.load(sys.stdin):
 p=Path('/proc')/str(value['pid']); fd=None; current=None
 try:
  fd=os.pidfd_open(value['pid']); current=(p/'stat').read_text().rsplit(')',1)[1].split()[19]
 except (ProcessLookupError,FileNotFoundError):pass
 finally:
  if fd is not None:os.close(fd)
 rows.append(dict(**value,currentStart=current,exactAlive=current==value['start'],homeExists=Path(value['home']).exists()))
print(json.dumps(rows))
"""
completed = subprocess.run(
    [
        "wsl.exe",
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--exec",
        "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
        "-c",
        script,
    ],
    input=json.dumps(identities),
    text=True,
    capture_output=True,
    check=True,
)
linux = json.loads(completed.stdout)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k.OpenProcess.restype = wintypes.HANDLE
k.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    *([ctypes.POINTER(wintypes.FILETIME)] * 4),
]
k.CloseHandle.argtypes = [wintypes.HANDLE]
windows = []
for name in ("automatic-host-process.json", "manual-host-process.json"):
    value = json.loads((base / name).read_text())
    h = k.OpenProcess(0x1000, False, value["pid"])
    current = None
    if h:
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            assert k.GetProcessTimes(h, *(ctypes.byref(t) for t in times))
            current = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k.CloseHandle(h)
    else:
        assert ctypes.get_last_error() == 87, ctypes.get_last_error()
    windows.append(
        dict(**value, currentCreation=current, exactAlive=current == value["creation"])
    )
historical = []
for r in rows:
    if r["windowsPid"] in [w["pid"] for w in windows]:
        continue
    h = k.OpenProcess(0x1000, False, r["windowsPid"])
    historical.append(
        dict(
            pid=r["windowsPid"],
            currentPidPresent=bool(h),
            historicalCreationRecorded=False,
        )
    )
    if h:
        k.CloseHandle(h)
    else:
        assert ctypes.get_last_error() == 87
manual = json.loads((base / "manual-ready.json").read_text())
allow = "--allow-manual" in sys.argv
active_linux = [r for r in linux if r["exactAlive"] or r["homeExists"]]
active_windows = [r for r in windows if r["exactAlive"]]
if allow:
    assert all(r["label"] == manual["label"] for r in active_linux)
    assert all(r["pid"] == manual["windowsPid"] for r in active_windows)
else:
    assert not active_linux and not active_windows
assert not any(v["currentPidPresent"] for v in historical), historical
result = dict(
    passed=True,
    manualIntentionallyHeld=allow,
    linux=linux,
    windows=windows,
    historicalCurrentPidQueries=historical,
    stderr=completed.stderr,
)
target = base / ("cleanup-checkpoint.json" if allow else "cleanup-final.json")
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            passed=True,
            manualIntentionallyHeld=allow,
            linuxIdentities=len(linux),
            activeLinux=[v["label"] for v in active_linux],
            activeWindows=[v["pid"] for v in active_windows],
        )
    )
)
