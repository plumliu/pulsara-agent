"""Deterministic namespace races at real no-follow/mkdir/identity boundaries."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

HERE = Path(__file__).parent
RESULTS = []


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Request:
    def __init__(self, data):
        self.data = data

    async def json(self):
        return self.data


async def exercise(module, candidate):
    root = Path(
        tempfile.mkdtemp(prefix="g0-file-race-", dir=Path.home() / "workspaces")
    )
    try:
        parent = root / "parent"
        parent.mkdir()
        held = root / "renamed-parent"
        original = os.mkdir
        triggered = False

        def swap_parent(name, mode=0o777, *, dir_fd=None):
            nonlocal triggered
            if name == "created" and dir_fd is not None:
                assert not triggered
                triggered = True
                parent.rename(held)
                original(parent)
            return original(name, mode, dir_fd=dir_fd)

        try:
            with patch.object(module.os, "mkdir", swap_parent):
                await module.directories(
                    Request({"op": "mkdir", "path": str(parent), "name": "created"})
                )
            raise AssertionError("race returned success")
        except (
            OSError,
            module.DirectoryOutcomeUnknown if candidate else OSError,
        ) as exc:
            assert (held / "created").is_dir() and not (parent / "created").exists()
            if candidate:
                assert isinstance(exc, module.DirectoryOutcomeUnknown)
            RESULTS.append(
                {
                    "name": "parent-replacement-at-mkdir",
                    "candidate": candidate,
                    "error": type(exc).__name__,
                    "createdInHeldParent": True,
                    "createdInReplacementParent": False,
                    "outcome": "unknown"
                    if candidate
                    else "misclassified-as-ordinary-error",
                    "replayed": False,
                }
            )

        target = root / "selection"
        target.mkdir()
        old = root / "selection-old"
        resolve = module.resolve_workspace

        def replace_before_resolve(value):
            target.rename(old)
            target.mkdir()
            return resolve(value)

        try:
            with patch.object(module, "resolve_workspace", replace_before_resolve):
                response = await module.directories(
                    Request({"op": "select", "path": str(target)})
                )
            assert not candidate, "candidate accepted namespace replacement"
            RESULTS.append(
                {
                    "name": "selection-replacement-before-resolver",
                    "candidate": False,
                    "acceptedReplacement": True,
                    "response": json.loads(response.body),
                }
            )
        except ValueError as exc:
            assert candidate and "changed before acceptance" in str(exc)
            RESULTS.append(
                {
                    "name": "selection-replacement-before-resolver",
                    "candidate": True,
                    "acceptedReplacement": False,
                    "error": str(exc),
                }
            )
        for p in (
            root / "parent",
            root / "renamed-parent",
            root / "selection",
            root / "selection-old",
        ):
            assert p.is_dir()
    finally:
        shutil.rmtree(root)
        shutil.rmtree(module.HOME)


async def main():
    await exercise(load("gui_v2", "files-gui-v2.py"), False)
    await exercise(load("candidate", "files_guest.py"), True)
    (HERE / "files-races.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "cases": len(RESULTS),
                "candidatePassed": 2,
                "baselineCounterexamples": 2,
                "cleanup": True,
            }
        )
    )


asyncio.run(main())
