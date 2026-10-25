"""Native Linux storage observation shared by WSL data and project admission."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


def require_native_ext4(path: Path, *, reject_mounts_within: Path | None = None) -> None:
    result = subprocess.run(
        ["/usr/bin/findmnt", "--json", "--target", str(path), "--output", "FSTYPE,FSROOT,TARGET"],
        capture_output=True, text=True, check=True,
    )
    filesystem, = json.loads(result.stdout)["filesystems"]
    if (filesystem["fstype"] != "ext4" or filesystem["fsroot"] != "/"
            or (reject_mounts_within is not None and Path(filesystem["target"]).is_relative_to(reject_mounts_within))):
        raise ValueError("selected directory must be on native ext4, without a replacement mount")
