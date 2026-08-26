"""Purpose-neutral lexical path preparation and no-follow directory binding."""

from __future__ import annotations

import os
from pathlib import Path
import sys


DIRECTORY_NOFOLLOW_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

# Darwin exposes these OS-owned top-level aliases as symlinks.  This is the
# only alias policy shared by local package/Skill source owners; arbitrary
# source parents and every source-tree member remain strictly no-follow.
_DARWIN_SYSTEM_ALIASES = {
    "tmp": ("private", "tmp"),
    "var": ("private", "var"),
    "etc": ("private", "etc"),
}


def prepare_local_source_path(path: Path) -> Path:
    """Freeze one lexical absolute source path without following its tree."""

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
    current = os.open(os.sep, DIRECTORY_NOFOLLOW_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def open_or_create_absolute_directory_nofollow(
    path: Path, *, mode: int = 0o700
) -> int:
    """Create missing components and bind the result without following links."""

    if not path.is_absolute():
        raise ValueError("absolute directory path required")
    current = os.open(os.sep, DIRECTORY_NOFOLLOW_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=mode, dir_fd=current)
                except FileExistsError:
                    pass
                next_fd = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


__all__ = [
    "DIRECTORY_NOFOLLOW_FLAGS",
    "open_absolute_directory_nofollow",
    "open_or_create_absolute_directory_nofollow",
    "prepare_local_source_path",
]
