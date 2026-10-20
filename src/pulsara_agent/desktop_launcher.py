"""Deliver a desktop launch request using the installed product's manifest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def installed_launch_command(executable: Path | None = None) -> list[str]:
    # The runtime is bundled under Resources/python (macOS) or resources/python.
    # No source-checkout, external browser, system Python, or PATH fallback.
    interpreter = (executable or Path(sys.executable)).absolute()
    for parent in interpreter.parents:
        manifest = parent / "desktop-launch.json"
        if not manifest.is_file():
            continue
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) - {"executable", "args"}
                or not isinstance(value.get("executable"), str)
                or not isinstance(value.get("args", []), list)
                or any(not isinstance(item, str) for item in value.get("args", []))):
            raise ValueError("Pulsara desktop launch manifest is invalid")
        target = (manifest.parent / value["executable"]).resolve()
        if not target.is_file() or not os.access(target, os.X_OK):
            raise RuntimeError(f"Pulsara desktop executable is unavailable: {target}")
        return [str(target), *value.get("args", []), "--pulsara-launch"]
    raise RuntimeError("Pulsara desktop is not installed for this command; install the CLI from Pulsara desktop")


def launch_desktop() -> None:
    subprocess.Popen(installed_launch_command(), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
