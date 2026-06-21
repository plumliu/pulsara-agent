#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from skill_utils import SkillValidationError, default_dest_root, resolve_workspace, validate_skill_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List installed workspace Pulsara skills.")
    parser.add_argument("--workspace", default=".", help="Workspace root. Defaults to the current directory.")
    parser.add_argument("--dest-root", help="Skills root. Defaults to <workspace>/.pulsara/skills.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workspace = resolve_workspace(args.workspace)
        dest_root = Path(args.dest_root).expanduser().resolve() if args.dest_root else default_dest_root(workspace)
        skills = []
        if dest_root.exists():
            for child in sorted(path for path in dest_root.iterdir() if path.is_dir()):
                try:
                    manifest = validate_skill_dir(child)
                except SkillValidationError as exc:
                    skills.append({"name": child.name, "path": str(child), "valid": False, "error": str(exc)})
                else:
                    skills.append(
                        {
                            "name": manifest.name,
                            "description": manifest.description,
                            "path": str(child),
                            "valid": True,
                        }
                    )

        if args.json:
            print(json.dumps({"skills_root": str(dest_root), "skills": skills}, ensure_ascii=False, indent=2))
        else:
            print(f"Skills root: {dest_root}")
            if not skills:
                print("No installed skills found.")
            for skill in skills:
                status = "valid" if skill.get("valid") else f"invalid: {skill.get('error')}"
                print(f"- {skill['name']} ({status})")
                if skill.get("description"):
                    print(f"  {skill['description']}")
        return 0
    except SkillValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
