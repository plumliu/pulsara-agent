"""Complete call-local ripgrep enumeration and typed matching records.

Ripgrep owns traversal, ignore rules, encoding, matching and binary detection.
Pulsara only selects eligible paths and packs the observed records. No producer
or search state survives a call.
"""

from __future__ import annotations

from itertools import groupby
import json
import errno
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping

from wcmatch import glob

from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.ripgrep import private_ripgrep
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


# Preserve the existing search operation deadline, across all its subprocesses.
SEARCH_TIMEOUT_SECONDS = 60
_PIPE_CHUNK_BYTES = 64 * 1024


class _Diagnostics:
    def __init__(self, partial: bool) -> None:
        self.partial = partial
        self.data = bytearray()
        self.lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        if chunk:
            with self.lock:
                self.partial = True
                self.data.extend(chunk)

    def snapshot(self) -> tuple[bool, str]:
        with self.lock:
            return (
                self.partial,
                bytes(self.data).decode("utf-8", "replace"),
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
    paths: Iterator[Path], *, base_arguments: list[str]
) -> Iterator[tuple[Path, list[Path], list[str]]]:
    argv_limit = _argv_budget()
    for parent, group in groupby(paths, key=lambda item: item.parent):
        batch: list[Path] = []
        globs: list[str] = []
        for path in group:
            literal = "/" + glob.escape(path.name, unix=True)
            added = ["-g", literal]
            if batch and (
                _argv_cost(base_arguments + globs + added + ["--", "."]) > argv_limit
            ):
                yield parent, batch, globs
                batch, globs = [], []
            if _argv_cost(base_arguments + added + ["--", "."]) > argv_limit:
                raise OSError(
                    errno.E2BIG,
                    "One file selector exceeds the platform command-line boundary",
                )
            batch.append(path)
            globs.extend(added)
        if batch:
            yield parent, batch, globs


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
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.message import ToolResultState
    from pulsara_agent.tools.builtins.filesystem import (
        _is_broad_search_root,
        _relpath,
        _search_string,
        _rg_json_bytes,
        file_business_result,
        file_target,
        path_target_arg,
    )

    content_search = tool.name == "search_file_content"
    expression_name = "pattern" if content_search else "glob"
    allowed = {"target", expression_name} | (
        {"file_glob", "output_mode"} if content_search else set()
    )
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
    root = tool._resolve_read_path(path_target_arg(call.arguments, default="."))
    fields = {"target": file_target(_relpath(root, tool.workspace_root))}
    parts = []
    try:
        if not root.exists():
            raise ValueError("The search root does not exist")
        if _is_broad_search_root(root, tool.workspace_root, tool._resolved_user_home()):
            raise ValueError("Choose a specific search root")
        base = root.parent if root.is_file() else root
        pattern = call.arguments.get("file_glob") if content_search else expression
        if "file_glob" in call.arguments:
            pattern = _search_string(call.arguments, "file_glob")
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
        diagnostics = _Diagnostics(False)
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        common = [
            str(private_ripgrep()),
            "--no-config",
            "--color",
            "never",
            "--sort",
            "path",
        ]
        if content_search:
            with _Producer(
                common + ["-e", expression, "--", "-"],
                cwd=tool.workspace_root,
                call=call,
                diagnostics=diagnostics,
                deadline=deadline,
            ) as validation:
                for _ in validation.chunks():
                    pass
            if validation.process.returncode not in (0, 1):
                raise ValueError(
                    diagnostics.snapshot()[1] or "Invalid search expression"
                )

        def selected_paths(inventory):
            for raw in inventory.records(b"\0"):
                path = Path(os.fsdecode(raw))
                if matcher is None or matcher.match(path.relative_to(base).as_posix()):
                    yield path

        with _Producer(
            common + ["--files", "--null", "--", str(root)],
            cwd=tool.workspace_root,
            call=call,
            diagnostics=diagnostics,
            deadline=deadline,
        ) as inventory:
            paths = selected_paths(inventory)
            if not content_search:
                for path in paths:
                    parts.append(
                        ResultPart(
                            "target", file_target(_relpath(path, tool.workspace_root))
                        )
                    )
            else:
                arguments = common + ["--max-depth", "1", "--json", "-e", expression]
                for parent, batch, literal_globs in _batches(
                    paths, base_arguments=arguments
                ):
                    command = (
                        common + ["--json", "-e", expression, "--", str(root)]
                        if root.is_file()
                        else arguments + literal_globs + ["--", "."]
                    )
                    active_path = None
                    count = 0
                    matched = False
                    with _Producer(
                        command,
                        cwd=parent,
                        call=call,
                        diagnostics=diagnostics,
                        deadline=deadline,
                    ) as producer:
                        for raw in producer.records(b"\n"):
                            kind, data = _decode_event(raw)
                            if kind == "summary":
                                continue
                            path = Path(os.fsdecode(_rg_json_bytes(data["path"])))
                            if not path.is_absolute():
                                path = parent / path
                            target = file_target(_relpath(path, tool.workspace_root))
                            if kind == "begin":
                                active_path = target
                                count = 0
                                matched = False
                            elif kind == "match":
                                matched = True
                                count += len(data["submatches"])
                                if mode == "content":
                                    parts.append(
                                        ResultPart(
                                            "object",
                                            {
                                                "target": target,
                                                "line": data["line_number"],
                                                "text": _rg_json_bytes(
                                                    data["lines"]
                                                ).decode("utf-8"),
                                            },
                                        )
                                    )
                            elif kind == "end" and matched:
                                if mode == "files_only":
                                    parts.append(ResultPart("target", active_path))
                                elif mode == "count":
                                    parts.append(
                                        ResultPart(
                                            "object",
                                            {"target": active_path, "count": count},
                                        )
                                    )
        partial, diagnostic = diagnostics.snapshot()
        if partial:
            raise OSError(diagnostic or "Search could not complete its full scope")
        if not parts:
            parts.append(
                ResultPart(
                    "text",
                    "没有找到匹配的内容。"
                    if content_search
                    else "没有找到匹配的文件。",
                )
            )
        return file_business_result(tool, call, fields=fields, parts=parts)
    except (InterruptedError, TimeoutError, OSError, ValueError) as exc:
        parts.append(ResultPart("text", f"{root}: {exc}"))
        return file_business_result(
            tool,
            call,
            fields=fields,
            parts=parts,
            status=ToolResultState.INTERRUPTED
            if isinstance(exc, InterruptedError)
            else ToolResultState.ERROR,
        )
