"""Settle only the known read-only bootstrap and observer of the first harness deadlock."""
import json
import os
from pathlib import Path
import signal

root = Path(__file__).resolve().parent
config_path = root / "pre-cancel/config.json"
config = json.loads(config_path.read_text())
home = Path(config["home"])
assert home == Path("/home/plumwsl/.pbfpg-efltozcl")
helper = json.loads((home / "helper-8.json").read_text())
assert helper["method"] == "prepareLayout" and not helper["armed"]
assert not (home / "gate-ready").exists()
actions = []

def signal_exact(pid, expected, number, ticks=None):
    handle = os.pidfd_open(pid)
    try:
        proc = Path("/proc") / str(pid)
        assert proc.stat().st_uid == os.getuid() == 1000
        command = proc.joinpath("cmdline").read_bytes()
        assert all(os.fsencode(value) in command for value in expected)
        actual = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
        assert ticks is None or ticks == actual
        signal.pidfd_send_signal(handle, number)
        actions.append({"pid": pid, "start_ticks": actual, "signal": number, "expected": expected})
    finally:
        os.close(handle)

signal_exact(helper["pid"], [str(root / "bootstrap.py"), str(config_path), "prepareLayout"], signal.SIGKILL, helper["start_ticks"])
observers = []
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        command = proc.joinpath("cmdline").read_bytes()
        if os.fsencode(str(root / "control.py")) in command and os.fsencode(str(config_path)) in command and b"wait-gate-ready" in command:
            observers.append(int(proc.name))
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        pass
assert len(observers) == 1
signal_exact(observers[0], [str(root / "control.py"), str(config_path), "wait-gate-ready"], signal.SIGTERM)
(root / "pre-cancel/rescue.json").write_text(json.dumps({"known_readonly_setup_only": True, "target_gate_never_reached": True, "actions": actions}, indent=2))
print(json.dumps({"known_actors_signalled": len(actions)}))
