"""Workspace file-system built-in tools.

This module ports the practical shape of Hermes' file tools into Pulsara's
local-workspace runtime: paginated reads, structured search, atomic writes,
staleness checks, fuzzy targeted edits, diffs, and post-write verification.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import (
    bool_arg,
    int_arg,
    json_text,
    object_schema,
    required_str_arg,
    str_arg,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


MAX_READ_LINES = 2_000
DEFAULT_READ_LINES = 500
MAX_READ_CHARS = 100_000
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 1_000
UTF8_BOM = "\ufeff"
READ_DEDUP_MESSAGE = (
    "File unchanged since last read. The content from the earlier read_file "
    "result in this conversation is still current."
)
BLOCKED_DEVICE_PATHS = {
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
    "/dev/stdin",
    "/dev/tty",
    "/dev/console",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
}
BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".pyc",
    ".so",
    ".tar",
    ".wasm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}


@dataclass(slots=True)
class _ReadRecord:
    mtime_ns: int
    dedup_hits: int = 0


@dataclass(slots=True)
class _WorkspaceFileState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    path_locks: dict[Path, threading.Lock] = field(default_factory=dict)
    read_cache: dict[tuple[Path, int, int], _ReadRecord] = field(default_factory=dict)
    read_timestamps: dict[Path, int] = field(default_factory=dict)
    last_lookup_key: tuple | None = None
    consecutive_lookup_count: int = 0

    def lock_for_path(self, path: Path) -> threading.Lock:
        with self.lock:
            path_lock = self.path_locks.get(path)
            if path_lock is None:
                path_lock = threading.Lock()
                self.path_locks[path] = path_lock
            return path_lock


_STATES: dict[Path, _WorkspaceFileState] = {}
_STATES_LOCK = threading.Lock()


def _state_for_workspace(workspace_root: Path) -> _WorkspaceFileState:
    root = workspace_root.resolve()
    with _STATES_LOCK:
        state = _STATES.get(root)
        if state is None:
            state = _WorkspaceFileState()
            _STATES[root] = state
        return state


@dataclass(slots=True)
class ReadFileTool(WorkspaceTool):
    name: str = "read_file"
    description: str = (
        "Read a UTF-8 text file with line numbers and pagination. Relative paths "
        "resolve from workspace_root; absolute paths and ~ may read host-local ordinary text files."
    )
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "path": {
                "type": "string",
                "description": "Relative paths resolve from workspace_root; absolute paths and ~ are allowed for text reads.",
            },
            "offset": {
                "type": "integer",
                "description": "1-indexed line number to start reading from.",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum number of lines to read, capped at {MAX_READ_LINES}.",
                "default": DEFAULT_READ_LINES,
            },
        },
        required=["path"],
    ))
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        path = self._resolve_read_path(str_arg(call.arguments, "path"))
        access_scope = _path_access_scope(path, self.workspace_root)
        workspace_relative = access_scope == "workspace"
        offset = _normalize_offset(int_arg(call.arguments, "offset", 1))
        limit = _normalize_limit(int_arg(call.arguments, "limit", DEFAULT_READ_LINES), MAX_READ_LINES)
        if _is_blocked_device(path):
            raise ValueError(f"cannot read device path that may block or produce infinite output: {path}")
        if _has_binary_extension(path):
            raise ValueError(f"cannot read binary file as text: {path.suffix.lower()}")
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {path}")

        state = _state_for_workspace(self.workspace_root)
        mtime_ns = path.stat().st_mtime_ns
        dedup_key = (path, offset, limit)
        with state.lock:
            record = state.read_cache.get(dedup_key)
            if record is not None and record.mtime_ns == mtime_ns:
                record.dedup_hits += 1
                if record.dedup_hits >= 2:
                    return self._result(
                        call,
                        status=ToolResultState.ERROR,
                        output=json_text({
                            "error": (
                                "Repeated read blocked: this exact file region has already "
                                "been returned and the file has not changed."
                            ),
                            "path": _relpath(path, self.workspace_root),
                            "access_scope": access_scope,
                            "workspace_relative": workspace_relative,
                            "already_read": record.dedup_hits + 1,
                        }),
                        metadata={
                            "path": str(path),
                            "dedup": True,
                            "access_scope": access_scope,
                            "workspace_relative": workspace_relative,
                        },
                    )
                return self._result(
                    call,
                    status=ToolResultState.SUCCESS,
                    output=json_text({
                        "status": "unchanged",
                        "message": READ_DEDUP_MESSAGE,
                        "path": _relpath(path, self.workspace_root),
                        "access_scope": access_scope,
                        "workspace_relative": workspace_relative,
                        "content_returned": False,
                        "dedup": True,
                    }),
                    metadata={
                        "path": str(path),
                        "dedup": True,
                        "access_scope": access_scope,
                        "workspace_relative": workspace_relative,
                    },
                )

        raw_text = path.read_text(encoding="utf-8", errors="replace")
        text, had_bom = _strip_bom(raw_text)
        lines = text.splitlines()
        total_lines = len(lines)
        start_index = min(offset - 1, total_lines)
        end_index = min(start_index + limit, total_lines)
        selected = lines[start_index:end_index]
        content = "\n".join(
            f"{line_number}|{line}"
            for line_number, line in enumerate(selected, start=offset)
        )
        if len(content) > MAX_READ_CHARS:
            return self._result(
                call,
                status=ToolResultState.ERROR,
                output=json_text({
                    "error": (
                        f"Read produced {len(content):,} characters, exceeding "
                        f"the safety limit of {MAX_READ_CHARS:,}. Use a smaller limit."
                    ),
                    "path": _relpath(path, self.workspace_root),
                    "access_scope": access_scope,
                    "workspace_relative": workspace_relative,
                    "total_lines": total_lines,
                }),
                metadata={
                    "path": str(path),
                    "chars": len(content),
                    "access_scope": access_scope,
                    "workspace_relative": workspace_relative,
                },
            )

        truncated = end_index < total_lines
        with state.lock:
            state.read_cache[dedup_key] = _ReadRecord(mtime_ns=mtime_ns)
            state.read_timestamps[path] = mtime_ns
            _track_lookup(state, ("read", path, offset, limit))
            consecutive = state.consecutive_lookup_count

        payload: dict[str, Any] = {
            "status": "ok",
            "path": _relpath(path, self.workspace_root),
            "access_scope": access_scope,
            "workspace_relative": workspace_relative,
            "offset": offset,
            "limit": limit,
            "total_lines": total_lines,
            "file_size": path.stat().st_size,
            "truncated": truncated,
            "content": content,
        }
        if had_bom:
            payload["had_utf8_bom"] = True
        if truncated:
            payload["_hint"] = f"More lines are available. Continue with offset={end_index + 1}."
        if consecutive >= 3:
            payload["_warning"] = (
                f"You have read this exact file region {consecutive} times consecutively. "
                "Use the information you already have."
            )
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={
                "path": str(path),
                "truncated": truncated,
                "lines": len(selected),
                "access_scope": access_scope,
                "workspace_relative": workspace_relative,
            },
        )


@dataclass(slots=True)
class SearchFilesTool(WorkspaceTool):
    name: str = "search_files"
    description: str = (
        "Search text files or find files by name. Relative paths resolve from workspace_root; "
        "absolute paths and ~ are allowed, but broad host roots are rejected outside the workspace."
    )
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "pattern": {"type": "string", "description": "Regex pattern or file glob/name pattern."},
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "'content' searches text; 'files' finds paths.",
                "default": "content",
            },
            "path": {
                "type": "string",
                "default": ".",
                "description": "Relative paths resolve from workspace_root. Outside workspace, use a specific file or subdirectory, not broad roots like ~, /, /Users, or /tmp.",
            },
            "file_glob": {"type": "string", "description": "Optional file glob for content search."},
            "limit": {"type": "integer", "default": DEFAULT_SEARCH_LIMIT},
            "offset": {"type": "integer", "default": 0},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
                "default": "content",
            },
            "context": {"type": "integer", "default": 0},
        },
        required=[],
    ))
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        pattern = str_arg(call.arguments, "pattern")
        if not pattern:
            raise ValueError("pattern is required")
        raw_target = str_arg(call.arguments, "target") or "content"
        target = raw_target
        if target not in {"content", "files"}:
            raise ValueError(f"unsupported search target: {raw_target}")
        path = self._resolve_read_path(str_arg(call.arguments, "path") or ".")
        access_scope = _path_access_scope(path, self.workspace_root)
        workspace_relative = access_scope == "workspace"
        limit = int_arg(call.arguments, "limit", DEFAULT_SEARCH_LIMIT)
        limit = _normalize_limit(limit, MAX_SEARCH_LIMIT)
        offset = max(0, int_arg(call.arguments, "offset", 0))
        file_glob = str_arg(call.arguments, "file_glob")
        output_mode = str_arg(call.arguments, "output_mode") or "content"
        context = max(0, int_arg(call.arguments, "context", 0))
        if output_mode not in {"content", "files_only", "count"}:
            raise ValueError(f"unsupported output_mode: {output_mode}")
        if not path.exists():
            raise FileNotFoundError(f"path not found: {path}")
        if _is_broad_search_root(path, self.workspace_root):
            raise ValueError(
                f"refusing broad recursive search root outside workspace: {path}. "
                "Use a specific file or subdirectory."
            )

        state = _state_for_workspace(self.workspace_root)
        search_key = ("search", pattern, target, path, file_glob or "", limit, offset, output_mode, context)
        with state.lock:
            _track_lookup(state, search_key)
            consecutive = state.consecutive_lookup_count
        if consecutive >= 4:
            return self._result(
                call,
                status=ToolResultState.ERROR,
                output=json_text({
                    "error": "Repeated search blocked: this exact search has already been returned.",
                    "pattern": pattern,
                    "access_scope": access_scope,
                    "workspace_relative": workspace_relative,
                    "already_searched": consecutive,
                }),
                metadata={
                    "path": str(path),
                    "pattern": pattern,
                    "access_scope": access_scope,
                    "workspace_relative": workspace_relative,
                },
            )

        if target == "files":
            payload = self._search_files(pattern, path=path, limit=limit, offset=offset)
        else:
            payload = self._search_content(
                pattern,
                path=path,
                file_glob=file_glob,
                limit=limit,
                offset=offset,
                output_mode=output_mode,
                context=context,
            )
        if consecutive >= 3:
            payload["_warning"] = (
                f"You have run this exact search {consecutive} times consecutively. "
                "Use the information you already have."
            )
        if payload.get("truncated"):
            payload["_hint"] = f"Results truncated. Continue with offset={offset + limit}."
        payload["access_scope"] = access_scope
        payload["workspace_relative"] = workspace_relative
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={
                "path": str(path),
                "pattern": pattern,
                "total_count": payload.get("total_count", 0),
                "access_scope": access_scope,
                "workspace_relative": workspace_relative,
            },
        )

    def _search_files(self, pattern: str, *, path: Path, limit: int, offset: int) -> dict[str, Any]:
        files = _rg_files(pattern, path) if which("rg") else _python_find_files(pattern, path)
        files = _sort_paths_by_mtime(files)
        page = files[offset:offset + limit]
        return {
            "status": "ok",
            "target": "files",
            "total_count": len(files),
            "truncated": offset + limit < len(files),
            "files": [_relpath(file, self.workspace_root) for file in page],
        }

    def _search_content(
        self,
        pattern: str,
        *,
        path: Path,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> dict[str, Any]:
        if which("rg"):
            return self._search_content_with_rg(
                pattern,
                path=path,
                file_glob=file_glob,
                limit=limit,
                offset=offset,
                output_mode=output_mode,
                context=context,
            )
        return self._search_content_with_python(
            pattern,
            path=path,
            file_glob=file_glob,
            limit=limit,
            offset=offset,
            output_mode=output_mode,
            context=context,
        )

    def _search_content_with_rg(
        self,
        pattern: str,
        *,
        path: Path,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> dict[str, Any]:
        cmd = ["rg", "--line-number", "--no-heading", "--with-filename", "--color", "never"]
        if context > 0:
            cmd.extend(["-C", str(context)])
        if file_glob:
            cmd.extend(["--glob", file_glob])
        if output_mode == "files_only":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        cmd.extend([pattern, str(path)])
        completed = subprocess.run(
            cmd,
            cwd=self.workspace_root,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        lines = [line for line in completed.stdout.splitlines() if line and line != "--"]
        if output_mode == "files_only":
            files = [Path(line).resolve() for line in lines]
            return {
                "status": "ok",
                "target": "content",
                "output_mode": "files_only",
                "total_count": len(files),
                "truncated": offset + limit < len(files),
                "files": [_relpath(file, self.workspace_root) for file in files[offset:offset + limit]],
            }
        if output_mode == "count":
            counts: dict[str, int] = {}
            for line in lines:
                path_part, _, count_part = line.rpartition(":")
                try:
                    counts[_relpath(Path(path_part).resolve(), self.workspace_root)] = int(count_part)
                except ValueError:
                    continue
            return {
                "status": "ok",
                "target": "content",
                "output_mode": "count",
                "total_count": sum(counts.values()),
                "counts": counts,
            }
        matches = [_parse_rg_match_line(line, self.workspace_root) for line in lines]
        matches = [match for match in matches if match is not None]
        page = matches[offset:offset + limit]
        return {
            "status": "ok",
            "target": "content",
            "output_mode": "content",
            "total_count": len(matches),
            "truncated": offset + limit < len(matches),
            "matches": page,
        }

    def _search_content_with_python(
        self,
        pattern: str,
        *,
        path: Path,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> dict[str, Any]:
        del context
        regex = re.compile(pattern)
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        if file_glob:
            files = [p for p in files if fnmatch.fnmatch(p.name, file_glob)]
        matches: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        matching_files: set[Path] = set()
        for file_path in files:
            if _has_binary_extension(file_path):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if regex.search(line):
                    rel = _relpath(file_path, self.workspace_root)
                    counts[rel] = counts.get(rel, 0) + 1
                    matching_files.add(file_path)
                    matches.append({"path": rel, "line": line_number, "content": line[:500]})
        if output_mode == "files_only":
            files_page = _sort_paths_by_mtime(list(matching_files))[offset:offset + limit]
            return {
                "status": "ok",
                "target": "content",
                "output_mode": "files_only",
                "total_count": len(matching_files),
                "truncated": offset + limit < len(matching_files),
                "files": [_relpath(file, self.workspace_root) for file in files_page],
            }
        if output_mode == "count":
            return {
                "status": "ok",
                "target": "content",
                "output_mode": "count",
                "total_count": sum(counts.values()),
                "counts": counts,
            }
        return {
            "status": "ok",
            "target": "content",
            "output_mode": "content",
            "total_count": len(matches),
            "truncated": offset + limit < len(matches),
            "matches": matches[offset:offset + limit],
        }


@dataclass(slots=True)
class EditFileTool(WorkspaceTool):
    name: str = "edit_file"
    description: str = (
        "Targeted find-and-replace edit. Uses exact and fuzzy matching, returns "
        "a unified diff, and verifies the write landed."
    )
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "path": {"type": "string"},
            "old_text": {
                "type": "string",
                "description": "Text to replace. Include surrounding context to make it unique.",
            },
            "new_text": {"type": "string", "description": "Replacement text. Empty string deletes."},
            "replace_all": {"type": "boolean", "default": False},
        },
        required=["path", "old_text", "new_text"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        path = self._resolve_path(str_arg(call.arguments, "path"))
        old_text = required_str_arg(call.arguments, "old_text")
        new_text = str_arg(call.arguments, "new_text")
        if new_text is None:
            raise ValueError("new_text is required")
        replace_all = bool_arg(call.arguments, "replace_all", False)
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {path}")
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        with path_lock:
            before_raw = path.read_text(encoding="utf-8", errors="replace")
            before, had_bom = _strip_bom(before_raw)
            updated, count, strategy, error = _fuzzy_replace(before, old_text, new_text, replace_all)
            if error or count == 0:
                return self._result(
                    call,
                    status=ToolResultState.ERROR,
                    output=json_text({
                        "error": error or "old_text was not found",
                        "path": _relpath(path, self.workspace_root),
                        "_hint": "Re-read the file or use search_files to locate the current text.",
                    }),
                    metadata={"path": str(path), "replacements": 0},
                )
            line_ending = _detect_line_ending(before_raw)
            if line_ending:
                updated = _normalize_line_endings(updated, line_ending)
            write_text = (UTF8_BOM if had_bom and not updated.startswith(UTF8_BOM) else "") + updated
            diff = _unified_diff(before, updated, _relpath(path, self.workspace_root))
            _atomic_write_text(path, write_text)
            verified, _ = _strip_bom(path.read_text(encoding="utf-8", errors="replace"))
            if _normalize_line_endings(verified, "\n") != _normalize_line_endings(updated, "\n"):
                return self._result(
                    call,
                    status=ToolResultState.ERROR,
                    output=json_text({
                        "error": "post-write verification failed; on-disk content differs from intended edit",
                        "path": _relpath(path, self.workspace_root),
                    }),
                    metadata={"path": str(path)},
                )
            _note_write(state, path)
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text({
                "status": "ok",
                "path": _relpath(path, self.workspace_root),
                "replacements": count,
                "strategy": strategy,
                "diff": diff,
                "files_modified": [_relpath(path, self.workspace_root)],
            }),
            metadata={"path": str(path), "replacements": count, "strategy": strategy},
        )


@dataclass(slots=True)
class WriteFileTool(WorkspaceTool):
    name: str = "write_file"
    description: str = (
        "Write complete UTF-8 content to a workspace file, replacing existing "
        "content atomically. Use edit_file for targeted edits."
    )
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "path": {"type": "string"},
            "content": {"type": "string"},
            "create_dirs": {"type": "boolean", "default": True},
        },
        required=["path", "content"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        path = self._resolve_path(str_arg(call.arguments, "path"))
        if "content" not in call.arguments:
            raise ValueError("content is required")
        content = str_arg(call.arguments, "content")
        if content is None:
            raise ValueError("content must be a string")
        create_dirs = bool_arg(call.arguments, "create_dirs", True)
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        with path_lock:
            stale_warning = _stale_warning(state, path)
            before = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if path.exists():
                line_ending = _detect_line_ending(before)
                if line_ending:
                    content = _normalize_line_endings(content, line_ending)
                if _has_bom(before) and not _has_bom(content):
                    content = UTF8_BOM + content
            if create_dirs:
                path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, content)
            _note_write(state, path)
        payload: dict[str, Any] = {
            "status": "ok",
            "path": _relpath(path, self.workspace_root),
            "bytes_written": path.stat().st_size,
            "files_modified": [_relpath(path, self.workspace_root)],
        }
        if stale_warning:
            payload["_warning"] = stale_warning
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={"path": str(path), "bytes": path.stat().st_size},
        )


def _normalize_offset(value: int) -> int:
    return max(1, value)


def _normalize_limit(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))


def _track_lookup(state: _WorkspaceFileState, key: tuple) -> None:
    if state.last_lookup_key == key:
        state.consecutive_lookup_count += 1
    else:
        state.last_lookup_key = key
        state.consecutive_lookup_count = 1


def _is_blocked_device(path: Path) -> bool:
    raw = str(path)
    if raw in BLOCKED_DEVICE_PATHS:
        return True
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        return False
    return resolved in BLOCKED_DEVICE_PATHS


def _has_binary_extension(path: Path) -> bool:
    return path.suffix.lower() in BINARY_EXTENSIONS


def _strip_bom(text: str) -> tuple[str, bool]:
    if text.startswith(UTF8_BOM):
        return text[len(UTF8_BOM):], True
    return text, False


def _has_bom(text: str) -> bool:
    return text.startswith(UTF8_BOM)


def _detect_line_ending(text: str) -> str | None:
    head = text[:4096]
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    lf = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\r\n":
        return lf.replace("\n", "\r\n")
    if target == "\n":
        return lf
    return text


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _path_access_scope(path: Path, workspace_root: Path) -> str:
    resolved = path.resolve()
    root = workspace_root.resolve()
    if resolved == root or root in resolved.parents:
        return "workspace"
    home = Path.home().resolve()
    if resolved == home or home in resolved.parents:
        return "home"
    temp_roots = _temp_roots()
    if any(resolved == temp_root or temp_root in resolved.parents for temp_root in temp_roots):
        return "temp"
    return "external_absolute"


def _is_broad_search_root(path: Path, workspace_root: Path) -> bool:
    resolved = path.resolve()
    root = workspace_root.resolve()
    if resolved == root or root in resolved.parents:
        return False
    if resolved.is_file():
        return False
    return resolved in _broad_search_roots(root)


def _broad_search_roots(workspace_root: Path) -> set[Path]:
    roots = {Path("/").resolve()}
    home = Path.home().resolve()
    roots.add(home)
    for candidate in ("/Users", "/home", "/System", "/Library", "/Applications", "/var", "/usr", "/bin", "/sbin", "/etc"):
        candidate_path = Path(candidate)
        if candidate_path.exists():
            roots.add(candidate_path.resolve())
    roots.update(_temp_roots())
    parent = workspace_root.resolve().parent
    if parent in {home, *(_temp_roots())}:
        roots.add(parent)
    return roots


def _temp_roots() -> set[Path]:
    roots = {Path(tempfile.gettempdir()).resolve()}
    for candidate in ("/tmp", "/private/tmp"):
        candidate_path = Path(candidate)
        if candidate_path.exists():
            roots.add(candidate_path.resolve())
    return roots


def _sort_paths_by_mtime(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns if path.exists() else 0, reverse=True)


def _rg_files(pattern: str, path: Path) -> list[Path]:
    glob = pattern
    if "/" not in glob and not glob.startswith("*"):
        glob = f"*{glob}*"
    completed = subprocess.run(
        ["rg", "--files", "-g", glob, str(path)],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return [Path(line).resolve() for line in completed.stdout.splitlines() if line]


def _python_find_files(pattern: str, path: Path) -> list[Path]:
    glob = pattern if any(ch in pattern for ch in "*?[]") else f"*{pattern}*"
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return [file for file in files if fnmatch.fnmatch(file.name, glob)]


def _parse_rg_match_line(line: str, root: Path) -> dict[str, Any] | None:
    match = re.match(r"^([A-Za-z]:)?(.*?):(\d+):(.*)$", line)
    if match is None:
        return None
    path_text = (match.group(1) or "") + match.group(2)
    return {
        "path": _relpath(Path(path_text).resolve(), root),
        "line": int(match.group(3)),
        "content": match.group(4)[:500],
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _stale_warning(state: _WorkspaceFileState, path: Path) -> str | None:
    with state.lock:
        read_mtime = state.read_timestamps.get(path)
    if read_mtime is None or not path.exists():
        return None
    current_mtime = path.stat().st_mtime_ns
    if current_mtime != read_mtime:
        return (
            f"{_relpath(path, path.parent)} was modified since the last read_file call. "
            "The content previously read by the agent may be stale."
        )
    return None


def _note_write(state: _WorkspaceFileState, path: Path) -> None:
    mtime_ns = path.stat().st_mtime_ns
    with state.lock:
        stale_keys = [key for key in state.read_cache if key[0] == path]
        for key in stale_keys:
            del state.read_cache[key]
        state.read_timestamps[path] = mtime_ns


def _unified_diff(before: str, after: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


def _fuzzy_replace(
    content: str,
    old_text: str,
    new_text: str,
    replace_all: bool,
) -> tuple[str, int, str | None, str | None]:
    if not old_text:
        return content, 0, None, "old_text cannot be empty"
    if old_text == new_text:
        return content, 0, None, "old_text and new_text are identical"
    strategies = (
        ("exact", _match_exact),
        ("trimmed_boundary", _match_trimmed_boundary),
        ("line_trimmed", _match_line_trimmed),
        ("whitespace_normalized", _match_whitespace_normalized),
    )
    for strategy, matcher in strategies:
        spans = matcher(content, old_text)
        if not spans:
            continue
        if len(spans) > 1 and not replace_all:
            return (
                content,
                0,
                None,
                f"Found {len(spans)} matches for old_text. Provide more context or set replace_all=true.",
            )
        selected = spans if replace_all else spans[:1]
        updated = _replace_spans(content, selected, new_text)
        return updated, len(selected), strategy, None
    return content, 0, None, "Could not find a match for old_text in the file"


def _match_exact(content: str, old_text: str) -> list[tuple[int, int]]:
    return _find_literal_spans(content, old_text)


def _match_trimmed_boundary(content: str, old_text: str) -> list[tuple[int, int]]:
    stripped = old_text.strip()
    if stripped == old_text or not stripped:
        return []
    return _find_literal_spans(content, stripped)


def _match_line_trimmed(content: str, old_text: str) -> list[tuple[int, int]]:
    old_lines = old_text.splitlines()
    if len(old_lines) <= 1:
        return []
    target = [line.strip() for line in old_lines]
    lines = content.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    starts: list[int] = []
    cursor = 0
    for line in lines:
        starts.append(cursor)
        cursor += len(line)
    window = len(target)
    for index in range(0, len(lines) - window + 1):
        if [line.strip() for line in lines[index:index + window]] == target:
            start = starts[index]
            end = starts[index + window] if index + window < len(starts) else len(content)
            spans.append((start, end))
    return spans


def _match_whitespace_normalized(content: str, old_text: str) -> list[tuple[int, int]]:
    parts = [part for part in re.split(r"\s+", old_text.strip()) if part]
    if len(parts) <= 1:
        return []
    pattern = r"\s+".join(re.escape(part) for part in parts)
    return [(match.start(), match.end()) for match in re.finditer(pattern, content, flags=re.MULTILINE)]


def _find_literal_spans(content: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = content.find(needle, start)
        if index == -1:
            return spans
        spans.append((index, index + len(needle)))
        start = index + len(needle)


def _replace_spans(content: str, spans: list[tuple[int, int]], replacement: str) -> str:
    updated = content
    for start, end in sorted(spans, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated
