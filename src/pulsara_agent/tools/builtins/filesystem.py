"""Workspace file-system built-in tools.

Reads expose exact-byte content revisions and process-local seen-line
observations. Existing files are changed only through revision-anchored,
deterministic line operations; ``write_file`` is create-only.
"""

from __future__ import annotations

import difflib
import errno
import fnmatch
from hashlib import sha256
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Any, Mapping

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.tools.builtins.schemas import (
    int_arg,
    json_text,
    str_arg,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool, WritePathScope


MAX_READ_LINES = 2_000
DEFAULT_READ_LINES = 2_000
MAX_READ_CHARS = 100_000
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 1_000
UTF8_BOM = "\ufeff"
UTF8_BOM_BYTES = b"\xef\xbb\xbf"
CONTENT_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
CHANGED_WINDOW_CONTEXT_LINES = 3
# Changed windows precede the diff in the result. Keeping their complete JSON
# below this budget ensures the existing 8,000-character head/tail projection
# does not expose a partial line while granting that line edit eligibility.
MAX_CHANGED_WINDOWS_JSON_CHARS = 3_500
BLOCKED_DEVICE_PATHS = {
    "/dev/null",
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
class _WorkspaceFileState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    path_locks: dict[Path, threading.Lock] = field(default_factory=dict)
    observations: dict[Path, "_FileObservationSlot"] = field(default_factory=dict)
    last_lookup_key: tuple | None = None
    consecutive_lookup_count: int = 0

    def lock_for_path(self, path: Path) -> threading.Lock:
        with self.lock:
            path_lock = self.path_locks.get(path)
            if path_lock is None:
                path_lock = threading.Lock()
                self.path_locks[path] = path_lock
            return path_lock

    def observe(
        self,
        path: Path,
        revision: str,
        intervals: tuple[tuple[int, int], ...],
    ) -> None:
        with self.lock:
            current = self.observations.get(path)
            if current is not None and current.content_revision == revision:
                intervals = _merge_intervals((*current.seen_line_intervals, *intervals))
            self.observations[path] = _FileObservationSlot(
                content_revision=revision,
                seen_line_intervals=intervals,
            )

    def observation(self, path: Path) -> "_FileObservationSlot | None":
        with self.lock:
            return self.observations.get(path)

    def clear_observation(self, path: Path) -> None:
        with self.lock:
            self.observations.pop(path, None)


@dataclass(frozen=True, slots=True)
class _FileObservationSlot:
    content_revision: str
    seen_line_intervals: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _TextLayout:
    text: str
    lines: tuple[str, ...]
    newline: str | None
    had_final_newline: bool
    had_bom: bool


@dataclass(frozen=True, slots=True)
class _LineOperation:
    kind: str
    start_line: int | None = None
    end_line: int | None = None
    line: int | None = None
    lines: tuple[str, ...] = ()
    content: str | None = None


class _FileApplicationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.details = dict(details or {})
        super().__init__(message)


class _AtomicTargetExists(FileExistsError):
    pass


class _AtomicNoClobberUnavailable(OSError):
    pass


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

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        path = self._resolve_read_path(str_arg(call.arguments, "path"))
        access_scope = _path_access_scope(
            path, self.workspace_root, self._resolved_user_home()
        )
        workspace_relative = access_scope == "workspace"
        offset = _normalize_offset(int_arg(call.arguments, "offset", 1))
        limit = _normalize_limit(
            int_arg(call.arguments, "limit", DEFAULT_READ_LINES), MAX_READ_LINES
        )
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        try:
            with path_lock:
                raw_bytes, layout = _read_existing_text(path)
                revision = _content_revision(raw_bytes)
                total_lines = len(layout.lines)
                start_index = min(offset - 1, total_lines)
                end_index = min(start_index + limit, total_lines)
                selected = layout.lines[start_index:end_index]
                content = "\n".join(
                    f"{line_number}|{line}"
                    for line_number, line in enumerate(selected, start=offset)
                )
                if len(content) > MAX_READ_CHARS:
                    return self._result(
                        call,
                        status=ToolResultState.ERROR,
                        output=json_text(
                            {
                                "error": "READ_OUTPUT_TOO_LARGE",
                                "message": (
                                    f"Read produced {len(content):,} characters, exceeding "
                                    f"the safety limit of {MAX_READ_CHARS:,}."
                                ),
                                "path": _relpath(path, self.workspace_root),
                                "access_scope": access_scope,
                                "workspace_relative": workspace_relative,
                                "total_lines": total_lines,
                                "_hint": "Retry read_file with a smaller limit.",
                            }
                        ),
                        metadata={
                            "path": str(path),
                            "chars": len(content),
                            "access_scope": access_scope,
                            "workspace_relative": workspace_relative,
                        },
                    )

                truncated = end_index < total_lines
                with state.lock:
                    _track_lookup(state, ("read", path, offset, limit))
                    consecutive = state.consecutive_lookup_count

                payload: dict[str, Any] = {
                    "status": "ok",
                    "path": _relpath(path, self.workspace_root),
                    "content_revision": revision,
                    "access_scope": access_scope,
                    "workspace_relative": workspace_relative,
                    "offset": offset,
                    "limit": limit,
                    "total_lines": total_lines,
                    "file_size": len(raw_bytes),
                    "truncated": truncated,
                    "content": content,
                }
                if layout.had_bom:
                    payload["had_utf8_bom"] = True
                if truncated:
                    payload["_hint"] = (
                        f"More lines are available. Continue with offset={end_index + 1}."
                    )
                if consecutive >= 3:
                    payload["_warning"] = (
                        f"You have read this exact file region {consecutive} times "
                        "consecutively. Use the information you already have."
                    )
                output = json_text(payload)
                fully_visible = (
                    len(output.encode("utf-8"))
                    <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
                )
                intervals = (
                    ((start_index + 1, end_index),)
                    if fully_visible and end_index > start_index
                    else ()
                )
                if not fully_visible:
                    payload["_warning"] = (
                        "This read exceeds the complete provider-visible ToolResult "
                        "bound, so none of its lines authorize edit_file. Re-read the "
                        "required range with a smaller limit."
                    )
                    output = json_text(payload)
                state.observe(path, revision, intervals)
        except _FileApplicationError as exc:
            return _application_error_result(self, call, path, exc)
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=output,
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

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        pattern = str_arg(call.arguments, "pattern")
        if not pattern:
            raise ValueError("pattern is required")
        raw_target = str_arg(call.arguments, "target") or "content"
        target = raw_target
        if target not in {"content", "files"}:
            raise ValueError(f"unsupported search target: {raw_target}")
        path = self._resolve_read_path(str_arg(call.arguments, "path") or ".")
        user_home = self._resolved_user_home()
        access_scope = _path_access_scope(path, self.workspace_root, user_home)
        workspace_relative = access_scope == "workspace"
        limit = int_arg(call.arguments, "limit", DEFAULT_SEARCH_LIMIT)
        limit = _normalize_limit(limit, MAX_SEARCH_LIMIT)
        offset = max(0, int_arg(call.arguments, "offset", 0))
        file_glob = str_arg(call.arguments, "file_glob")
        output_mode = str_arg(call.arguments, "output_mode") or "content"
        if output_mode not in {"content", "files_only", "count"}:
            raise ValueError(f"unsupported output_mode: {output_mode}")
        if not path.exists():
            raise FileNotFoundError(f"path not found: {path}")
        if _is_broad_search_root(path, self.workspace_root, user_home):
            raise ValueError(
                f"refusing broad recursive search root outside workspace: {path}. "
                "Use a specific file or subdirectory."
            )

        state = _state_for_workspace(self.workspace_root)
        search_key = (
            "search",
            pattern,
            target,
            path,
            file_glob or "",
            limit,
            offset,
            output_mode,
        )
        with state.lock:
            _track_lookup(state, search_key)
            consecutive = state.consecutive_lookup_count
        if consecutive >= 4:
            return self._result(
                call,
                status=ToolResultState.ERROR,
                output=json_text(
                    {
                        "error": "Repeated search blocked: this exact search has already been returned.",
                        "pattern": pattern,
                        "access_scope": access_scope,
                        "workspace_relative": workspace_relative,
                        "already_searched": consecutive,
                    }
                ),
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
            )
        if consecutive >= 3:
            payload["_warning"] = (
                f"You have run this exact search {consecutive} times consecutively. "
                "Use the information you already have."
            )
        if payload.get("truncated"):
            payload["_hint"] = (
                f"Results truncated. Continue with offset={offset + limit}."
            )
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

    def _search_files(
        self, pattern: str, *, path: Path, limit: int, offset: int
    ) -> dict[str, Any]:
        files = (
            _rg_files(pattern, path)
            if which("rg")
            else _python_find_files(pattern, path)
        )
        files = _sort_paths_by_mtime(files)
        page = files[offset : offset + limit]
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
    ) -> dict[str, Any]:
        if which("rg"):
            return self._search_content_with_rg(
                pattern,
                path=path,
                file_glob=file_glob,
                limit=limit,
                offset=offset,
                output_mode=output_mode,
            )
        return self._search_content_with_python(
            pattern,
            path=path,
            file_glob=file_glob,
            limit=limit,
            offset=offset,
            output_mode=output_mode,
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
    ) -> dict[str, Any]:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--color",
            "never",
        ]
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
        lines = [
            line for line in completed.stdout.splitlines() if line and line != "--"
        ]
        if output_mode == "files_only":
            files = [Path(line).resolve() for line in lines]
            return {
                "status": "ok",
                "target": "content",
                "output_mode": "files_only",
                "total_count": len(files),
                "truncated": offset + limit < len(files),
                "files": [
                    _relpath(file, self.workspace_root)
                    for file in files[offset : offset + limit]
                ],
            }
        if output_mode == "count":
            counts: dict[str, int] = {}
            for line in lines:
                path_part, _, count_part = line.rpartition(":")
                try:
                    counts[_relpath(Path(path_part).resolve(), self.workspace_root)] = (
                        int(count_part)
                    )
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
        page = matches[offset : offset + limit]
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
    ) -> dict[str, Any]:
        regex = re.compile(pattern)
        files = (
            [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        )
        if file_glob:
            files = [p for p in files if fnmatch.fnmatch(p.name, file_glob)]
        matches: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        matching_files: set[Path] = set()
        for file_path in files:
            if _has_binary_extension(file_path):
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if regex.search(line):
                    rel = _relpath(file_path, self.workspace_root)
                    counts[rel] = counts.get(rel, 0) + 1
                    matching_files.add(file_path)
                    matches.append(
                        {"path": rel, "line": line_number, "content": line[:500]}
                    )
        if output_mode == "files_only":
            files_page = _sort_paths_by_mtime(list(matching_files))[
                offset : offset + limit
            ]
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
            "matches": matches[offset : offset + limit],
        }


@dataclass(slots=True)
class EditFileTool(WorkspaceTool):
    name: str = "edit_file"

    def execute(
        self,
        call: ToolCall,
        *,
        write_scope: WritePathScope = WritePathScope.WORKSPACE,
    ) -> ToolExecutionResult:
        path = self._resolve_path(
            str_arg(call.arguments, "path"), write_scope=write_scope
        )
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        try:
            base_revision, operations = _parse_edit_arguments(call.arguments)
            with path_lock:
                before_bytes, before_layout = _read_existing_text(path)
                current_revision = _content_revision(before_bytes)
                if current_revision != base_revision:
                    raise _FileApplicationError(
                        "CONTENT_REVISION_MISMATCH",
                        "The file changed after the revision used for this edit.",
                        hint="Re-read the target range and retry with the new content_revision.",
                        details={"current_revision": current_revision},
                    )
                observation = state.observation(path)
                if observation is None or observation.content_revision != base_revision:
                    raise _FileApplicationError(
                        "READ_OBSERVATION_REQUIRED",
                        "No current read_file observation authorizes this edit.",
                        hint=_read_hint_for_operations(operations),
                    )
                after_text = _stage_edit(
                    before_layout,
                    operations,
                    observation.seen_line_intervals,
                )
                after_bytes = _encode_text(
                    after_text,
                    had_bom=before_layout.had_bom,
                )
                if after_bytes == before_bytes:
                    raise _FileApplicationError(
                        "NO_OP",
                        "The requested operations do not change the file.",
                        hint="Remove the no-op operation or re-read before editing again.",
                    )
                staged_layout = _decode_text_bytes(after_bytes, path=path)
                new_revision = _content_revision(after_bytes)
                changed_windows, seen_intervals, windows_truncated = _changed_windows(
                    before_layout.lines,
                    staged_layout.lines,
                )
                diff = _unified_diff(
                    _text_with_original_bom(before_layout),
                    _text_with_original_bom(staged_layout),
                    _relpath(path, self.workspace_root),
                )
                latest_bytes, _latest_layout = _read_existing_text(path)
                if latest_bytes != before_bytes:
                    latest_revision = _content_revision(latest_bytes)
                    raise _FileApplicationError(
                        "CONTENT_REVISION_MISMATCH",
                        "The file changed while this edit was being staged.",
                        hint="Re-read the target range and retry with the new content_revision.",
                        details={"current_revision": latest_revision},
                    )
                _atomic_replace_bytes(path, after_bytes)
                verified_bytes = path.read_bytes()
                if verified_bytes != after_bytes:
                    raise RuntimeError(
                        "post-write verification failed; the file may already be modified"
                    )
                state.observe(path, new_revision, seen_intervals)
        except _FileApplicationError as exc:
            return _application_error_result(self, call, path, exc)

        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(
                {
                    "status": "ok",
                    "path": _relpath(path, self.workspace_root),
                    "base_revision": base_revision,
                    "content_revision": new_revision,
                    "operations_applied": len(operations),
                    "changed_windows": changed_windows,
                    "changed_windows_truncated": windows_truncated,
                    "diff": diff,
                    "files_modified": [_relpath(path, self.workspace_root)],
                }
            ),
            metadata={"path": str(path), "operations_applied": len(operations)},
        )


@dataclass(slots=True)
class WriteFileTool(WorkspaceTool):
    name: str = "write_file"

    def execute(
        self,
        call: ToolCall,
        *,
        write_scope: WritePathScope = WritePathScope.WORKSPACE,
    ) -> ToolExecutionResult:
        raw_path = str_arg(call.arguments, "path")
        path = self._resolve_path(raw_path, write_scope=write_scope)
        requested_leaf = _requested_leaf_path(raw_path, self.workspace_root)
        if "content" not in call.arguments:
            raise ValueError("content is required")
        content = str_arg(call.arguments, "content")
        if content is None:
            raise ValueError("content must be a string")
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        try:
            raw_bytes = _validate_new_text_content(path, content)
            with path_lock:
                if requested_leaf.is_symlink() or path.exists() or path.is_symlink():
                    raise _FileApplicationError(
                        "FILE_ALREADY_EXISTS",
                        "write_file creates new files and never overwrites an existing path.",
                        hint="Use read_file, then edit_file with the returned content_revision.",
                    )
                try:
                    _atomic_create_bytes(path, raw_bytes)
                except _AtomicTargetExists as exc:
                    raise _FileApplicationError(
                        "FILE_ALREADY_EXISTS",
                        "The target appeared while write_file was preparing publication.",
                        hint="Use read_file, then edit_file with the returned content_revision.",
                    ) from exc
                except _AtomicNoClobberUnavailable as exc:
                    raise _FileApplicationError(
                        "ATOMIC_NO_CLOBBER_UNAVAILABLE",
                        "This filesystem cannot provide atomic no-clobber publication.",
                        hint="Choose a supported local filesystem or create the file explicitly outside this tool.",
                    ) from exc
                verified_bytes = path.read_bytes()
                if verified_bytes != raw_bytes:
                    raise RuntimeError(
                        "post-write verification failed; the file may already be created"
                    )
                state.clear_observation(path)
        except _FileApplicationError as exc:
            return _application_error_result(self, call, path, exc)

        revision = _content_revision(verified_bytes)
        payload: dict[str, Any] = {
            "status": "ok",
            "path": _relpath(path, self.workspace_root),
            "content_revision": revision,
            "bytes_written": len(verified_bytes),
            "files_modified": [_relpath(path, self.workspace_root)],
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={"path": str(path), "bytes": len(verified_bytes)},
        )


def _content_revision(raw_bytes: bytes) -> str:
    return f"sha256:{sha256(raw_bytes).hexdigest()}"


def _requested_leaf_path(raw_path: str | None, workspace_root: Path) -> Path:
    """Resolve parent components while retaining the requested final directory entry."""
    if not raw_path or not raw_path.strip():
        raise ValueError("path is required")
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        requested = workspace_root / requested
    return requested.parent.resolve() / requested.name


def _application_error_result(
    tool: WorkspaceTool,
    call: ToolCall,
    path: Path,
    error: _FileApplicationError,
) -> ToolExecutionResult:
    payload: dict[str, object] = {
        "error": error.code,
        "message": error.message,
        "path": _relpath(path, tool.workspace_root),
        "_hint": error.hint,
    }
    payload.update(error.details)
    return tool._result(
        call,
        status=ToolResultState.ERROR,
        output=json_text(payload),
        metadata={"path": str(path), "error_code": error.code},
    )


def _read_existing_text(path: Path) -> tuple[bytes, _TextLayout]:
    if _is_blocked_device(path):
        raise _FileApplicationError(
            "UNSUPPORTED_BINARY_FILE",
            "Blocked device paths cannot be read or edited as text files.",
            hint="Choose a regular UTF-8 text file.",
        )
    if _has_binary_extension(path):
        raise _FileApplicationError(
            "UNSUPPORTED_BINARY_FILE",
            f"Known binary file type is not supported: {path.suffix.lower()}",
            hint="Choose a regular UTF-8 text file.",
        )
    if not path.exists():
        raise _FileApplicationError(
            "FILE_NOT_FOUND",
            "The requested file does not exist.",
            hint="Check the path. Use write_file only when creating a new file.",
        )
    if not path.is_file():
        raise _FileApplicationError(
            "NOT_A_REGULAR_FILE",
            "The requested path is not a regular file.",
            hint="Choose an existing regular UTF-8 text file.",
        )
    raw_bytes = path.read_bytes()
    return raw_bytes, _decode_text_bytes(raw_bytes, path=path)


def _decode_text_bytes(raw_bytes: bytes, *, path: Path) -> _TextLayout:
    had_bom = raw_bytes.startswith(UTF8_BOM_BYTES)
    body = raw_bytes[len(UTF8_BOM_BYTES) :] if had_bom else raw_bytes
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _FileApplicationError(
            "UNSUPPORTED_TEXT_ENCODING",
            "The file is not valid UTF-8 and cannot be edited without data loss.",
            hint="Convert the file to UTF-8 explicitly before using file tools.",
        ) from exc
    if "\x00" in text:
        raise _FileApplicationError(
            "UNSUPPORTED_BINARY_FILE",
            "The file contains NUL bytes and is treated as binary.",
            hint="Use a binary-aware tool for this file.",
        )
    newline = _exact_newline_style(text)
    had_final_newline = text.endswith(("\r\n", "\n", "\r"))
    return _TextLayout(
        text=text,
        lines=_logical_lines(text, had_final_newline=had_final_newline),
        newline=newline,
        had_final_newline=had_final_newline,
        had_bom=had_bom,
    )


def _logical_lines(text: str, *, had_final_newline: bool) -> tuple[str, ...]:
    if not text:
        return ()
    parts = re.split(r"\r\n|\r|\n", text)
    if had_final_newline:
        parts.pop()
    return tuple(parts)


def _exact_newline_style(text: str) -> str | None:
    styles = set(re.findall(r"\r\n|\r|\n", text))
    if not styles:
        return None
    if styles == {"\n"}:
        return "\n"
    if styles == {"\r\n"}:
        return "\r\n"
    return "mixed"


def _encode_text(text: str, *, had_bom: bool) -> bytes:
    try:
        body = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _FileApplicationError(
            "UNSUPPORTED_TEXT_ENCODING",
            "The requested content is not valid Unicode text encodable as UTF-8.",
            hint="Remove surrogate code points and retry with valid Unicode text.",
        ) from exc
    return (UTF8_BOM_BYTES if had_bom else b"") + body


def _validate_new_text_content(path: Path, content: str) -> bytes:
    if _is_blocked_device(path) or _has_binary_extension(path) or "\x00" in content:
        raise _FileApplicationError(
            "UNSUPPORTED_BINARY_FILE",
            "write_file creates regular UTF-8 text files only.",
            hint="Choose a non-binary path and content without NUL bytes.",
        )
    return _encode_text(content, had_bom=False)


def _parse_edit_arguments(
    arguments: Mapping[str, object],
) -> tuple[str, tuple[_LineOperation, ...]]:
    required = {"path", "base_revision", "operations"}
    if set(arguments) != required:
        raise _FileApplicationError(
            "INVALID_OPERATION_COMBINATION",
            "edit_file accepts only path, base_revision, and operations.",
            hint="Use the current line-operation schema; old_text/new_text are not supported.",
        )
    base_revision = arguments.get("base_revision")
    if not isinstance(base_revision, str) or not CONTENT_REVISION_PATTERN.fullmatch(
        base_revision
    ):
        raise _FileApplicationError(
            "INVALID_CONTENT_REVISION",
            "base_revision must be the exact SHA-256 content_revision from read_file.",
            hint="Call read_file and copy its complete content_revision unchanged.",
        )
    raw_operations = arguments.get("operations")
    if not isinstance(raw_operations, (list, tuple)) or not raw_operations:
        raise _FileApplicationError(
            "INVALID_OPERATION_COMBINATION",
            "operations must be a non-empty array.",
            hint="Provide at least one closed line operation.",
        )
    return base_revision, tuple(_parse_operation(item) for item in raw_operations)


def _parse_operation(raw: object) -> _LineOperation:
    if not isinstance(raw, Mapping):
        raise _invalid_operation("Each operation must be an object.")
    kind = raw.get("kind")
    if kind == "replace_lines":
        _require_exact_keys(raw, {"kind", "start_line", "end_line", "lines"})
        return _LineOperation(
            kind=kind,
            start_line=_positive_line(raw.get("start_line"), "start_line"),
            end_line=_positive_line(raw.get("end_line"), "end_line"),
            lines=_operation_lines(raw.get("lines")),
        )
    if kind == "delete_lines":
        _require_exact_keys(raw, {"kind", "start_line", "end_line"})
        return _LineOperation(
            kind=kind,
            start_line=_positive_line(raw.get("start_line"), "start_line"),
            end_line=_positive_line(raw.get("end_line"), "end_line"),
        )
    if kind in {"insert_before", "insert_after"}:
        _require_exact_keys(raw, {"kind", "line", "lines"})
        return _LineOperation(
            kind=kind,
            line=_positive_line(raw.get("line"), "line"),
            lines=_operation_lines(raw.get("lines")),
        )
    if kind == "replace_file":
        _require_exact_keys(raw, {"kind", "content"})
        content = raw.get("content")
        if not isinstance(content, str):
            raise _invalid_operation("replace_file content must be a string.")
        if "\x00" in content:
            raise _FileApplicationError(
                "UNSUPPORTED_BINARY_FILE",
                "replace_file content contains a NUL byte.",
                hint="Use valid UTF-8 text without NUL bytes.",
            )
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _FileApplicationError(
                "UNSUPPORTED_TEXT_ENCODING",
                "replace_file content cannot be encoded as UTF-8.",
                hint="Remove surrogate code points and retry.",
            ) from exc
        return _LineOperation(kind=kind, content=content)
    raise _invalid_operation(f"Unsupported operation kind: {kind!r}.")


def _require_exact_keys(raw: Mapping[str, object], expected: set[str]) -> None:
    if set(raw) != expected:
        raise _invalid_operation(
            f"{raw.get('kind')!r} requires exactly {sorted(expected)}."
        )


def _positive_line(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_operation(f"{name} must be a positive integer.")
    return value


def _operation_lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise _invalid_operation("lines must be a non-empty array of logical lines.")
    lines: list[str] = []
    for line in value:
        if not isinstance(line, str) or any(
            marker in line for marker in ("\r", "\n", "\x00")
        ):
            raise _invalid_operation(
                "Each lines item must be one string without CR, LF, or NUL."
            )
        try:
            line.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _FileApplicationError(
                "UNSUPPORTED_TEXT_ENCODING",
                "An operation line cannot be encoded as UTF-8.",
                hint="Remove surrogate code points and retry.",
            ) from exc
        lines.append(line)
    return tuple(lines)


def _invalid_operation(message: str) -> _FileApplicationError:
    return _FileApplicationError(
        "INVALID_OPERATION_COMBINATION",
        message,
        hint="Use one of replace_lines, delete_lines, insert_before, insert_after, or replace_file.",
    )


def _stage_edit(
    layout: _TextLayout,
    operations: tuple[_LineOperation, ...],
    seen_intervals: tuple[tuple[int, int], ...],
) -> str:
    replace_file = tuple(item for item in operations if item.kind == "replace_file")
    if replace_file:
        if len(operations) != 1:
            raise _invalid_operation("replace_file must be the only operation.")
        assert replace_file[0].content is not None
        return _stage_replace_file(layout, replace_file[0].content)
    if layout.newline == "mixed":
        raise _FileApplicationError(
            "UNSUPPORTED_MIXED_LINE_ENDINGS",
            "Local line operations require uniform LF or CRLF line endings.",
            hint="Use replace_file with the current base_revision to replace the complete file.",
        )
    total_lines = len(layout.lines)
    ranges: list[tuple[int, int, _LineOperation]] = []
    gaps: dict[int, _LineOperation] = {}
    for operation in operations:
        if operation.kind in {"replace_lines", "delete_lines"}:
            assert operation.start_line is not None and operation.end_line is not None
            start = operation.start_line
            end = operation.end_line
            if start > end or end > total_lines:
                raise _line_range_error(start, end, total_lines)
            if not _interval_fully_seen(seen_intervals, start, end):
                raise _unseen_error(start, end)
            if (
                operation.kind == "replace_lines"
                and tuple(layout.lines[start - 1 : end]) == operation.lines
            ):
                raise _FileApplicationError(
                    "NO_OP",
                    f"replace_lines {start}..{end} is identical to the current lines.",
                    hint="Remove the no-op operation or submit changed logical lines.",
                )
            for prior_start, prior_end, _ in ranges:
                if max(start, prior_start) <= min(end, prior_end):
                    raise _FileApplicationError(
                        "OVERLAPPING_OPERATIONS",
                        f"Line range {start}..{end} overlaps {prior_start}..{prior_end}.",
                        hint="Use non-overlapping ranges based on the original file.",
                    )
            ranges.append((start, end, operation))
            continue
        assert operation.kind in {"insert_before", "insert_after"}
        assert operation.line is not None
        line = operation.line
        if line > total_lines:
            raise _line_range_error(line, line, total_lines)
        if not _interval_fully_seen(seen_intervals, line, line):
            raise _unseen_error(line, line)
        gap = line - 1 if operation.kind == "insert_before" else line
        if gap in gaps:
            raise _FileApplicationError(
                "OVERLAPPING_OPERATIONS",
                f"More than one operation targets the gap around line {line}.",
                hint="Combine insertions that target the same original-file gap.",
            )
        gaps[gap] = operation
    for start, end, _ in ranges:
        for gap in gaps:
            if start <= gap < end:
                raise _FileApplicationError(
                    "OVERLAPPING_OPERATIONS",
                    f"Insertion gap {gap} falls inside replaced range {start}..{end}.",
                    hint="Move the insertion outside the range or combine it with replace_lines.",
                )
    return _materialize_line_operations(layout, ranges, gaps)


def _stage_replace_file(layout: _TextLayout, content: str) -> str:
    if content.startswith(UTF8_BOM):
        content = content[len(UTF8_BOM) :]
    if layout.newline == "mixed":
        return content
    target_newline = layout.newline if layout.newline in {"\n", "\r\n"} else "\n"
    return _normalize_line_endings(content, target_newline)


def _materialize_line_operations(
    layout: _TextLayout,
    ranges: list[tuple[int, int, _LineOperation]],
    gaps: dict[int, _LineOperation],
) -> str:
    range_by_start = {start: (end, operation) for start, end, operation in ranges}
    output: list[str] = []
    cursor = 1
    total_lines = len(layout.lines)
    while cursor <= total_lines:
        insertion = gaps.get(cursor - 1)
        if insertion is not None:
            output.extend(insertion.lines)
        ranged = range_by_start.get(cursor)
        if ranged is not None:
            end, operation = ranged
            if operation.kind == "replace_lines":
                output.extend(operation.lines)
            cursor = end + 1
            continue
        output.append(layout.lines[cursor - 1])
        cursor += 1
    tail = gaps.get(total_lines)
    if tail is not None:
        output.extend(tail.lines)
    if not output:
        return ""
    newline = layout.newline if layout.newline in {"\n", "\r\n"} else "\n"
    rendered = newline.join(output)
    if layout.had_final_newline:
        rendered += newline
    return rendered


def _line_range_error(start: int, end: int, total_lines: int) -> _FileApplicationError:
    return _FileApplicationError(
        "INVALID_LINE_RANGE",
        f"Line range {start}..{end} is invalid for a {total_lines}-line file.",
        hint=f"Call read_file with offset={max(1, min(start, total_lines or 1))} and retry.",
        details={"total_lines": total_lines},
    )


def _unseen_error(start: int, end: int) -> _FileApplicationError:
    return _FileApplicationError(
        "UNSEEN_LINE_RANGE",
        f"Lines {start}..{end} were not all shown by read_file for this revision.",
        hint=f"Call read_file with offset={start}, limit={end - start + 1}, then retry.",
        details={"suggested_read": {"offset": start, "limit": end - start + 1}},
    )


def _interval_fully_seen(
    intervals: tuple[tuple[int, int], ...], start: int, end: int
) -> bool:
    return any(left <= start and end <= right for left, right in intervals)


def _merge_intervals(
    intervals: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    if not intervals:
        return ()
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _read_hint_for_operations(operations: tuple[_LineOperation, ...]) -> str:
    anchors: list[int] = []
    for operation in operations:
        if operation.kind == "replace_file":
            return "Call read_file for the target path and retry with its content_revision."
        if operation.start_line is not None:
            anchors.extend(
                (operation.start_line, operation.end_line or operation.start_line)
            )
        elif operation.line is not None:
            anchors.append(operation.line)
    if not anchors:
        return "Call read_file for the target path and retry."
    start = min(anchors)
    end = max(anchors)
    return f"Call read_file with offset={start}, limit={end - start + 1}, then retry."


def _changed_windows(
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> tuple[list[dict[str, object]], tuple[tuple[int, int], ...], bool]:
    if not after:
        return [], (), False
    raw_ranges: list[tuple[int, int]] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j1 == j2:
            anchor = min(max(1, j1 + 1), len(after))
            raw_ranges.append((anchor, anchor))
            continue
        start = j1 + 1
        end = j2
        if end - start + 1 > CHANGED_WINDOW_CONTEXT_LINES * 2:
            raw_ranges.extend(
                (
                    (start, start + CHANGED_WINDOW_CONTEXT_LINES - 1),
                    (end - CHANGED_WINDOW_CONTEXT_LINES + 1, end),
                )
            )
        else:
            raw_ranges.append((start, end))
    expanded = tuple(
        (
            max(1, start - CHANGED_WINDOW_CONTEXT_LINES),
            min(len(after), end + CHANGED_WINDOW_CONTEXT_LINES),
        )
        for start, end in raw_ranges
    )
    intervals = _merge_intervals(expanded)
    windows: list[dict[str, object]] = []
    visible_intervals: list[tuple[int, int]] = []
    truncated = False
    used_chars = len(json_text({"changed_windows": []}))
    for start, end in intervals:
        window: dict[str, object] = {
            "offset": start,
            "end_line": end,
            "content": "\n".join(
                f"{line_number}|{after[line_number - 1]}"
                for line_number in range(start, end + 1)
            ),
        }
        window_chars = len(json_text(window)) + (2 if windows else 0)
        if used_chars + window_chars > MAX_CHANGED_WINDOWS_JSON_CHARS:
            truncated = True
            continue
        windows.append(window)
        visible_intervals.append((start, end))
        used_chars += window_chars
    return windows, tuple(visible_intervals), truncated


def _text_with_original_bom(layout: _TextLayout) -> str:
    return (UTF8_BOM if layout.had_bom else "") + layout.text


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


def _path_access_scope(path: Path, workspace_root: Path, user_home: Path | None) -> str:
    resolved = path.resolve()
    root = workspace_root.resolve()
    if resolved == root or root in resolved.parents:
        return "workspace"
    if user_home is not None:
        home = user_home.resolve()
        if resolved == home or home in resolved.parents:
            return "home"
    temp_roots = _temp_roots()
    if any(
        resolved == temp_root or temp_root in resolved.parents
        for temp_root in temp_roots
    ):
        return "temp"
    return "external_absolute"


def _is_broad_search_root(
    path: Path, workspace_root: Path, user_home: Path | None
) -> bool:
    resolved = path.resolve()
    root = workspace_root.resolve()
    if resolved == root or root in resolved.parents:
        return False
    if resolved.is_file():
        return False
    return resolved in _broad_search_roots(root, user_home)


def _broad_search_roots(workspace_root: Path, user_home: Path | None) -> set[Path]:
    roots = {Path("/").resolve()}
    if user_home is not None:
        roots.add(user_home.resolve())
    for candidate in (
        "/Users",
        "/home",
        "/System",
        "/Library",
        "/Applications",
        "/var",
        "/usr",
        "/bin",
        "/sbin",
        "/etc",
    ):
        candidate_path = Path(candidate)
        if candidate_path.exists():
            roots.add(candidate_path.resolve())
    roots.update(_temp_roots())
    parent = workspace_root.resolve().parent
    parent_roots = _temp_roots()
    if user_home is not None:
        parent_roots.add(user_home.resolve())
    if parent in parent_roots:
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
    return sorted(
        paths,
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
        reverse=True,
    )


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


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_create_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    published = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
            published = True
        except FileExistsError as exc:
            raise _AtomicTargetExists(str(path)) from exc
        except OSError as exc:
            unsupported = {
                errno.EXDEV,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                errno.EOPNOTSUPP,
            }
            if exc.errno in unsupported:
                raise _AtomicNoClobberUnavailable(str(path)) from exc
            raise
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            if not published:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(fd)


def _unified_diff(before: str, after: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
