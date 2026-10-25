"""Package the existing verified native Linux resources for the Windows host."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import sys
import tarfile
import tempfile

import prepare_resources as resources


def package(source: Path, destination: Path) -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("Build the Windows guest payload natively on Ubuntu x86_64")
    resources.target_platform()
    source = source.resolve(strict=True)
    resources.RESOURCES = source
    resources.validate_resources()
    version = json.loads((resources.ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pulsara-wsl-payload-", dir=destination) as temporary:
        temporary = Path(temporary)
        archive = temporary / "payload.tar"
        with tarfile.open(archive, "w") as stream:
            for entry in sorted(source.iterdir()):
                if entry.name == "bin":
                    raise ValueError("The CLI directory is provided by the existing packaged CLI owner")
                if entry.name != "desktop-launch.json":
                    stream.add(entry, arcname="payload/" + entry.name)
            launch = json.dumps({"execution_domain": "wsl"}).encode("utf-8")
            metadata = tarfile.TarInfo("payload/desktop-launch.json")
            metadata.mode = 0o600
            metadata.size = len(launch)
            stream.addfile(metadata, io.BytesIO(launch))
            cli = tarfile.TarInfo("payload/bin")
            cli.type = tarfile.DIRTYPE
            cli.mode = 0o755
            stream.addfile(cli)
            script = (resources.ROOT / "scripts/pulsara-cli.sh").read_bytes().replace(b"\r\n", b"\n")
            launcher = tarfile.TarInfo("payload/bin/pulsara")
            launcher.mode = 0o755
            launcher.size = len(script)
            stream.addfile(launcher, io.BytesIO(script))
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"version": version, "sha256": digest, "platform": "linux-x86_64"}
        archive.replace(destination / "payload.tar")
        metadata = temporary / "manifest.json"
        metadata.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        metadata.replace(destination / "manifest.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, default=resources.RESOURCES)
    parser.add_argument("--output", type=Path, default=resources.ROOT / "build/wsl-payload")
    args = parser.parse_args()
    package(args.resources, args.output)


if __name__ == "__main__":
    main()
