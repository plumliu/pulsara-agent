"""Workspace file-system built-in tools.

Reads expose exact-byte content revisions and process-local seen-line
observations. Existing files are changed only through revision-anchored,
deterministic line operations; ``write_file`` is create-only.
"""

from __future__ import annotations

from pulsara_agent.identity import content_revision, REVISION_PATTERN, ID_PAYLOAD_PATTERN

import difflib
import errno
import base64
import os
import re
import tempfile
import threading
import stat
from dataclasses import dataclass, field, replace
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
    ToolCall, ToolExecutionResult, ToolOutputSourceCoverage, ToolOutputSourceFormatHint,
)
from pulsara_agent.ports.tool_output import (
    ToolOutputBodyCandidate,
    ToolOutputContentError,
    ToolOutputReadPort,
)
from pulsara_agent.primitives.text_paging import (
    TextPosition, line_starts, paginate_text,
)
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget, ToolResultPageBoundary,
)
from pulsara_agent.tools.builtins.filesystem_contracts import (
    DEFAULT_READ_LINES, MAX_READ_LINES,
)
from pulsara_agent.tools.builtins.schemas import (
    json_text,
    str_arg,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool, WritePathScope


UTF8_BOM = "\ufeff"
UTF8_BOM_BYTES = b"\xef\xbb\xbf"
CONTENT_REVISION_PATTERN = re.compile(REVISION_PATTERN)
IMAGE_REFERENCE_PATTERN = re.compile("image:" + ID_PAYLOAD_PATTERN)
CHANGED_WINDOW_CONTEXT_LINES = 3
# Keep changed-window previews near their established display size; the actual
# complete native carrier is additionally quoted before replacing the file.
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
                seen_line_fragments=(current.seen_line_fragments
                    if current is not None and current.content_revision == revision else ()),
            )

    def observation(self, path: Path) -> "_FileObservationSlot | None":
        with self.lock:
            return self.observations.get(path)

    def observe_fragments(
        self, path: Path, revision: str,
        fragments: tuple[tuple[int, int, int, int], ...],
    ) -> None:
        """Only promote a line once its actually delivered characters cover it."""
        with self.lock:
            old = self.observations.get(path)
            same = old is not None and old.content_revision == revision
            seen = list(old.seen_line_intervals if same else ())
            partial = dict(old.seen_line_fragments if same else ())
            for line, start, end, length in fragments:
                if any(first <= line <= last for first, last in seen):
                    continue
                delivered = ((start, end - 1),) if end > start else ()
                intervals = _merge_intervals((*partial.get(line, ()), *delivered))
                if length == 0 or any(a == 0 and b >= length - 1 for a, b in intervals):
                    seen.append((line, line))
                    partial.pop(line, None)
                else:
                    partial[line] = intervals
            self.observations[path] = _FileObservationSlot(
                revision, _merge_intervals(tuple(seen)), tuple(sorted(partial.items()))
            )

    def clear_observation(self, path: Path) -> None:
        with self.lock:
            self.observations.pop(path, None)


@dataclass(frozen=True, slots=True)
class _FileObservationSlot:
    content_revision: str
    seen_line_intervals: tuple[tuple[int, int], ...]
    seen_line_fragments: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = ()


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

    def provider_value(self) -> dict[str, object]:
        return {"target": {"kind": self.kind.name, self.kind.value: self.value}}


def path_target_arg(arguments: Mapping[str, object], *, default: str | None = None) -> str:
    if "target" not in arguments and default is not None:
        return default
    target = arguments.get("target")
    if (not isinstance(target, Mapping) or set(target) != {"kind", "path"}
        or target.get("kind") != "PATH" or not isinstance(target.get("path"), str)
        or not target["path"]):
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
    if (not isinstance(target, Mapping) or set(target) != {"kind", "image_ref"}
        or target.get("kind") != "IMAGE_REF" or not isinstance(target.get("image_ref"), str)):
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
                read_image_file_bounded(path, maximum_bytes=maximum_bytes), path, requested_path
            )
        except TimeoutError:
            raise
        except ValueError as exc:
            return self._error(call, str(exc), requested_path)
        except OSError:
            return self._error(call, "IMAGE_READ_FAILED", requested_path)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError("view_image requires the Host image validation owner")

    def _error(
        self, call: ToolCall, code: str, requested_path: str
    ) -> ToolExecutionResult:
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json_text({"error": code, "target": file_target(requested_path)}),
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


def _page_budget(call: ToolCall) -> ToolResultPageBudget:
    # Production calls receive their actual provider allowance from the runtime.
    # Direct low-level tests and internal calls use the same physical Q owner.
    return call.page_budget or ToolResultPageBudget(tool_call_id=call.id)


def _read_continuation(arguments: Mapping[str, object], *, path: bool) -> TextPosition:
    value = arguments.get("continuation")
    if value is None:
        if "continuation" in arguments:
            raise ValueError("continuation cannot be null")
        return TextPosition(_search_integer(arguments, "offset", 1, 1))
    required = {"line", "char_offset", "content_revision"} if path else {"line", "char_offset"}
    if not isinstance(value, Mapping) or set(value) != required or "offset" in arguments:
        raise ValueError("Invalid text continuation")
    if path and (not isinstance(value["content_revision"], str)
                 or CONTENT_REVISION_PATTERN.fullmatch(value["content_revision"]) is None):
        raise ValueError("Invalid continuation content_revision")
    return TextPosition(value["line"], value["char_offset"])


def _complete_page_result(tool: WorkspaceTool, call: ToolCall, payload: Mapping[str, object],
                          *, metadata: dict[str, Any] | None = None,
                          memory_ids: tuple[str, ...] = ()) -> ToolExecutionResult:
    result = tool._result(call, status=ToolResultState.SUCCESS,
                          output=json_text(payload), metadata=metadata)
    return replace(result, page_complete=True, model_visible_memory_fact_ids=memory_ids)


def _text_boundary(tool: WorkspaceTool, call: ToolCall, target: Mapping[str, object],
                   error: Exception) -> ToolExecutionResult:
    return tool._result(call, status=ToolResultState.ERROR,
                        output=json_text({"status": "resource_boundary", "error": "TEXT_PAGE_NOT_INLINEABLE",
                                          "target": dict(target), "message": str(error)}))


@dataclass(slots=True)
class ReadFileTool(WorkspaceTool):
    name: str = "read_file"
    result_source_reader: ToolOutputReadPort | None = None

    def read_code_mode_source(self, raw_path: str, *, maximum_bytes: int) -> str:
        """Resolve source through the file owner, without granting edit observation."""
        path = self._resolve_read_path(raw_path)
        state = _state_for_workspace(self.workspace_root)
        with state.lock_for_path(path):
            with path.open("rb") as source:
                content = source.read(maximum_bytes + 1)
            if len(content) > maximum_bytes:
                raise ValueError("Code Mode source exceeds its bridge resource boundary")
            return content.decode("utf-8")

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        target = call.arguments.get("target")
        if isinstance(target, Mapping) and target.get("kind") == "SESSION_ENTRY":
            return self._read_result_body(call, target)
        if set(call.arguments) - {"target", "offset", "limit", "continuation"}:
            raise ValueError("Unknown read_file arguments")
        position = _read_continuation(call.arguments, path=True)
        limit = _search_integer(call.arguments, "limit", DEFAULT_READ_LINES, 1, MAX_READ_LINES)
        path = self._resolve_read_path(path_target_arg(call.arguments))
        target = file_target(_relpath(path, self.workspace_root))
        access_scope = _path_access_scope(path, self.workspace_root, self._resolved_user_home())
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        try:
            with path_lock:
                raw_bytes, layout = _read_existing_text(path)
                revision = _content_revision(raw_bytes)
                continuation = call.arguments.get("continuation")
                if continuation is not None and continuation["content_revision"] != revision:
                    raise _FileApplicationError("CONTENT_REVISION_MISMATCH", "The file changed during this read.",
                                                hint="Start a fresh read_file call for the required current range.",
                                                details={"current_revision": revision})
                previous_observation = state.observation(path)
                # PATH presentation keeps the established N|text format. The
                # source revision still covers every original byte and newline.
                text = "\n".join(layout.lines) + ("\n" if layout.lines else "")
                budget = _page_budget(call)

                def payload(content: str, next_position: TextPosition | None) -> dict[str, object]:
                    if next_position is None or next_position.char_offset == 0:
                        content = content.removesuffix("\n")
                    next_call = None
                    if next_position is not None:
                        cursor = {**next_position.public_value(), "content_revision": revision}
                        next_call = {"tool": self.name, "arguments": {
                            "target": target, "limit": limit, "continuation": cursor}}
                    return {"status": "ok", "target": target, "content_revision": revision,
                            "access_scope": access_scope, "workspace_relative": access_scope == "workspace",
                            "position": position.public_value(), "total_lines": len(layout.lines),
                            "file_size": len(raw_bytes), "content": content, "next": next_call,
                            **({"had_utf8_bom": True} if layout.had_bom else {})}

                page = paginate_text(text, position=position, max_lines=limit, line_labels=True,
                                     fits=lambda content, next_position: budget.fits(json_text(payload(content, next_position))))
                starts = line_starts(text)
                fragments = []
                for index, line in enumerate(layout.lines):
                    begin = starts[index]
                    end = begin + len(line)
                    if page.start_offset <= end and page.end_offset > begin:
                        fragments.append((index + 1, max(0, page.start_offset - begin),
                                          min(len(line), page.end_offset - begin), len(line)))
                frozen_fragments = tuple(fragments)
                if not call.defer_file_observation:
                    state.observe_fragments(path, revision, frozen_fragments)
                result = _complete_page_result(self, call, payload(page.text, page.next_position),
                    metadata={"path": str(path), "access_scope": access_scope,
                              "workspace_relative": access_scope == "workspace"})
        except _FileApplicationError as exc:
            return _application_error_result(self, call, path, exc)
        except ToolResultPageBoundary as exc:
            return _text_boundary(self, call, target, exc)
        if call.defer_file_observation:
            def delivered() -> None:
                # An old delayed delivery cannot overwrite a newer revision.
                with path_lock:
                    try:
                        current_bytes, _ = _read_existing_text(path)
                    except _FileApplicationError:
                        return
                    observed = state.observation(path)
                    if (_content_revision(current_bytes) == revision
                        and (observed is previous_observation
                             or (observed is not None and observed.content_revision == revision))):
                        state.observe_fragments(path, revision, frozen_fragments)
            result = replace(result, delivery_succeeded=delivered)
        return result

    def _read_result_body(self, call: ToolCall, target: Mapping[str, object]) -> ToolExecutionResult:
        if set(call.arguments) - {"target", "continuation"}:
            raise ValueError("Saved-result reads accept target and continuation only")
        position = _read_continuation(call.arguments, path=False)
        if self.result_source_reader is None:
            raise RuntimeError("read_file result source owner is not bound")
        try:
            view = self.result_source_reader.read_body(target)
            text = view.content.decode("utf-8")
            budget = _page_budget(call)
            def payload(content: str, next_position: TextPosition | None) -> dict[str, object]:
                next_call = None if next_position is None else {"tool": self.name, "arguments": {
                    "target": dict(view.target), "continuation": next_position.public_value()}}
                return {"status": "ok", "target": dict(view.target), "position": position.public_value(),
                        "result_facts": dict(view.required_facts),
                        "media_type": view.media_type, "codec": view.codec,
                        "source_coverage": view.source_coverage.value,
                        "source_coverage_reason": (None if view.source_coverage_reason is None
                                                   else view.source_coverage_reason.value),
                        "content": content, "next": next_call}
            page = paginate_text(text, position=position,
                fits=lambda content, next_position: budget.fits(json_text(payload(content, next_position)),
                    model_visible_memory_fact_ids=view.model_visible_memory_fact_ids))
            return _complete_page_result(self, call, payload(page.text, page.next_position),
                                         memory_ids=view.model_visible_memory_fact_ids)
        except KeyError:
            return self._result(call, status=ToolResultState.ERROR,
                                output=json_text({"status": "not_found", "target": dict(target)}))
        except ToolOutputContentError as exc:
            return self._result(call, status=ToolResultState.ERROR,
                                output=json_text({"status": "content_error", "target": dict(target), "error": str(exc)}))
        except ToolResultPageBoundary as exc:
            return _text_boundary(self, call, target, exc)


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
    return value[field].encode("utf-8") if field == "text" else base64.b64decode(value[field], validate=True)


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
            path_target_arg(call.arguments), write_scope=write_scope
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
                budget = _page_budget(call)
                required = {"status": "ok", "target": file_target(_relpath(path, self.workspace_root)),
                    "base_revision": base_revision, "content_revision": new_revision,
                    "operations_applied": operations_applied,
                    "files_modified": [_relpath(path, self.workspace_root)]}
                payload = {**required, "changed_windows": changed_windows,
                           "changed_windows_truncated": windows_truncated, "diff": diff}
                native_payload = payload
                if not budget.fits(json_text(payload)):
                    native_payload = {**required, "changed_windows": changed_windows,
                                      "changed_windows_truncated": windows_truncated}
                    while changed_windows and not budget.fits(json_text(native_payload), include_read=budget.read is not None):
                        changed_windows = changed_windows[:-1]
                        seen_intervals = seen_intervals[:-1]
                        windows_truncated = True
                        native_payload = {**required, "changed_windows": changed_windows,
                                          "changed_windows_truncated": True}
                    if not budget.fits(json_text(native_payload), include_read=budget.read is not None):
                        return _text_boundary(self, call, required["target"],
                            ToolResultPageBoundary("The edit receipt does not fit. The file was not changed."))
                    payload = {**required, "changed_windows": changed_windows,
                               "changed_windows_truncated": windows_truncated, "diff": diff}
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
                if not call.defer_file_observation:
                    state.observe(path, new_revision, seen_intervals)
        except _FileApplicationError as exc:
            return _application_error_result(self, call, path, exc)

        result = self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
            metadata={"path": str(path), "operations_applied": operations_applied},
        )
        result = replace(result, native_output=json_text(native_payload), required_facts=required,
            output_body_candidate=(ToolOutputBodyCandidate(diff, ToolOutputSourceFormatHint.TEXT,
                ToolOutputSourceCoverage.COMPLETE) if diff else None))
        if call.defer_file_observation:
            def delivered() -> None:
                with path_lock:
                    try:
                        current, _ = _read_existing_text(path)
                    except _FileApplicationError:
                        return
                    if _content_revision(current) == new_revision:
                        state.observe(path, new_revision, seen_intervals)
            result = replace(result, delivery_succeeded=delivered)
        return result


@dataclass(slots=True)
class WriteFileTool(WorkspaceTool):
    name: str = "write_file"
    result_source_reader: ToolOutputReadPort | None = None

    def execute(
        self,
        call: ToolCall,
        *,
        write_scope: WritePathScope = WritePathScope.WORKSPACE,
    ) -> ToolExecutionResult:
        if set(call.arguments) not in ({"target", "content"}, {"target", "source"}):
            raise ValueError("write_file requires target and exactly one of content or source")
        raw_path = path_target_arg(call.arguments)
        path = self._resolve_path(raw_path, write_scope=write_scope)
        requested_leaf = _requested_leaf_path(raw_path, self.workspace_root)
        if "source" in call.arguments:
            if self.result_source_reader is None:
                raise RuntimeError("write_file result source owner is not bound")
            source = call.arguments["source"]
            if not isinstance(source, Mapping) or source.get("kind") != "SESSION_ENTRY":
                raise ValueError("source must be a SESSION_ENTRY target")
            try:
                source_body = self.result_source_reader.read_body(source)
                raw_bytes = source_body.content
            except KeyError:
                return self._result(call, status=ToolResultState.ERROR,
                                    output=json_text({"status": "not_found", "target": dict(source)}))
            except ToolOutputContentError as exc:
                return self._result(call, status=ToolResultState.ERROR,
                                    output=json_text({"status": "content_error", "target": dict(source), "error": str(exc)}))
        else:
            content = str_arg(call.arguments, "content")
            if content is None:
                raise ValueError("content must be a string")
            try:
                raw_bytes = _validate_new_text_content(path, content)
            except _FileApplicationError as exc:
                return _application_error_result(self, call, path, exc)
        revision = _content_revision(raw_bytes)
        payload: dict[str, Any] = {
            "status": "ok", "target": file_target(_relpath(path, self.workspace_root)),
            "content_revision": revision, "bytes_written": len(raw_bytes),
            "files_modified": [_relpath(path, self.workspace_root)],
        }
        if "source" in call.arguments:
            payload.update(source=dict(source_body.target),
                source_coverage=source_body.source_coverage.value,
                source_coverage_reason=(None if source_body.source_coverage_reason is None
                                        else source_body.source_coverage_reason.value))
        if not _page_budget(call).fits(json_text(payload)):
            return _text_boundary(self, call, payload["target"],
                                  ToolResultPageBoundary("The file creation receipt does not fit. No file was created."))
        state = _state_for_workspace(self.workspace_root)
        path_lock = state.lock_for_path(path)
        try:
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

        return replace(_complete_page_result(self, call, payload,
            metadata={"path": str(path), "bytes": len(verified_bytes)}),
            required_facts={key: payload[key] for key in ("status", "target", "content_revision", "bytes_written")})


def _content_revision(raw_bytes: bytes) -> str:
    return content_revision(raw_bytes)


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
        "target": file_target(_relpath(path, tool.workspace_root)),
        "_hint": error.hint,
    }
    payload.update(error.details)
    result = tool._result(
        call,
        status=ToolResultState.ERROR,
        output=json_text(payload),
        metadata={"path": str(path), "error_code": error.code},
    )
    if call.name in {"write_file", "edit_file"}:
        result = replace(result, required_facts={
            "error": error.code, "target": payload["target"], "message": error.message})
    return result


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
    required = {"target", "base_revision", "operations"}
    if set(arguments) != required:
        raise _FileApplicationError(
            "INVALID_OPERATION_COMBINATION",
            "edit_file accepts only target, base_revision, and operations.",
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


def _unified_diff(before: str, after: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


def read_image_file_bounded(path: Path, *, maximum_bytes: int) -> bytes:
    """Shared physical reader; callers own authorization and image validation."""
    if maximum_bytes < 1:
        raise ValueError("IMAGE_RESOURCE_EXCEEDED")
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
