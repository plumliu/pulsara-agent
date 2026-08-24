"""Narrow source-path preparation for loose local Skill operations."""

from __future__ import annotations

import os
from pathlib import Path
import sys


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_DARWIN_SYSTEM_ALIASES = {
    "tmp": ("private", "tmp"),
    "var": ("private", "var"),
    "etc": ("private", "etc"),
}


def prepare_local_skill_source_path(path: Path) -> Path:
    """Freeze one lexical source path without following its source-tree links.

    Darwin exposes a small, OS-owned set of top-level aliases as symlinks.  We
    normalize only those frozen aliases before descriptor-relative no-follow
    traversal.  Arbitrary parents, the final source, and every source member
    retain the strict no-follow contract.
    """

    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    prepared = Path(os.path.normpath(os.fspath(expanded)))
    if sys.platform != "darwin" or len(prepared.parts) < 2:
        return prepared
    alias = _DARWIN_SYSTEM_ALIASES.get(prepared.parts[1])
    if alias is None:
        return prepared
    return Path(os.sep).joinpath(*alias, *prepared.parts[2:])


def open_absolute_directory_nofollow(path: Path) -> int:
    """Open an absolute directory through an all-components no-follow walk."""

    if not path.is_absolute():
        raise ValueError("absolute directory path required")
    current = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


__all__ = [
    "open_absolute_directory_nofollow",
    "prepare_local_skill_source_path",
]
