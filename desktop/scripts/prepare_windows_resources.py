"""Build the Windows helper and verify the supplied Linux payload; never build Linux on Windows."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate_payload(directory: Path, version: str) -> None:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version or manifest.get("platform") != "linux-x86_64":
        raise ValueError("The Linux payload must match this Windows application version and architecture")
    archive = directory / "payload.tar"
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Missing regular Linux payload archive")
    with archive.open("rb") as stream:
        observed = hashlib.file_digest(stream, "sha256").hexdigest()
    if manifest.get("sha256") != observed:
        raise ValueError("Linux payload archive checksum mismatch")


def compile_helper(output: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows host helper natively on Windows")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise RuntimeError("The Windows build requires the .NET Framework 4.x compiler")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/r:System.Runtime.Serialization.dll", f"/out:{output}", str(ROOT / "native/windows-interop-helper/Program.cs")], check=True)
    checked = subprocess.run([str(output), "--version"], check=True, capture_output=True, text=True)
    if checked.stdout.strip() != "pulsara-interop 1":
        raise RuntimeError("The Windows interoperability helper protocol differs")


def build() -> None:
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    validate_payload(ROOT / "resources/wsl-payload", version)
    compile_helper(ROOT / "resources/windows-interop/pulsara-interop.exe")
    print("Windows host helper and Linux payload verified")


if __name__ == "__main__":
    build()
