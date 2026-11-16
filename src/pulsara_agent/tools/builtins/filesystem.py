"""Workspace file-system built-in tools.

Reads preserve their complete selected text. Edits apply exact unique text
replacement or replace a whole existing file; write_file is create-only.
"""

from __future__ import annotations

from pulsara_agent.identity import ID_PAYLOAD_PATTERN

import difflib
import errno
import base64
import os
import re
import tempfile
import threading
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
)
from pulsara_agent.ports.tool_output import (
    ToolOutputContentError,
    ToolOutputReadPort,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool, WritePathScope


UTF8_BOM = "\ufeff"
UTF8_BOM_BYTES = b"\xef\xbb\xbf"
IMAGE_REFERENCE_PATTERN = re.compile("image:" + ID_PAYLOAD_PATTERN)
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

    def lock_for_path(self, path: Path) -> threading.Lock:
        with self.lock:
            return self.path_locks.setdefault(path, threading.Lock())


@dataclass(frozen=True, slots=True)
class _TextLayout:
    text: str
    had_bom: bool


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

    def provider_value(self) -> dict[str, object]:
        return {"target": {"kind": self.kind.name, self.kind.value: self.value}}


def path_target_arg(
    arguments: Mapping[str, object], *, default: str | None = None
) -> str:
    if "target" not in arguments and default is not None:
        return default
    target = arguments.get("target")
    if (
        not isinstance(target, Mapping)
        or set(target) != {"kind", "path"}
        or target.get("kind") != "PATH"
        or not isinstance(target.get("path"), str)
        or not target["path"]
    ):
        raise ValueError("target must be a PATH object with a non-empty path")
    return target["path"]


def file_target(path: str) -> dict[str, str]:
    return {"kind": "PATH", "path": path}


def parse_view_image_source(arguments: Mapping[str, object]) -> ViewImageSource:
    """Parse the closed PATH / IMAGE_REF target without granting read authority."""
    if not isinstance(arguments, Mapping) or set(arguments) != {"target"}:
        raise ValueError("view_image requires exactly one target")
    target = arguments["target"]
    if isinstance(target, Mapping) and target.get("kind") == "PATH":
        return ViewImageSource(ViewImageSourceKind.PATH, path_target_arg(arguments))
    if (
        not isinstance(target, Mapping)
        or set(target) != {"kind", "image_ref"}
        or target.get("kind") != "IMAGE_REF"
        or not isinstance(target.get("image_ref"), str)
    ):
        raise ValueError("view_image target must be PATH or IMAGE_REF")
    return ViewImageSource(ViewImageSourceKind.IMAGE_REF, target["image_ref"])


@dataclass(slots=True)
class ViewImageTool(WorkspaceTool):
    name: str = "view_image"

    def read_bounded(
        self, call: ToolCall, *, maximum_bytes: int
    ) -> LocalImageReadCandidate | ToolExecutionResult:
        requested_path = path_target_arg(call.arguments)
        path = self._resolve_read_path(requested_path)
        try:
            return LocalImageReadCandidate(
                read_image_file_bounded(path, maximum_bytes=maximum_bytes),
                path,
                requested_path,
            )
        except TimeoutError:
            raise
        except ValueError as exc:
            return self._error(call, str(exc), requested_path)
        except FileNotFoundError:
            return self._error(call, "文件不存在", requested_path)
        except OSError as exc:
            return self._error(call, str(exc), requested_path)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError("view_image requires the Host image validation owner")

    def _error(self, call: ToolCall, code: str, requested_path: str) -> ToolExecutionResult:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        target = file_target(_relpath(self._resolve_read_path(requested_path), self.workspace_root))
        return ToolExecutionResult(call_id=call.id, tool_name=call.name, status=ToolResultState.ERROR,
            business_value=ToolBusinessValue({"target": target},
                (ResultPart("text", f"无法读取图片 {target['path']}：{code}。\n"),)))


def _state_for_workspace(workspace_root: Path) -> _WorkspaceFileState:
    root = workspace_root.resolve()
    with _STATES_LOCK:
        state = _STATES.get(root)
        if state is None:
            state = _WorkspaceFileState()
            _STATES[root] = state
        return state


def file_business_result(
    tool, call, *, fields=None, parts=(), status=ToolResultState.SUCCESS, metadata=None
):
    from pulsara_agent.ports.tool_execution import ToolBusinessValue

    value = ToolBusinessValue(fields or {}, tuple(parts))
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=status,
        business_value=value,
        metadata=metadata or {},
    )


@dataclass(slots=True)
class ReadFileTool(WorkspaceTool):
    name: str = "read_file"
    result_source_reader: ToolOutputReadPort | None = None

    def read_code_mode_source(self, raw_path: str) -> str:
        """Read complete code source through the existing file path owner."""
        path = self._resolve_read_path(raw_path)
        with _state_for_workspace(self.workspace_root).lock_for_path(path):
            return path.read_bytes().decode("utf-8")

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart

        target = call.arguments.get("target")
        if isinstance(target, Mapping) and target.get("kind") == "NEXT":
            if set(call.arguments) != {"target"}:
                raise ValueError("NEXT accepts target only")
            if self.result_source_reader is None:
                raise RuntimeError("result continuation owner is not bound")
            try:
                continuation = self.result_source_reader.read_native_page(target)
                from pulsara_agent.ports.tool_execution import ToolBusinessValue
                return ToolExecutionResult(call_id=call.id,tool_name=call.name,status=ToolResultState.SUCCESS,
                    business_value=ToolBusinessValue(),native_continuation=continuation)
            except (ValueError,KeyError,ToolOutputContentError) as exc:
                return file_business_result(self,call,parts=(ResultPart("text",str(exc)),),status=ToolResultState.ERROR)
        if set(call.arguments) - {"target", "offset", "limit"}:
            raise ValueError("Unknown read_file arguments")
        offset = call.arguments.get("offset", 1)
        limit = call.arguments.get("limit")
        if type(offset) is not int or offset == 0:
            raise ValueError("offset must be a nonzero integer")
        if "limit" in call.arguments and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be a positive integer")
        path = self._resolve_read_path(path_target_arg(call.arguments))
        try:
            with _state_for_workspace(self.workspace_root).lock_for_path(path):
                raw, layout = _read_existing_text(path)
                lines = _split_lf_keepends(layout.text)
                start = offset - 1 if offset > 0 else max(0, len(lines) + offset)
                selected = "".join(
                    lines[start:] if limit is None else lines[start : start + limit]
                )
            return file_business_result(
                self,
                call,
                fields={
                    "target": file_target(_relpath(path, self.workspace_root)),
                    "meta": {
                        "total_lines": len(lines),
                        "file_size": len(raw),
                        "had_utf8_bom": layout.had_bom,
                    },
                },
                parts=(ResultPart("text", selected),) if selected else (),
                metadata={"path": str(path)},
            )
        except (_FileApplicationError, OSError) as exc:
            return _application_error_result(self, call, path, exc)


@dataclass(slots=True)
class _SearchTool(WorkspaceTool):
    def execute(self, call: ToolCall) -> ToolExecutionResult:
        from pulsara_agent.tools.builtins.filesystem_search import execute_search

        return execute_search(self, call)


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
    if not isinstance(value, Mapping) or set(value) not in ({"text"}, {"bytes"}):
        raise ValueError("ripgrep data must contain exactly one of text or bytes")
    field = "text" if "text" in value else "bytes"
    if not isinstance(value[field], str):
        raise ValueError("ripgrep text and bytes values must be strings")
    return (
        value[field].encode("utf-8")
        if field == "text"
        else base64.b64decode(value[field], validate=True)
    )


@dataclass(slots=True)
class SearchContentTool(_SearchTool):
    name: str = "search_file_content"


@dataclass(slots=True)
class FindFilesTool(_SearchTool):
    name: str = "find_files"


@dataclass(slots=True)
class EditFileTool(WorkspaceTool):
    name: str = "edit_file"

    def execute(
        self, call: ToolCall, *, write_scope=WritePathScope.WORKSPACE
    ) -> ToolExecutionResult:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart

        if set(call.arguments) not in (
            {"target", "content"},
            {"target", "old_text", "new_text"},
        ):
            raise ValueError(
                "edit_file requires target and content, or old_text and new_text"
            )
        path = self._resolve_path(
            path_target_arg(call.arguments), write_scope=write_scope
        )
        try:
            with _state_for_workspace(self.workspace_root).lock_for_path(path):
                before, layout = _read_existing_text(path)
                if "content" in call.arguments:
                    content = call.arguments["content"]
                    if not isinstance(content, str):
                        raise ValueError("content must be a string")
                    after = _validate_new_text_content(path, content)
                else:
                    old, new = call.arguments["old_text"], call.arguments["new_text"]
                    if not isinstance(old, str) or not isinstance(new, str):
                        raise ValueError("old_text and new_text must be strings")
                    _validate_new_text_content(path, old)
                    _validate_new_text_content(path, new)
                    if not old:
                        if layout.text:
                            raise ValueError(
                                "Empty old_text requires an existing empty body"
                            )
                        text = new
                    else:
                        at = layout.text.find(old)
                        if at < 0:
                            raise ValueError(
                                "old_text was not found in the current file"
                            )
                        if layout.text.find(old, at + 1) >= 0:
                            raise ValueError("old_text has multiple matching positions")
                        text = layout.text[:at] + new + layout.text[at + len(old) :]
                    after = _encode_text(text, had_bom=layout.had_bom)
                if after == before:
                    parts = ()
                else:
                    diff = _unified_diff(
                        before.decode("utf-8"),
                        after.decode("utf-8"),
                        _relpath(path, self.workspace_root),
                    )
                    _atomic_replace_bytes(path, after)
                    parts = (ResultPart("text", diff),)
            return file_business_result(
                self,
                call,
                fields={"target": file_target(_relpath(path, self.workspace_root))},
                parts=parts,
            )
        except (_FileApplicationError, OSError, ValueError) as exc:
            return _application_error_result(self, call, path, exc)


@dataclass(slots=True)
class WriteFileTool(WorkspaceTool):
    name: str = "write_file"
    result_source_reader: ToolOutputReadPort | None = None

    def execute(
        self, call: ToolCall, *, write_scope=WritePathScope.WORKSPACE
    ) -> ToolExecutionResult:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.conversation_kernel.tool_outputs import (
            project_tool_result_export,
        )

        if set(call.arguments) not in (
            {"target", "content"},
            {"target", "tool_result_target"},
        ):
            raise ValueError(
                "write_file requires target and exactly one of content or tool_result_target"
            )
        raw_path = path_target_arg(call.arguments)
        path = self._resolve_path(raw_path, write_scope=write_scope)
        leaf = _requested_leaf_path(raw_path, self.workspace_root)
        try:
            exporting = "tool_result_target" in call.arguments
            if exporting:
                if self.result_source_reader is None:
                    raise RuntimeError("result source owner is not bound")
                raw = project_tool_result_export(
                    self.result_source_reader.read_result(
                        call.arguments["tool_result_target"]
                    ).value
                )
            else:
                content = call.arguments["content"]
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                raw = _validate_new_text_content(path, content)
            with _state_for_workspace(self.workspace_root).lock_for_path(path):
                if leaf.is_symlink() or path.exists() or path.is_symlink():
                    raise ValueError(
                        "write_file creates new files and cannot overwrite an existing path"
                    )
                try:
                    _atomic_create_bytes(path, raw)
                except _AtomicTargetExists as exc:
                    raise ValueError(
                        "The destination appeared before publication; no file was replaced"
                    ) from exc
                except _AtomicNoClobberUnavailable as exc:
                    raise ValueError(
                        "This file system does not support atomic no-clobber file creation; no file was created"
                    ) from exc
                except _AtomicFileCreationError as exc:
                    raise ValueError(
                        "File publication is unconfirmed; inspect the destination before retrying"
                        if exc.published is not False
                        else "File creation failed before publication"
                    ) from exc
            return file_business_result(
                self,
                call,
                fields={
                    "target": file_target(_relpath(path, self.workspace_root)),
                    "meta": {"bytes_written": len(raw)},
                },
                parts=(
                    ResultPart(
                        "text", "已导出完整工具结果。" if exporting else "已创建文件。"
                    ),
                ),
                metadata={"path": str(path), "bytes": len(raw)},
            )
        except (
            _FileApplicationError,
            OSError,
            ValueError,
            KeyError,
            ToolOutputContentError,
        ) as exc:
            return _application_error_result(self, call, path, exc)


def _requested_leaf_path(raw_path: str | None, workspace_root: Path) -> Path:
    """Resolve parent components while retaining the requested final directory entry."""
    if not raw_path or not raw_path.strip():
        raise ValueError("path is required")
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        requested = workspace_root / requested
    return requested.parent.resolve() / requested.name


def _application_error_result(tool, call, path, exc):
    from pulsara_agent.conversation_kernel.content_parts import ResultPart

    hint = getattr(exc, "hint", "")
    explanation = f"{path}: {exc}" + ("\n" + hint if hint else "")
    return file_business_result(
        tool,
        call,
        fields={"target": file_target(_relpath(path, tool.workspace_root))},
        parts=(ResultPart("text", explanation),),
        status=ToolResultState.ERROR,
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
    return _TextLayout(text=text, had_bom=had_bom)


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
    # Both write_file content inputs use this byte-preserving publication.
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


def _split_lf_keepends(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    pieces = text.split("\n")
    return tuple(piece + "\n" for piece in pieces[:-1]) + (
        (pieces[-1],) if pieces[-1] else ()
    )


def _unified_diff(before: str, after: str, filename: str) -> str:
    lines = difflib.unified_diff(
        _split_lf_keepends(before),
        _split_lf_keepends(after),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )


def read_image_file_bounded(path: Path, *, maximum_bytes: int) -> bytes:
    """Shared physical reader; callers own authorization and image validation."""
    if maximum_bytes < 1:
        raise ValueError("IMAGE_RESOURCE_EXCEEDED")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError("IMAGE_PATH_NOT_REGULAR")
        if facts.st_size < 1:
            raise ValueError("IMAGE_DECODE_FAILED")
        if facts.st_size > maximum_bytes:
            raise ValueError("IMAGE_RESOURCE_EXCEEDED")
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
            raise ValueError("IMAGE_DECODE_FAILED")
        if len(payload) > maximum_bytes:
            raise ValueError("IMAGE_RESOURCE_EXCEEDED")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)
