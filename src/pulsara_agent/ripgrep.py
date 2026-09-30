"""Stable private executable path; ripgrep owns search, not Pulsara's PATH."""

from pathlib import Path
import os
import subprocess

RIPGREP_VERSION = "15.2.0"


class RipgrepUnavailable(RuntimeError):
    pass


def private_ripgrep() -> Path:
    path = (
        Path(__file__).parent
        / "_vendor"
        / "ripgrep"
        / ("rg.exe" if os.name == "nt" else "rg")
    )
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            raise OSError(f"missing or non-executable resource: {path}")
        completed = subprocess.run(
            [str(path), "--version"], capture_output=True, timeout=60, check=True
        )
        if completed.stdout.splitlines()[0].split()[:2] != [
            b"ripgrep",
            RIPGREP_VERSION.encode(),
        ]:
            raise ValueError("incorrect bundled ripgrep version")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        raise RipgrepUnavailable(
            f"Pulsara 内置搜索依赖不可用，请修复/重新安装 Pulsara: {exc}"
        ) from exc
    return path.absolute()
