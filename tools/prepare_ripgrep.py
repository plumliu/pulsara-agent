"""Explicit network preparation. Build hooks and runtime never download tools."""

from __future__ import annotations
import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile

VERSION = "15.2.0"
ASSETS = {
    ("Darwin", "arm64"): (
        "aarch64-apple-darwin.tar.gz",
        "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4",
    ),
    ("Darwin", "x86_64"): (
        "x86_64-apple-darwin.tar.gz",
        "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
    ),
    ("Linux", "aarch64"): (
        "aarch64-unknown-linux-musl.tar.gz",
        "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915",
    ),
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-musl.tar.gz",
        "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
    ),
    ("Windows", "ARM64"): (
        "aarch64-pc-windows-msvc.zip",
        "e4abca10c3a64ebea742667dd7009449d49403db5460dd6873e389fa2945360f",
    ),
    ("Windows", "AMD64"): (
        "x86_64-pc-windows-msvc.zip",
        "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5",
    ),
}


def prepare(repository: Path) -> None:
    target = (platform.system(), platform.machine())
    if target not in ASSETS:
        raise RuntimeError(f"Unsupported private ripgrep target: {target}")
    suffix, checksum = ASSETS[target]
    name = f"ripgrep-{VERSION}-{suffix}"
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/{name}"
    payload = urllib.request.urlopen(url, timeout=60).read()
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise RuntimeError(f"upstream asset checksum mismatch: {name}")
    executable = "rg.exe" if platform.system() == "Windows" else "rg"
    wanted = {executable, "COPYING", "LICENSE-MIT", "UNLICENSE"}
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            resources = {
                Path(n).name: archive.read(n)
                for n in archive.namelist()
                if Path(n).name in wanted
            }
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            resources = {
                Path(m.name).name: archive.extractfile(m).read()
                for m in archive
                if m.isfile() and Path(m.name).name in wanted
            }
    if not {executable, "COPYING", "LICENSE-MIT"} <= resources.keys():
        raise RuntimeError("upstream executable/license resources missing")
    # This release links PCRE2 10.45; retain its upstream redistribution notice.
    notice = urllib.request.urlopen("https://raw.githubusercontent.com/PCRE2Project/pcre2/pcre2-10.45/LICENCE.md", timeout=60).read()
    if hashlib.sha256(notice).hexdigest() != "9cf7ac6976099a1d856826d3ef1b093bd6b84489dc6100628ac79e740cf9885a":
        raise RuntimeError("PCRE2 redistribution notice checksum mismatch")
    resources["LICENSE-PCRE2"] = notice
    root = repository / "src/pulsara_agent/_vendor/ripgrep"
    root.mkdir(parents=True, exist_ok=True)
    for old_name in ("rg", "rg.exe"):
        if old_name not in resources:
            (root / old_name).unlink(missing_ok=True)
    for name, contents in resources.items():
        path = root / name
        path.write_bytes(contents)
        path.chmod(0o755 if name == executable else 0o644)
    print(f"Prepared ripgrep {VERSION}: {suffix}")


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1])
