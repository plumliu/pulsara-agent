"""Workspace file-system built-in tools.

Reads expose exact-byte content revisions and process-local seen-line
observations. Existing files are changed only through revision-anchored,
deterministic line operations; ``write_file`` is create-only.
"""

from __future__ import annotations

import difflib
import errno
import base64
import json
from hashlib import sha256
import os
import re
import subprocess
import tempfile
import threading
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from wcmatch import glob

from pulsara_agent.ripgrep import private_ripgrep
from typing import Any, Mapping
from uuid import uuid4

from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
)
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
IMAGE_REFERENCE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
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


class _AtomicFileCreationError(OSError):
    """A local write failed; publication may already have happened."""

    def __init__(self, cause: Exception, *, published: bool | None) -> None:
        super().__init__(str(cause))
        self.published = published
        self.failure_errno = getattr(cause, "errno", None)


_STATES: dict[Path, _WorkspaceFileState] = {}
_STATES_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class LocalImageReadCandidate:
    payload: bytes = field(repr=False)
    resolved_path: Path
    requested_path: str


class ViewImageSourceKind(StrEnum):
    PATH = "path"
    IMAGE_REF = "image_ref"


@dataclass(frozen=True, slots=True)
class ViewImageSource:
    """The closed, tool-specific source union for one view_image call."""

    kind: ViewImageSourceKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ViewImageSourceKind) or not self.value:
            raise ValueError("view_image source is invalid")
        if (
            self.kind is ViewImageSourceKind.IMAGE_REF
            and IMAGE_REFERENCE_PATTERN.fullmatch(self.value) is None
        ):
            raise ValueError("view_image image_ref is invalid")

    def provider_value(self) -> dict[str, str]:
        return {self.kind.value: self.value}


def parse_view_image_source(arguments: Mapping[str, object]) -> ViewImageSource:
    """Parse path xor image_ref without adding a general media-source layer."""

    if not isinstance(arguments, Mapping) or any(
        key not in {"path", "image_ref"} for key in arguments
    ):
        raise ValueError("view_image requires exactly one supported source")
    path = arguments.get("path")
    image_ref = arguments.get("image_ref")
    has_path = isinstance(path, str) and bool(path)
    has_ref = isinstance(image_ref, str) and bool(image_ref)
    if has_path == has_ref:
        raise ValueError("view_image requires exactly one of path or image_ref")
    if "path" in arguments and not has_path:
        raise ValueError("view_image path is invalid")
    if "image_ref" in arguments and not has_ref:
        raise ValueError("view_image image_ref is invalid")
    if has_path:
        assert isinstance(path, str)
        return ViewImageSource(ViewImageSourceKind.PATH, path)
    assert isinstance(image_ref, str)
    return ViewImageSource(ViewImageSourceKind.IMAGE_REF, image_ref)


@dataclass(slots=True)
class ViewImageTool(WorkspaceTool):
    name: str = "view_image"

    def read_bounded(
        self, call: ToolCall, *, maximum_bytes: int
    ) -> LocalImageReadCandidate | ToolExecutionResult:
        requested_path = str_arg(call.arguments, "path")
        path = self._resolve_read_path(requested_path)
        if maximum_bytes < 1:
            return self._error(call, "IMAGE_RESOURCE_EXCEEDED", requested_path)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            facts = os.fstat(descriptor)
            if not stat.S_ISREG(facts.st_mode):
                return self._error(call, "IMAGE_PATH_NOT_REGULAR", requested_path)
            if facts.st_size < 1:
                return self._error(call, "IMAGE_DECODE_FAILED", requested_path)
            if facts.st_size > maximum_bytes:
                return self._error(call, "IMAGE_RESOURCE_EXCEEDED", requested_path)
            remaining = maximum_bytes + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if not payload:
                return self._error(call, "IMAGE_DECODE_FAILED", requested_path)
            if len(payload) > maximum_bytes:
                return self._error(call, "IMAGE_RESOURCE_EXCEEDED", requested_path)
            return LocalImageReadCandidate(payload, path, requested_path)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return self._error(call, "IMAGE_READ_FAILED", requested_path)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError("view_image requires the Host image validation owner")

    def _error(
        self, call: ToolCall, code: str, requested_path: str
    ) -> ToolExecutionResult:
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json_text({"error": code, "path": requested_path}),
            metadata={"requested_path": requested_path},
        )


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
class _SearchTool(WorkspaceTool):
    def execute(self, call: ToolCall) -> ToolExecutionResult:
        content_search = self.name == "search_content"
        expression_name = "pattern" if content_search else "glob"
        allowed = {expression_name, "path", "limit", "offset"}
        if content_search:
            allowed.update({"file_glob", "output_mode"})
        if set(call.arguments) - allowed:
            raise ValueError(
                "Unknown search arguments: "
                + ", ".join(sorted(set(call.arguments) - allowed))
            )
        expression = _search_string(call.arguments, expression_name)
        raw_path = _search_string(call.arguments, "path", ".")
        limit = _search_integer(
            call.arguments, "limit", DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT
        )
        offset = _search_integer(call.arguments, "offset", 0, 0)
        mode = (
            _search_string(call.arguments, "output_mode", "content")
            if content_search
            else None
        )
        if mode is not None and mode not in {"content", "files_only", "count"}:
            raise ValueError("unsupported output_mode")
        pattern = (
            _search_string(call.arguments, "file_glob")
            if "file_glob" in call.arguments
            else (None if content_search else expression)
        )
        matcher = (
            glob.compile(
                pattern,
                flags=glob.GLOBSTAR
                | glob.MATCHBASE
                | glob.FORCEUNIX
                | glob.CASE
                | glob.DOTMATCH,
            )
            if pattern is not None
            else None
        )
        path = self._resolve_read_path(raw_path)
        user_home = self._resolved_user_home()
        access_scope = _path_access_scope(path, self.workspace_root, user_home)
        workspace_relative = access_scope == "workspace"
        if not path.exists():
            return _application_error_result(
                self,
                call,
                path,
                _FileApplicationError(
                    "FILE_NOT_FOUND",
                    "The requested search path does not exist.",
                    hint="Check the path and choose an existing file or directory.",
                ),
            )
        if _is_broad_search_root(path, self.workspace_root, user_home):
            return _application_error_result(
                self,
                call,
                path,
                _FileApplicationError(
                    "SEARCH_ROOT_TOO_BROAD",
                    "This search root outside the workspace is too broad.",
                    hint="Choose a specific file or subdirectory.",
                ),
            )
        state = _state_for_workspace(self.workspace_root)
        key = (self.name, expression, path, pattern, limit, offset, mode)
        with state.lock:
            _track_lookup(state, key)
            consecutive = state.consecutive_lookup_count
        if consecutive >= 4:
            return self._result(
                call,
                status=ToolResultState.ERROR,
                output=json_text(
                    {
                        "error": "Repeated search blocked: this exact search has already been returned.",
                        "access_scope": access_scope,
                        "workspace_relative": workspace_relative,
                        "already_searched": consecutive,
                    }
                ),
                metadata={"path": str(path)},
            )
        base = path.parent if path.is_file() else path

        def selected(file: Path) -> bool:
            return matcher is None or matcher.match(file.relative_to(base).as_posix())

        command = [str(private_ripgrep()), "--no-config", "--color", "never"]
        if not content_search:
            command.extend(["--files", "--null"])
        elif mode == "content":
            command.append("--json")
        else:
            command.extend(
                ["--with-filename", "--null", "-l" if mode == "files_only" else "-c"]
            )
        if content_search:
            command.extend(["-e", expression])
        command.extend(["--", str(path)])
        completed = subprocess.run(
            command,
            cwd=self.workspace_root,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise RuntimeError(
                (completed.stderr or completed.stdout)
                .decode("utf-8", "replace")
                .strip()
            )
        payload: dict[str, Any] = {
            "status": "ok",
            "access_scope": access_scope,
            "workspace_relative": workspace_relative,
        }
        if content_search:
            payload["output_mode"] = mode
        if not content_search or mode == "files_only":
            files = [
                Path(os.fsdecode(item))
                for item in completed.stdout.split(b"\0")
                if item
            ]
            entries = sorted(
                _relpath(file, self.workspace_root) for file in files if selected(file)
            )
            page = entries[offset : offset + limit]
            payload.update(files=page, total_count=len(entries))
        elif mode == "count":
            counts: dict[str, int] = {}
            remaining = completed.stdout
            while remaining:
                filename, separator, rest = remaining.partition(b"\0")
                count, newline, remaining = rest.partition(b"\n")
                if not separator or not newline:
                    raise ValueError("Malformed ripgrep count output")
                file = Path(os.fsdecode(filename))
                if selected(file):
                    counts[_relpath(file, self.workspace_root)] = int(count)
            entries = sorted(counts)
            page = entries[offset : offset + limit]
            payload.update(
                counts={file: counts[file] for file in page},
                total_count=sum(counts.values()),
            )
        else:
            entries = []
            for record in completed.stdout.splitlines():
                event = json.loads(record)
                if event["type"] != "match":
                    continue
                data = event["data"]
                file = Path(os.fsdecode(_rg_json_bytes(data["path"])))
                if selected(file):
                    entries.append(
                        {
                            "path": _relpath(file, self.workspace_root),
                            "line": data["line_number"],
                            "content": _rg_json_bytes(data["lines"])
                            .decode("utf-8", "replace")
                            .rstrip("\r\n")[:500],
                        }
                    )
            entries.sort(key=lambda item: (item["path"], item["line"]))
            page = entries[offset : offset + limit]
            payload.update(matches=page, total_count=len(entries))
        payload["truncated"] = offset + len(page) < len(entries)
        if payload["truncated"]:
            payload["_hint"] = (
                f"Results truncated. Continue with offset={offset + len(page)}."
            )
        if consecutive >= 3:
            payload["_warning"] = (
                f"You have run this exact search {consecutive} times consecutively. Use the information you already have."
            )
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={
                "path": str(path),
                expression_name: expression,
                "total_count": payload["total_count"],
                "access_scope": access_scope,
                "workspace_relative": workspace_relative,
            },
        )


def _search_string(
    arguments: Mapping[str, Any], name: str, default: str | None = None
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _search_integer(
    arguments: Mapping[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = arguments.get(name, default)
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{name} must be an integer in the allowed range")
    return value


def _rg_json_bytes(value: dict[str, str]) -> bytes:
    return (
        value["text"].encode("utf-8")
        if "text" in value
        else base64.b64decode(value["bytes"], validate=True)
    )


@dataclass(slots=True)
class SearchContentTool(_SearchTool):
    name: str = "search_content"


@dataclass(slots=True)
class FindFilesTool(_SearchTool):
    name: str = "find_files"


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
                after_text, operations_applied = _stage_edit(
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
                        hint="Submit edits that change the file, or skip editing if it is already correct.",
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
                    "operations_applied": operations_applied,
                    "changed_windows": changed_windows,
                    "changed_windows_truncated": windows_truncated,
                    "diff": diff,
                    "files_modified": [_relpath(path, self.workspace_root)],
                }
            ),
            metadata={"path": str(path), "operations_applied": operations_applied},
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
                except _AtomicFileCreationError as exc:
                    raise _FileApplicationError(
                        "FILE_PUBLICATION_UNCONFIRMED"
                        if exc.published is not False
                        else "FILE_CREATE_FAILED",
                        (
                            "The file was published but successful completion could not be confirmed."
                            if exc.published is True
                            else "The destination may have been created; completion is uncertain."
                            if exc.published is None
                            else "The destination file was not created."
                        ),
                        hint="Inspect the requested path before deciding whether to retry.",
                    ) from exc
                verified_bytes = raw_bytes
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
) -> tuple[str, int]:
    replace_file = tuple(item for item in operations if item.kind == "replace_file")
    if replace_file:
        if len(operations) != 1:
            raise _invalid_operation("replace_file must be the only operation.")
        assert replace_file[0].content is not None
        return _stage_replace_file(layout, replace_file[0].content), 1
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
    # Validate the complete batch first: an unchanged replacement must not
    # bypass observed-range or overlap checks. Only effective edits are staged
    # and counted; execute() rejects NO_OP only if the final bytes are unchanged.
    effective_ranges = [
        (start, end, operation)
        for start, end, operation in ranges
        if operation.kind != "replace_lines"
        or tuple(layout.lines[start - 1 : end]) != operation.lines
    ]
    return (
        _materialize_line_operations(layout, effective_ranges, gaps),
        len(effective_ranges) + len(gaps),
    )


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
    # Artifact export and write_file share this byte-preserving publication.
    # Bind the already permission-resolved parent, rather than re-following
    # mutable path components when creating, linking, verifying or cleaning up.
    parent_fd: int | None = None
    fd: int | None = None
    tmp_name = f".pulsara-{uuid4().hex}.tmp"
    temporary_created = False
    published = False
    publication_attempted = False
    try:
        parent_fd = open_or_create_absolute_directory_nofollow(path.parent)
        fd = os.open(
            tmp_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temporary_created = True
        with os.fdopen(fd, "w+b", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if handle.read() != content:
                raise OSError("written bytes differ from source")
        try:
            publication_attempted = True
            os.link(
                tmp_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
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
        _fsync_directory_fd(parent_fd)
        # A normal local file may change after this call. At publication time,
        # both the bound leaf and the returned absolute location must name the
        # object whose bytes were checked above.
        actual = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        written = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(actual, written):
            raise OSError("published file was replaced")
        visible_parent = open_absolute_directory_nofollow(path.parent)
        try:
            if not os.path.samestat(os.fstat(visible_parent), os.fstat(parent_fd)):
                raise OSError("published parent location changed")
            visible = os.stat(path.name, dir_fd=visible_parent, follow_symlinks=False)
            if not stat.S_ISREG(visible.st_mode) or not os.path.samestat(
                visible, written
            ):
                raise OSError("published file location changed")
        finally:
            os.close(visible_parent)
    except (_AtomicTargetExists, _AtomicNoClobberUnavailable):
        raise
    except Exception as exc:
        if (
            publication_attempted
            and not published
            and parent_fd is not None
            and fd is not None
        ):
            # The publication syscall may have succeeded before an error was
            # observed (for example on a mounted filesystem). Inspect the bound
            # entry rather than equating an exception with no file creation.
            try:
                actual = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                published = True if os.path.samestat(actual, os.fstat(fd)) else None
            except FileNotFoundError:
                published = False
            except OSError:
                published = None
        raise _AtomicFileCreationError(exc, published=published) from exc
    finally:
        close_error: OSError | None = None
        if parent_fd is not None:
            if temporary_created:
                try:
                    # Only remove our own temporary entry, never a substituted
                    # entry or the final file after publication.
                    current = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
                    if fd is not None and os.path.samestat(current, os.fstat(fd)):
                        os.unlink(tmp_name, dir_fd=parent_fd)
                except OSError:
                    pass
        for descriptor in (parent_fd, fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    close_error = close_error or exc
        if close_error is not None:
            raise _AtomicFileCreationError(
                close_error, published=published
            ) from close_error


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        _fsync_directory_fd(fd)
    finally:
        os.close(fd)


def _fsync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise


def _unified_diff(before: str, after: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
