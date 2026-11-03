"""Call-local ripgrep enumeration and file-position continuation.

Ripgrep owns traversal, ignore rules, encoding, matching and binary detection.
Pulsara only selects eligible paths and packs the observed records. No producer
or search state survives a call.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
import json
import os
from pathlib import Path, PurePosixPath
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping

from wcmatch import glob

from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary
from pulsara_agent.ripgrep import private_ripgrep
from pulsara_agent.tools.builtins.filesystem_contracts import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
)
from pulsara_agent.tools.builtins.schemas import json_text
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


# Matching-line excerpts show bounded context around an actual submatch.
# read_file supplies the complete current source when the task needs it.
SEARCH_EXCERPT_CHARS = 500
# Preserve the existing search operation deadline, across all its subprocesses.
SEARCH_TIMEOUT_SECONDS = 60
_PIPE_CHUNK_BYTES = 64 * 1024


class _Diagnostics:
    def __init__(self, partial: bool) -> None:
        self.partial = partial
        self.data = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self.lock:
            self.partial = True
            # Diagnostics are a displayed part of one page, not a retained
            # unbounded stderr log. Omission is explicit and does not clear the
            # chain's sticky partial fact.
            available = MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES - len(
                self.data
            )
            self.data.extend(chunk[:available])
            self.truncated |= len(chunk) > available

    def snapshot(self) -> tuple[bool, str, bool]:
        with self.lock:
            return (
                self.partial,
                bytes(self.data).decode("utf-8", "replace"),
                self.truncated,
            )


class _Producer:
    """Drain stderr concurrently; stdout is bounded pipe chunks, never communicate()."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        call: ToolCall,
        diagnostics: _Diagnostics,
        deadline: float,
    ) -> None:
        self.call, self.diagnostics, self.deadline = call, diagnostics, deadline
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.queue: queue.Queue[bytes | None | BaseException] = queue.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.natural = False
        self.threads = [
            threading.Thread(target=self._stdout, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        ]
        try:
            for thread in self.threads:
                thread.start()
        except BaseException:
            self.stopped.set()
            self.process.kill()
            self.process.wait()
            for thread in self.threads:
                if thread.ident is not None:
                    thread.join()
            self.process.stdout.close()
            self.process.stderr.close()
            raise

    def _put(self, value: bytes | None | BaseException) -> None:
        while not self.stopped.is_set():
            try:
                self.queue.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _stdout(self) -> None:
        try:
            while chunk := os.read(self.process.stdout.fileno(), _PIPE_CHUNK_BYTES):
                self._put(chunk)
                if self.stopped.is_set():
                    break
            self._put(None)
        except BaseException as error:
            self._put(error)

    def _stderr(self) -> None:
        while chunk := os.read(self.process.stderr.fileno(), _PIPE_CHUNK_BYTES):
            self.diagnostics.add(chunk)

    def chunks(self) -> Iterator[bytes]:
        while True:
            cancelled = self.call.cancel_requested
            if cancelled is not None and cancelled():
                raise InterruptedError("Search was cancelled")
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Search exceeded its existing operation deadline")
            try:
                value = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if value is None:
                self.natural = True
                return
            if isinstance(value, BaseException):
                raise value
            yield value

    def records(self, separator: bytes) -> Iterator[bytes]:
        pending = bytearray()
        search_from = 0
        for chunk in self.chunks():
            pending.extend(chunk)
            while (end := pending.find(separator, search_from)) >= 0:
                value = bytes(pending[:end])
                del pending[: end + len(separator)]
                search_from = 0
                yield value
            search_from = max(0, len(pending) - len(separator) + 1)
        if pending:
            raise ValueError("Malformed ripgrep output: unfinished record")

    def close(self) -> int:
        self.stopped.set()
        termination_sent = False
        if not self.natural and self.process.poll() is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            else:
                # Popen.kill may discover/reap an already exited child instead
                # of sending a signal. Its public returncode is then populated.
                termination_sent = self.process.returncode is None
        code = self.process.wait()
        for thread in self.threads:
            thread.join()
        self.process.stdout.close()
        self.process.stderr.close()
        expected_stop = termination_sent and code == -signal.SIGKILL
        if code not in (0, 1) and not expected_stop:
            self.diagnostics.add(f"ripgrep exited with status {code}.\n".encode())
        return code

    def __enter__(self) -> "_Producer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _Cursor:
    path: str
    line: int | None
    partial: bool

    def value(self, partial: bool) -> dict[str, object]:
        return {
            "path": self.path,
            "partial": partial,
            **({"line": self.line} if self.line is not None else {}),
        }


def _path_key(path: str) -> tuple[bytes, ...]:
    # Native rg --sort path orders directory components, not whole path strings.
    return tuple(os.fsencode(part) for part in PurePosixPath(path).parts)


def _cursor(value: object, *, content: bool) -> _Cursor | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {"path", "line", "partial"}:
        raise ValueError("Invalid search continuation")
    path = value.get("path")
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError(
            "continuation path must be relative to the original search root"
        )
    if type(value.get("partial")) is not bool:
        raise ValueError("continuation partial must be a boolean")
    line = value.get("line")
    if "line" in value and (not content or type(line) is not int or line < 1):
        raise ValueError("line continuation is valid only for content searches")
    return _Cursor(path, line, value["partial"])


def _argv_budget() -> int:
    if os.name == "nt":
        # CreateProcessW's existing command-line boundary, in UTF-16 code units.
        return 32_767
    limit = os.sysconf("SC_ARG_MAX")
    env_bytes = sum(
        len(os.fsencode(k)) + len(os.fsencode(v)) + 2 for k, v in os.environ.items()
    )
    pointers = (len(os.environ) + 1) * (8 if sys.maxsize > 2**32 else 4)
    return max(1, limit - env_bytes - pointers)


def _argv_cost(arguments: list[str]) -> int:
    if os.name == "nt":
        return len(subprocess.list2cmdline(arguments).encode("utf-16-le")) // 2 + 1
    pointer = 8 if sys.maxsize > 2**32 else 4
    return (
        sum(len(os.fsencode(argument)) + 1 + pointer for argument in arguments)
        + pointer
    )


def _batches(
    paths: Iterator[Path], *, maximum_files: int, base_arguments: list[str]
) -> Iterator[tuple[Path, list[Path], list[str]]]:
    argv_limit = _argv_budget()
    for parent, group in groupby(paths, key=lambda item: item.parent):
        batch: list[Path] = []
        globs: list[str] = []
        for path in group:
            literal = "/" + glob.escape(path.name, unix=True)
            added = ["-g", literal]
            if batch and (
                len(batch) >= maximum_files
                or _argv_cost(base_arguments + globs + added + ["--", "."]) > argv_limit
            ):
                yield parent, batch, globs
                batch, globs = [], []
            if _argv_cost(base_arguments + added + ["--", "."]) > argv_limit:
                raise ToolResultPageBoundary(
                    "One file selector exceeds the platform command-line boundary"
                )
            batch.append(path)
            globs.extend(added)
        if batch:
            yield parent, batch, globs


def _excerpt(data: Mapping[str, object]) -> dict[str, object]:
    from pulsara_agent.tools.builtins.filesystem import _rg_json_bytes

    # The default rg line matcher can match a literal CR before LF. Do not
    # rstrip CR/LF indiscriminately and accidentally remove the real match.
    raw = _rg_json_bytes(data["lines"]).removesuffix(b"\n")
    submatches = data.get("submatches")
    if not isinstance(submatches, list) or not submatches:
        raise ValueError("ripgrep match is missing its submatch positions")
    byte_start = submatches[0]["start"]
    if type(byte_start) is not int or not 0 <= byte_start <= len(raw):
        raise ValueError("Invalid ripgrep submatch position")
    text = raw.decode("utf-8", "replace")
    match_start = len(raw[:byte_start].decode("utf-8", "replace"))
    start = max(0, match_start - SEARCH_EXCERPT_CHARS // 4)
    end = min(len(text), start + SEARCH_EXCERPT_CHARS)
    return {
        "excerpt": text[start:end],
        "excerpt_start": start,
        "excerpt_end": end,
        "line_chars": len(text),
        "match_char_offset": match_start,
        "excerpt_partial": start > 0 or end < len(text),
    }


def _decode_event(raw: bytes) -> tuple[str, Mapping[str, object]]:
    """Validate the closed rg JSON event shapes used by this adapter.

    JSON decoding remains the standard library's job. Ripgrep owns statistics
    and matching; this only validates the fields required to locate records and
    distinguish known control events from a malformed producer stream.
    """
    from pulsara_agent.tools.builtins.filesystem import _rg_json_bytes

    event = json.loads(raw)
    if not isinstance(event, dict) or set(event) != {"type", "data"}:
        raise ValueError("Malformed ripgrep JSON event")
    kind, data = event["type"], event["data"]
    if not isinstance(kind, str) or kind not in {
        "begin",
        "match",
        "context",
        "end",
        "summary",
    }:
        raise ValueError("Unknown ripgrep JSON event type")
    if not isinstance(data, dict):
        raise ValueError("Malformed ripgrep event data")
    if kind == "summary":
        if not isinstance(data.get("stats"), dict) or not isinstance(
            data.get("elapsed_total"), dict
        ):
            raise ValueError("Malformed ripgrep summary")
        return kind, data
    if "path" not in data or not _rg_json_bytes(data["path"]):
        raise ValueError("Malformed ripgrep event path")
    if kind == "end":
        binary_offset = data.get("binary_offset")
        if (
            "binary_offset" not in data
            or not isinstance(data.get("stats"), dict)
            or (
                binary_offset is not None
                and (type(binary_offset) is not int or binary_offset < 0)
            )
        ):
            raise ValueError("Malformed ripgrep end event")
    if kind in {"match", "context"}:
        if "lines" not in data:
            raise ValueError("Malformed ripgrep matching-line data")
        lines = _rg_json_bytes(data["lines"])
        if (
            type(data.get("line_number")) is not int
            or data["line_number"] < 1
            or type(data.get("absolute_offset")) is not int
            or data["absolute_offset"] < 0
            or not isinstance(data.get("submatches"), list)
        ):
            raise ValueError("Malformed ripgrep match position")
        if kind == "match" and not data["submatches"]:
            raise ValueError("A ripgrep match is missing its submatch")
        for match in data["submatches"]:
            if (
                not isinstance(match, dict)
                or set(match) != {"match", "start", "end"}
                or type(match["start"]) is not int
                or type(match["end"]) is not int
                or not 0 <= match["start"] <= match["end"] <= len(lines)
            ):
                raise ValueError("Malformed ripgrep submatch")
            _rg_json_bytes(match["match"])
    return kind, data


def execute_search(tool: WorkspaceTool, call: ToolCall) -> ToolExecutionResult:
    from pulsara_agent.tools.builtins.filesystem import (
        _application_error_result,
        _FileApplicationError,
        _is_broad_search_root,
        _path_access_scope,
        _relpath,
        _search_integer,
        _search_string,
        _rg_json_bytes,
        _complete_page_result,
        _page_budget,
        file_target,
        path_target_arg,
    )

    content_search = tool.name == "search_content"
    expression_name = "pattern" if content_search else "glob"
    allowed = {expression_name, "target", "limit", "continuation"}
    if content_search:
        allowed |= {"file_glob", "output_mode"}
    if set(call.arguments) - allowed:
        raise ValueError("Unknown search arguments")
    expression = _search_string(call.arguments, expression_name)
    mode = (
        _search_string(call.arguments, "output_mode", "content")
        if content_search
        else "files_only"
    )
    if mode not in {"content", "files_only", "count"}:
        raise ValueError("Unsupported output_mode")
    cursor = _cursor(
        call.arguments.get("continuation"), content=content_search and mode == "content"
    )
    if "continuation" in call.arguments and cursor is None:
        raise ValueError("continuation cannot be null")
    limit = _search_integer(
        call.arguments, "limit", DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT
    )
    root = tool._resolve_read_path(path_target_arg(call.arguments, default="."))
    home = tool._resolved_user_home()
    for condition, code, message in (
        (not root.exists(), "FILE_NOT_FOUND", "The search root does not exist."),
        (
            _is_broad_search_root(root, tool.workspace_root, home),
            "SEARCH_ROOT_TOO_BROAD",
            "Choose a specific search root.",
        ),
    ):
        if condition:
            return _application_error_result(
                tool,
                call,
                root,
                _FileApplicationError(
                    code,
                    message,
                    hint="Choose a specific existing file or narrower directory.",
                ),
            )
    base = root.parent if root.is_file() else root
    pattern = call.arguments.get("file_glob") if content_search else expression
    if pattern is not None:
        pattern = _search_string({"glob": pattern}, "glob")
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
    diagnostics = _Diagnostics(cursor.partial if cursor else False)
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    binary = str(private_ripgrep())
    common = [binary, "--no-config", "--color", "never", "--sort", "path"]
    if content_search:
        # Validate even when the inventory/glob contains no selected file. This
        # is ripgrep's own regex parser, not a Python approximation.
        validation = _Producer(
            common + ["-e", expression, "--", "-"],
            cwd=tool.workspace_root,
            call=call,
            diagnostics=diagnostics,
            deadline=deadline,
        )
        with validation:
            for _ in validation.chunks():
                pass
        if validation.process.returncode not in (0, 1):
            raise RuntimeError(diagnostics.snapshot()[1] or "Invalid search expression")
    entries: list[tuple[dict[str, object], _Cursor]] = []
    exhausted = False

    scope = _path_access_scope(root, tool.workspace_root, home)
    key = (
        "matches"
        if mode == "content" and content_search
        else "counts"
        if mode == "count"
        else "targets"
    )
    query = dict(call.arguments)
    query.pop("continuation", None)
    query["target"] = file_target(str(root))
    query["limit"] = limit
    if content_search:
        query["output_mode"] = mode
    budget = _page_budget(call)

    def payload(count: int, diagnostic_chars: int) -> dict[str, object]:
        partial, diagnostic, diagnostic_truncated = diagnostics.snapshot()
        finished = exhausted and count == len(entries)
        next_call = None
        if not finished and count:
            next_call = {
                "tool": tool.name,
                "arguments": {
                    **query,
                    "continuation": entries[count - 1][1].value(partial),
                },
            }
        value: dict[str, object] = {
            "status": "partial" if partial else "ok",
            "target": file_target(str(root)),
            "access_scope": scope,
            "workspace_relative": scope == "workspace",
            key: [item for item, _ in entries[:count]],
            "next": next_call,
        }
        if content_search:
            value["output_mode"] = mode
        if diagnostic:
            value["diagnostics"] = diagnostic[:diagnostic_chars]
            value["diagnostics_truncated"] = (
                diagnostic_truncated or diagnostic_chars < len(diagnostic)
            )
        return value

    def fitted_payload(count: int) -> dict[str, object] | None:
        diagnostic_chars = len(diagnostics.snapshot()[1])
        while True:
            value = payload(count, diagnostic_chars)
            if budget.fits(json_text(value)):
                return value
            if diagnostic_chars == 0:
                return None
            diagnostic_chars //= 2

    def selected_paths() -> Iterator[Path]:
        for raw in inventory.records(b"\0"):
            path = Path(os.fsdecode(raw))
            relative = path.relative_to(base).as_posix()
            if matcher is not None and not matcher.match(relative):
                continue
            if cursor is not None:
                key, after = _path_key(relative), _path_key(cursor.path)
                if key < after or (key == after and cursor.line is None):
                    continue
            yield path

    def append(record: dict[str, object], after: _Cursor) -> bool:
        entries.append((record, after))
        if fitted_payload(len(entries)) is None:
            entries.pop()
            if not entries:
                raise ToolResultPageBoundary(
                    "One search record and its continuation do not fit this call's result budget"
                )
            return True
        return len(entries) >= limit

    with _Producer(
        common + ["--files", "--null", "--", str(root)],
        cwd=tool.workspace_root,
        call=call,
        diagnostics=diagnostics,
        deadline=deadline,
    ) as inventory:
        paths = selected_paths()
        if not content_search:
            for path in paths:
                relative = path.relative_to(base).as_posix()
                if append(
                    file_target(_relpath(path, tool.workspace_root)),
                    _Cursor(relative, None, False),
                ):
                    break
            else:
                exhausted = True
        else:
            output_arguments = (
                ["--json"]
                if mode == "content"
                else [
                    "--with-filename",
                    "--null",
                    "-l" if mode == "files_only" else "-c",
                ]
            )
            batch_arguments = common + [
                "--max-depth",
                "1",
                *output_arguments,
                "-e",
                expression,
            ]
            done = False
            for parent, batch, literal_globs in _batches(
                paths, maximum_files=limit, base_arguments=batch_arguments
            ):
                # An explicit user FILE target retains its explicit-file
                # semantics. Directory inventory files stay directory searches.
                command = (
                    common + output_arguments + ["-e", expression, "--", str(root)]
                    if root.is_file()
                    else batch_arguments + literal_globs + ["--", "."]
                )
                with _Producer(
                    command,
                    cwd=parent,
                    call=call,
                    diagnostics=diagnostics,
                    deadline=deadline,
                ) as producer:
                    if mode == "content":
                        for raw in producer.records(b"\n"):
                            kind, data = _decode_event(raw)
                            if kind != "match":
                                continue
                            event_path = Path(os.fsdecode(_rg_json_bytes(data["path"])))
                            path = (
                                event_path
                                if event_path.is_absolute()
                                else parent / event_path
                            )
                            relative = path.relative_to(base).as_posix()
                            line = data["line_number"]
                            if type(line) is not int or line < 1:
                                raise ValueError("Invalid ripgrep line position")
                            if (
                                cursor
                                and relative == cursor.path
                                and cursor.line is not None
                                and line <= cursor.line
                            ):
                                continue
                            record = {
                                "target": file_target(
                                    _relpath(path, tool.workspace_root)
                                ),
                                "line": line,
                                **_excerpt(data),
                            }
                            if append(record, _Cursor(relative, line, False)):
                                done = True
                                break
                    elif mode == "files_only":
                        for raw in producer.records(b"\0"):
                            event_path = Path(os.fsdecode(raw))
                            path = (
                                event_path
                                if event_path.is_absolute()
                                else parent / event_path
                            )
                            if append(
                                file_target(_relpath(path, tool.workspace_root)),
                                _Cursor(path.relative_to(base).as_posix(), None, False),
                            ):
                                done = True
                                break
                    else:
                        # Filename is NUL-terminated, count is newline-terminated;
                        # filenames themselves may contain colons and newlines.
                        pending = bytearray()
                        filename: bytes | None = None
                        for chunk in producer.chunks():
                            pending.extend(chunk)
                            while True:
                                separator = b"\0" if filename is None else b"\n"
                                end = pending.find(separator)
                                if end < 0:
                                    break
                                part = bytes(pending[:end])
                                del pending[: end + 1]
                                if filename is None:
                                    filename = part
                                    continue
                                event_path = Path(os.fsdecode(filename))
                                filename = None
                                path = (
                                    event_path
                                    if event_path.is_absolute()
                                    else parent / event_path
                                )
                                record = {
                                    "target": file_target(
                                        _relpath(path, tool.workspace_root)
                                    ),
                                    "count": int(part),
                                }
                                if append(
                                    record,
                                    _Cursor(
                                        path.relative_to(base).as_posix(), None, False
                                    ),
                                ):
                                    done = True
                                    break
                            if done:
                                break
                        if not done and (pending or filename is not None):
                            raise ValueError("Malformed ripgrep count output")
                if done:
                    break
            else:
                exhausted = True

    # Joining can reveal late diagnostics. Recheck the exact frozen metadata,
    # keeping an atomic prefix if its final quote changed after the last record.
    value = fitted_payload(len(entries))
    if value is None:
        low, high, winner = 0, len(entries), None
        while low <= high:
            middle = (low + high) // 2
            candidate = fitted_payload(middle)
            if candidate is not None:
                winner = candidate
                low = middle + 1
            else:
                high = middle - 1
        value = winner
    if value is None or (entries and not value[key]):
        raise ToolResultPageBoundary(
            "One search record and its continuation do not fit this call's result budget"
        )
    return _complete_page_result(tool, call, value, metadata={"path": str(root)})
