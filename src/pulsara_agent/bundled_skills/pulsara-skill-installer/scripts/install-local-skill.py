#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from skill_utils import (
    SkillValidationError,
    default_dest_root,
    manifest_to_json,
    reject_symlinks,
    resolve_source,
    resolve_workspace,
    validate_skill_dir,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install a local Pulsara skill into .pulsara/skills.")
    parser.add_argument("--workspace", default=".", help="Workspace root. Defaults to the current directory.")
    parser.add_argument("--src", required=True, help="Source skill directory, absolute or workspace-relative.")
    parser.add_argument("--dest-root", help="Destination skills root. Defaults to <workspace>/.pulsara/skills.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable installation details.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workspace = resolve_workspace(args.workspace)
        src = resolve_source(workspace, args.src)
        dest_root = Path(args.dest_root).expanduser().resolve() if args.dest_root else default_dest_root(workspace)
        manifest = validate_skill_dir(src)
        reject_symlinks(src)

        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / manifest.name
        if dest.exists():
            raise SkillValidationError(f"destination already exists, refusing overwrite: {dest}")
        if dest in src.parents or dest == src:
            raise SkillValidationError("source is already inside the destination skills root")

        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        installed = validate_skill_dir(dest)
        if args.json:
            print(manifest_to_json(installed, dest))
        else:
            print(f"Installed skill: {installed.name}")
            print(f"Destination: {dest}")
            print("Discovery: start a new Pulsara turn or run `pulsara host inspect` to refresh the skill catalog.")
        return 0
    except SkillValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
