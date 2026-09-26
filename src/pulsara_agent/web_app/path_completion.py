"""Read directory metadata for cwd-relative composer completion, never contents."""

from __future__ import annotations

from heapq import nsmallest
import os
from pathlib import Path


# Bound one HTTP response/render batch, not the number of accessible candidates.
# Section 11 of the file-path specification requires cursor pagination beyond it.
PATH_CANDIDATE_PAGE_SIZE = 100


def complete_workspace_paths(root: Path, prefix: str, cursor: str | None) -> dict[str, object]:
    if Path(prefix).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in prefix):
        raise ValueError("请输入相对于当前工作目录的路径。")
    parent, _, name_prefix = prefix.rpartition("/")
    directory = (root / parent).resolve(strict=True)
    needle = name_prefix.casefold()
    with os.scandir(directory) as entries:
        def eligible():
            for entry in entries:
                if not entry.name.casefold().startswith(needle) or (cursor is not None and entry.name <= cursor):
                    continue
                # Existing one-line path-reference syntax excludes control chars.
                if any(ord(char) < 32 or ord(char) == 127 for char in str(directory / entry.name)):
                    continue
                try:
                    kind = "directory" if entry.is_dir() else "file" if entry.is_file() else None
                except OSError:
                    continue  # A concurrently removed/unreadable entry is not a candidate.
                if kind is not None:
                    yield entry.name, kind

        page = nsmallest(PATH_CANDIDATE_PAGE_SIZE + 1, eligible())
    more = len(page) > PATH_CANDIDATE_PAGE_SIZE
    page = page[:PATH_CANDIDATE_PAGE_SIZE]
    return {
        "directory": str(directory),
        "items": [{
            "name": name,
            "path": str(directory / name),
            "relative_path": f"{parent}/{name}" if parent else name,
            "kind": kind,
        } for name, kind in page],
        "next_cursor": page[-1][0] if more else None,
    }
