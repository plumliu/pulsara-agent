"""Prepare independent R7 research sources; never overwrite R6 evidence."""

from pathlib import Path

PARENT = Path(__file__).resolve().parent.parent
SOURCE = PARENT / "round6"
BASE = PARENT / "round7"
BASE.mkdir()
names = [
    "distro_manage.py",
    "distro_prepare.py",
    "distro_build.py",
    "owner.cjs",
    "guest_control.py",
    "sitecustomize.py",
    "runtime.original.cjs",
    "runtime.candidate.cjs",
    "postgres.original.cjs",
    "main-quit.original.js",
]
for name in names:
    text = (SOURCE / name).read_text(encoding="utf-8")
    text = text.replace("/round6", "/round7").replace(
        "PulsaraG0-kernel6", "PulsaraG0-kernel7"
    )
    (BASE / name).write_text(text, encoding="utf-8")
