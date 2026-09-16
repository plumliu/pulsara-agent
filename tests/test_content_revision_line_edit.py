from __future__ import annotations

import errno
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Barrier, Thread
from io import BytesIO

from jsonschema import Draft202012Validator
from PIL import Image
import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    UserHomeResolution,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins import filesystem
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    SearchFilesTool,
    LocalImageReadCandidate,
    ViewImageTool,
    WriteFileTool,
)


def _call(tool, arguments: dict[str, object], *, call_id: str = "call:1"):
    return tool.execute(ToolCall(call_id, tool.name, arguments))


def _payload(result: ToolExecutionResult) -> dict[str, object]:
    value = json.loads(result.output)
    assert isinstance(value, dict)
    return value


def _read(
    root: Path,
    path: str,
    *,
    offset: int = 1,
    limit: int = 2_000,
) -> tuple[str, dict[str, object]]:
    result = _call(
        ReadFileTool(root),
        {"path": path, "offset": offset, "limit": limit},
        call_id=f"read:{offset}:{limit}",
    )
    assert result.status is ToolResultState.SUCCESS
    payload = _payload(result)
    revision = payload["content_revision"]
    assert isinstance(revision, str)
    return revision, payload


def _edit(
    root: Path,
    path: str,
    revision: str,
    operations: list[dict[str, object]],
) -> ToolExecutionResult:
    return _call(
        EditFileTool(root),
        {"path": path, "base_revision": revision, "operations": operations},
        call_id="edit:1",
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"alpha",
        b"alpha\n",
        b"alpha\r\n",
        b"\xef\xbb\xbfalpha\n",
        b"alpha \n",
    ],
)
def test_read_file_revision_covers_exact_raw_bytes(tmp_path: Path, raw: bytes) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(raw)

    revision, payload = _read(tmp_path, "sample.txt")

    assert revision == f"sha256:{sha256(raw).hexdigest()}"
    assert payload["file_size"] == len(raw)


def test_exact_byte_differences_produce_distinct_revisions(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    values = (
        b"alpha",
        b"alpha\n",
        b"alpha\r\n",
        b"alpha \n",
        b"\xef\xbb\xbfalpha\n",
    )
    revisions = set()
    for raw in values:
        target.write_bytes(raw)
        revision, _ = _read(tmp_path, "sample.txt")
        revisions.add(revision)
    assert len(revisions) == len(values)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"\xff\xfe", "UNSUPPORTED_TEXT_ENCODING"),
        (b"alpha\x00beta", "UNSUPPORTED_BINARY_FILE"),
    ],
)
def test_read_file_rejects_lossy_or_binary_text(
    tmp_path: Path, raw: bytes, error: str
) -> None:
    (tmp_path / "sample.txt").write_bytes(raw)
    result = _call(ReadFileTool(tmp_path), {"path": "sample.txt"})
    assert result.status is ToolResultState.ERROR
    assert _payload(result)["error"] == error


def test_read_output_too_large_does_not_install_observation(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    raw = ("x" * (filesystem.MAX_READ_CHARS + 1)).encode()
    target.write_bytes(raw)
    read_result = _call(ReadFileTool(tmp_path), {"path": "sample.txt"})
    assert _payload(read_result)["error"] == "READ_OUTPUT_TOO_LARGE"

    edit_result = _edit(
        tmp_path,
        "sample.txt",
        f"sha256:{sha256(raw).hexdigest()}",
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["y"]}],
    )
    assert _payload(edit_result)["error"] == "READ_OBSERVATION_REQUIRED"
    assert target.read_bytes() == raw


def test_head_tail_projected_read_does_not_authorize_partial_lines(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text(
        "\n".join(f"{index:04d}-" + "x" * 80 for index in range(600)), encoding="utf-8"
    )
    result = _call(ReadFileTool(tmp_path), {"path": "sample.txt"})
    payload = _payload(result)
    assert result.status is ToolResultState.SUCCESS
    assert len(result.output.encode("utf-8")) > 40_000
    assert "none of its lines authorize" in payload["_warning"]

    edit_result = _edit(
        tmp_path,
        "sample.txt",
        payload["content_revision"],
        [
            {
                "kind": "replace_lines",
                "start_line": 1,
                "end_line": 1,
                "lines": ["changed"],
            }
        ],
    )
    assert _payload(edit_result)["error"] == "UNSEEN_LINE_RANGE"


def test_search_result_does_not_authorize_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    raw = b"one\ntwo\n"
    target.write_bytes(raw)
    search = _call(
        SearchFilesTool(tmp_path),
        {"path": "sample.txt", "pattern": "one", "target": "content"},
    )
    assert search.status is ToolResultState.SUCCESS

    edit_result = _edit(
        tmp_path,
        "sample.txt",
        f"sha256:{sha256(raw).hexdigest()}",
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["ONE"]}],
    )
    assert _payload(edit_result)["error"] == "READ_OBSERVATION_REQUIRED"
    assert target.read_bytes() == raw


@pytest.mark.parametrize(
    ("path", "error", "hint"),
    (("missing", "FILE_NOT_FOUND", "existing"),
     ("/", "SEARCH_ROOT_TOO_BROAD", "specific")),
)
def test_search_rejection_explains_how_to_select_a_path(
    tmp_path: Path, path: str, error: str, hint: str,
) -> None:
    result = _call(SearchFilesTool(tmp_path), {"path": path, "pattern": "needle"})
    assert result.status is ToolResultState.ERROR
    payload = _payload(result)
    assert payload["error"] == error
    assert hint in payload["_hint"]
    assert "Error" not in payload["message"]


def test_paginated_reads_union_seen_intervals_only_for_same_revision(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt", offset=1, limit=2)
    same_revision, _ = _read(tmp_path, "sample.txt", offset=4, limit=2)
    assert same_revision == revision

    rejected = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 2, "end_line": 4, "lines": ["x"]}],
    )
    assert _payload(rejected)["error"] == "UNSEEN_LINE_RANGE"
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\nfive\n"

    _read(tmp_path, "sample.txt", offset=3, limit=1)
    accepted = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 2, "end_line": 4, "lines": ["x"]}],
    )
    assert accepted.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == "one\nx\nfive\n"


def test_new_read_revision_replaces_old_observation(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    old_revision, _ = _read(tmp_path, "sample.txt")
    target.write_text("one\nchanged\n", encoding="utf-8")
    new_revision, _ = _read(tmp_path, "sample.txt", offset=1, limit=1)
    assert new_revision != old_revision

    rejected = _edit(
        tmp_path,
        "sample.txt",
        new_revision,
        [{"kind": "replace_lines", "start_line": 2, "end_line": 2, "lines": ["x"]}],
    )
    assert _payload(rejected)["error"] == "UNSEEN_LINE_RANGE"


def test_edit_requires_process_local_read_observation(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    raw = b"one\ntwo\n"
    target.write_bytes(raw)
    revision = f"sha256:{sha256(raw).hexdigest()}"

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["x"]}],
    )

    payload = _payload(result)
    assert result.status is ToolResultState.ERROR
    assert payload["error"] == "READ_OBSERVATION_REQUIRED"
    assert target.read_bytes() == raw


def test_revision_observation_is_joined_to_canonical_path(tmp_path: Path) -> None:
    raw = b"same\n"
    (tmp_path / "seen.txt").write_bytes(raw)
    target = tmp_path / "unseen.txt"
    target.write_bytes(raw)
    revision, _ = _read(tmp_path, "seen.txt")

    result = _edit(
        tmp_path,
        "unseen.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["new"]}],
    )

    assert _payload(result)["error"] == "READ_OBSERVATION_REQUIRED"
    assert target.read_bytes() == raw


@pytest.mark.parametrize(
    "revision",
    ["", "0" * 64, "sha256:abc", "sha256:" + "A" * 64, "sha512:" + "0" * 64],
)
def test_base_revision_format_is_strict(tmp_path: Path, revision: str) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    _read(tmp_path, "sample.txt")
    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["two"]}],
    )
    assert _payload(result)["error"] == "INVALID_CONTENT_REVISION"
    assert target.read_bytes() == b"one\n"


@pytest.mark.parametrize(
    "changed",
    [b"one \ntwo\n", b"one\r\ntwo\r\n", b"one\ntwo"],
)
def test_stale_revision_rejects_every_exact_byte_change(
    tmp_path: Path, changed: bytes
) -> None:
    target = tmp_path / "sample.txt"
    original = b"one\ntwo\n"
    target.write_bytes(original)
    revision, _ = _read(tmp_path, "sample.txt")
    target.write_bytes(changed)

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["x"]}],
    )

    payload = _payload(result)
    assert payload["error"] == "CONTENT_REVISION_MISMATCH"
    assert payload["current_revision"] == f"sha256:{sha256(changed).hexdigest()}"
    assert target.read_bytes() == changed


def test_content_restored_to_exact_bytes_can_use_same_revision(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    original = b"one\ntwo\n"
    target.write_bytes(original)
    revision, _ = _read(tmp_path, "sample.txt")
    target.write_bytes(b"temporary\n")
    target.write_bytes(original)

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 2, "end_line": 2, "lines": ["TWO"]}],
    )
    assert result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b"one\nTWO\n"


def test_operations_share_original_line_coordinate_system(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [
            {
                "kind": "replace_lines",
                "start_line": 2,
                "end_line": 3,
                "lines": ["two-a", "two-b", "two-c"],
            },
            {"kind": "insert_after", "line": 4, "lines": ["after-four"]},
            {"kind": "insert_before", "line": 1, "lines": ["head"]},
        ],
    )

    assert result.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == (
        "head\n1\ntwo-a\ntwo-b\ntwo-c\n4\nafter-four\n5\n"
    )
    payload = _payload(result)
    assert payload["operations_applied"] == 3
    assert payload["content_revision"] == (
        f"sha256:{sha256(target.read_bytes()).hexdigest()}"
    )
    assert "changed_windows" in payload
    assert "diff" in payload


def test_line_operations_cover_boundaries_blank_lines_and_unicode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("first\nmiddle\nlast", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [
            {"kind": "insert_before", "line": 1, "lines": ["开头", ""]},
            {
                "kind": "replace_lines",
                "start_line": 2,
                "end_line": 2,
                "lines": ["中央"],
            },
            {"kind": "insert_after", "line": 3, "lines": ["", "结尾"]},
        ],
    )

    assert result.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == "开头\n\nfirst\n中央\nlast\n\n结尾"


def test_delete_all_lines_produces_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"one\ntwo\n")
    revision, _ = _read(tmp_path, "sample.txt")
    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "delete_lines", "start_line": 1, "end_line": 2}],
    )
    assert result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b""
    assert _payload(result)["changed_windows"] == []


def test_line_edit_preserves_crlf_bom_final_newline_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    target.chmod(0o640)
    revision, _ = _read(tmp_path, "sample.txt")

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [
            {
                "kind": "replace_lines",
                "start_line": 2,
                "end_line": 2,
                "lines": ["TWO", "three"],
            }
        ],
    )

    assert result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b"\xef\xbb\xbfone\r\nTWO\r\nthree\r\n"
    assert target.stat().st_mode & 0o777 == 0o640


def test_line_edit_preserves_missing_final_newline(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"one\ntwo")
    revision, _ = _read(tmp_path, "sample.txt")
    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "insert_after", "line": 2, "lines": ["three"]}],
    )
    assert result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b"one\ntwo\nthree"


def test_mixed_line_endings_require_replace_file(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"one\r\ntwo\n")
    revision, _ = _read(tmp_path, "sample.txt")

    line_result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["ONE"]}],
    )
    assert _payload(line_result)["error"] == "UNSUPPORTED_MIXED_LINE_ENDINGS"
    assert target.read_bytes() == b"one\r\ntwo\n"

    replace_result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_file", "content": "ONE\r\ntwo\r\n"}],
    )
    assert replace_result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b"ONE\r\ntwo\r\n"


def test_replace_file_needs_revision_but_not_full_seen_range(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt", offset=2, limit=1)

    result = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [{"kind": "replace_file", "content": "replacement\n"}],
    )

    assert result.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_replace_file_can_fill_and_clear_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"")
    empty_revision, _ = _read(tmp_path, "sample.txt")
    filled = _edit(
        tmp_path,
        "sample.txt",
        empty_revision,
        [{"kind": "replace_file", "content": "hello\n"}],
    )
    assert filled.status is ToolResultState.SUCCESS

    filled_revision = _payload(filled)["content_revision"]
    assert isinstance(filled_revision, str)
    cleared = _edit(
        tmp_path,
        "sample.txt",
        filled_revision,
        [{"kind": "replace_file", "content": ""}],
    )
    assert cleared.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b""


@pytest.mark.parametrize(
    ("operations", "error"),
    [
        (
            [{"kind": "replace_lines", "start_line": 2, "end_line": 1, "lines": ["x"]}],
            "INVALID_LINE_RANGE",
        ),
        (
            [
                {
                    "kind": "replace_lines",
                    "start_line": 1,
                    "end_line": 1,
                    "lines": ["one"],
                }
            ],
            "NO_OP",
        ),
        (
            [
                {"kind": "delete_lines", "start_line": 1, "end_line": 2},
                {
                    "kind": "replace_lines",
                    "start_line": 2,
                    "end_line": 3,
                    "lines": ["x"],
                },
            ],
            "OVERLAPPING_OPERATIONS",
        ),
        (
            [
                {"kind": "insert_after", "line": 1, "lines": ["x"]},
                {"kind": "insert_before", "line": 2, "lines": ["y"]},
            ],
            "OVERLAPPING_OPERATIONS",
        ),
        (
            [
                {"kind": "replace_file", "content": "x"},
                {"kind": "insert_after", "line": 1, "lines": ["y"]},
            ],
            "INVALID_OPERATION_COMBINATION",
        ),
    ],
)
def test_invalid_operations_fail_before_writing(
    tmp_path: Path, operations: list[dict[str, object]], error: str
) -> None:
    target = tmp_path / "sample.txt"
    original = b"one\ntwo\nthree\n"
    target.write_bytes(original)
    revision, _ = _read(tmp_path, "sample.txt")

    result = _edit(tmp_path, "sample.txt", revision, operations)

    assert result.status is ToolResultState.ERROR
    assert _payload(result)["error"] == error
    assert target.read_bytes() == original


def test_old_edit_arguments_are_rejected_without_fallback(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    result = _call(
        EditFileTool(tmp_path),
        {"path": "sample.txt", "old_text": "one", "new_text": "two"},
    )
    assert _payload(result)["error"] == "INVALID_OPERATION_COMBINATION"
    assert target.read_text(encoding="utf-8") == "one\n"


def test_success_changed_window_authorizes_followup_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    first_revision, _ = _read(tmp_path, "sample.txt", offset=2, limit=1)
    first = _edit(
        tmp_path,
        "sample.txt",
        first_revision,
        [{"kind": "replace_lines", "start_line": 2, "end_line": 2, "lines": ["TWO"]}],
    )
    next_revision = _payload(first)["content_revision"]
    assert isinstance(next_revision, str)

    second = _edit(
        tmp_path,
        "sample.txt",
        next_revision,
        [{"kind": "replace_lines", "start_line": 3, "end_line": 3, "lines": ["THREE"]}],
    )
    assert second.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour\n"


def test_oversized_changed_line_is_not_marked_seen(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")
    first = _edit(
        tmp_path,
        "sample.txt",
        revision,
        [
            {
                "kind": "replace_lines",
                "start_line": 1,
                "end_line": 1,
                "lines": ["x" * filesystem.MAX_CHANGED_WINDOWS_JSON_CHARS],
            }
        ],
    )
    payload = _payload(first)
    assert payload["changed_windows"] == []
    assert payload["changed_windows_truncated"] is True

    second = _edit(
        tmp_path,
        "sample.txt",
        payload["content_revision"],
        [{"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["y"]}],
    )
    assert _payload(second)["error"] == "UNSEEN_LINE_RANGE"


def test_write_file_is_create_only_and_returns_revision(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    created = _call(tool, {"path": "nested/new.txt", "content": "hello\n"})
    payload = _payload(created)
    target = tmp_path / "nested/new.txt"
    assert created.status is ToolResultState.SUCCESS
    assert target.read_bytes() == b"hello\n"
    assert payload["content_revision"] == (
        f"sha256:{sha256(target.read_bytes()).hexdigest()}"
    )

    rejected = _call(tool, {"path": "nested/new.txt", "content": "overwrite"})
    assert rejected.status is ToolResultState.ERROR
    assert _payload(rejected)["error"] == "FILE_ALREADY_EXISTS"
    assert target.read_bytes() == b"hello\n"


@pytest.mark.parametrize("content", ["", "你好\n"])
def test_write_file_creates_empty_or_unicode_content_with_missing_parents(
    tmp_path: Path, content: str
) -> None:
    result = _call(
        WriteFileTool(tmp_path),
        {"path": "one/two/new.txt", "content": content},
    )
    target = tmp_path / "one/two/new.txt"
    assert result.status is ToolResultState.SUCCESS
    assert target.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("kind", ["directory", "symlink", "dangling_symlink"])
def test_write_file_rejects_every_existing_directory_entry(
    tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "occupied"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        (tmp_path / "backing.txt").write_text("original", encoding="utf-8")
        target.symlink_to("backing.txt")
    else:
        target.symlink_to("missing.txt")

    result = _call(WriteFileTool(tmp_path), {"path": "occupied", "content": "new"})

    assert result.status is ToolResultState.ERROR
    assert _payload(result)["error"] == "FILE_ALREADY_EXISTS"
    if kind == "symlink":
        assert (tmp_path / "backing.txt").read_text(encoding="utf-8") == "original"
    if kind == "dangling_symlink":
        assert not (tmp_path / "missing.txt").exists()


def test_write_file_concurrent_create_never_clobbers(tmp_path: Path) -> None:
    barrier = Barrier(2)
    results: list[ToolExecutionResult] = []

    def invoke(content: str) -> None:
        barrier.wait()
        results.append(
            _call(WriteFileTool(tmp_path), {"path": "race.txt", "content": content})
        )

    threads = [Thread(target=invoke, args=(value,)) for value in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result.status for result in results) == [
        ToolResultState.ERROR,
        ToolResultState.SUCCESS,
    ]
    assert (tmp_path / "race.txt").read_text(encoding="utf-8") in {"alpha", "beta"}


def test_write_file_reports_unsupported_atomic_no_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(_source: str, _target: Path) -> None:
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(os, "link", unsupported)
    result = _call(WriteFileTool(tmp_path), {"path": "new.txt", "content": "x"})
    assert result.status is ToolResultState.ERROR
    assert _payload(result)["error"] == "ATOMIC_NO_CLOBBER_UNAVAILABLE"
    assert not (tmp_path / "new.txt").exists()
    assert list(tmp_path.glob(".new.txt.*.tmp")) == []


def test_atomic_replace_failure_preserves_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")

    def fail_replace(_source: str, _target: Path) -> None:
        raise OSError(errno.EIO, "publication failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        _edit(
            tmp_path,
            "sample.txt",
            revision,
            [
                {
                    "kind": "replace_lines",
                    "start_line": 1,
                    "end_line": 1,
                    "lines": ["two"],
                }
            ],
        )
    assert target.read_bytes() == b"one\n"
    assert list(tmp_path.glob(".sample.txt.*.tmp")) == []


def test_post_write_verification_failure_is_physical_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")
    real_atomic_replace = filesystem._atomic_replace_bytes

    def corrupt_after_publish(path: Path, content: bytes) -> None:
        real_atomic_replace(path, content)
        path.write_bytes(b"external\n")

    monkeypatch.setattr(filesystem, "_atomic_replace_bytes", corrupt_after_publish)
    with pytest.raises(RuntimeError, match="may already be modified"):
        _edit(
            tmp_path,
            "sample.txt",
            revision,
            [
                {
                    "kind": "replace_lines",
                    "start_line": 1,
                    "end_line": 1,
                    "lines": ["two"],
                }
            ],
        )
    assert target.read_bytes() == b"external\n"


def test_competing_edits_serialize_and_only_one_revision_wins(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    revision, _ = _read(tmp_path, "sample.txt")
    barrier = Barrier(2)
    results: list[ToolExecutionResult] = []

    def invoke(value: str) -> None:
        barrier.wait()
        results.append(
            _edit(
                tmp_path,
                "sample.txt",
                revision,
                [
                    {
                        "kind": "replace_lines",
                        "start_line": 1,
                        "end_line": 1,
                        "lines": [value],
                    }
                ],
            )
        )

    threads = [Thread(target=invoke, args=(value,)) for value in ("two", "three")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [result.status for result in results].count(ToolResultState.SUCCESS) == 1
    error = next(result for result in results if result.status is ToolResultState.ERROR)
    assert _payload(error)["error"] == "CONTENT_REVISION_MISMATCH"
    assert target.read_text(encoding="utf-8") in {"two\n", "three\n"}


@pytest.mark.parametrize(
    ("path", "content", "error"),
    [
        ("image.png", "text", "UNSUPPORTED_BINARY_FILE"),
        ("nul.txt", "a\x00b", "UNSUPPORTED_BINARY_FILE"),
    ],
)
def test_write_file_rejects_non_text_targets(
    tmp_path: Path, path: str, content: str, error: str
) -> None:
    result = _call(WriteFileTool(tmp_path), {"path": path, "content": content})
    assert _payload(result)["error"] == error
    assert not (tmp_path / path).exists()


def test_blocked_device_is_rejected_as_text() -> None:
    result = _call(ReadFileTool(Path.cwd()), {"path": "/dev/null"})
    assert result.status is ToolResultState.ERROR
    assert _payload(result)["error"] == "UNSUPPORTED_BINARY_FILE"


def test_catalog_schema_is_single_closed_line_edit_path() -> None:
    schema = builtin_tool_catalog_entry("edit_file").descriptor.input_schema
    assert schema["required"] == ("path", "base_revision", "operations")
    assert set(schema["properties"]) == {"path", "base_revision", "operations"}
    assert schema["additionalProperties"] is False

    validator = Draft202012Validator(schema)
    valid = {
        "path": "a.txt",
        "base_revision": "sha256:" + "0" * 64,
        "operations": [
            {"kind": "replace_lines", "start_line": 1, "end_line": 1, "lines": ["x"]}
        ],
    }
    assert list(validator.iter_errors(valid)) == []
    assert list(
        validator.iter_errors({"path": "a.txt", "old_text": "a", "new_text": "b"})
    )
    assert list(
        validator.iter_errors(
            {
                **valid,
                "operations": [
                    {
                        "kind": "delete_lines",
                        "start_line": 1,
                        "end_line": 1,
                        "lines": [],
                    }
                ],
            }
        )
    )


def test_file_tool_descriptions_explain_line_locators_without_changing_edit_semantics() -> None:
    read_description = builtin_tool_catalog_entry("read_file").descriptor.description
    edit_descriptor = builtin_tool_catalog_entry("edit_file").descriptor
    edit_description = edit_descriptor.description
    combined = f"{read_description}\n{edit_description}"

    assert "read_file displays: 2|预算=360" in combined
    assert '"kind":"replace_lines"' in combined
    assert '"lines":["预算=400"]' in combined
    assert "The leading 2| is a display locator, not replacement text." in combined
    assert "Do not copy the display prefix" in combined
    assert "1|2|预算=360" in combined
    assert "replace_file" in edit_description
    assert "does not require the full file" in edit_description

    variants = edit_descriptor.input_schema["properties"]["operations"]["items"][
        "oneOf"
    ]
    replace_lines = next(
        item
        for item in variants
        if item["properties"]["kind"].get("const") == "replace_lines"
    )
    logical_lines = replace_lines["properties"]["lines"]
    assert "display locator" in logical_lines["description"]
    assert "Do not copy" in logical_lines["items"]["description"]
    assert "1|2|预算=360" in logical_lines["description"]

    replace_file = next(
        item
        for item in variants
        if item["properties"]["kind"].get("const") == "replace_file"
    )
    replace_content = replace_file["properties"]["content"]["description"]
    assert "complete original target text" in replace_content
    assert "display locator" in replace_content
    assert "does not require the full file to have been displayed" in replace_content


def test_filesystem_state_has_no_timestamp_or_revision_registry() -> None:
    fields = filesystem._WorkspaceFileState.__dataclass_fields__
    assert "read_timestamps" not in fields
    assert set(fields) == {
        "lock",
        "path_locks",
        "observations",
        "last_lookup_key",
        "consecutive_lookup_count",
    }


def test_view_image_reads_a_regular_file_by_content_not_extension(
    tmp_path: Path,
) -> None:
    output = BytesIO()
    Image.new("RGB", (3, 2), (12, 34, 56)).save(output, "PNG")
    payload = output.getvalue()
    mismatched = tmp_path / "actual-png.jpg"
    mismatched.write_bytes(payload)
    tool = ViewImageTool(tmp_path)
    call = ToolCall("call:image", "view_image", {"path": mismatched.name})

    candidate = tool.read_bounded(call, maximum_bytes=len(payload))

    assert isinstance(candidate, LocalImageReadCandidate)
    assert candidate.payload == payload
    assert candidate.requested_path == mismatched.name


def test_view_image_applies_the_frozen_read_bound_before_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"larger-than-allowance")
    tool = ViewImageTool(tmp_path)
    call = ToolCall("call:image", "view_image", {"path": path.name})

    result = tool.read_bounded(call, maximum_bytes=3)

    assert isinstance(result, ToolExecutionResult)
    assert result.status is ToolResultState.ERROR
    assert json.loads(result.output)["error"] == "IMAGE_RESOURCE_EXCEEDED"


def test_view_image_reuses_the_frozen_read_path_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "user-home"
    pulsara_home = tmp_path / "pulsara-home"
    workspace.mkdir()
    user_home.mkdir()
    pulsara_home.mkdir()
    output = BytesIO()
    Image.new("RGB", (3, 2), (12, 34, 56)).save(output, "PNG")
    payload = output.getvalue()
    relative = workspace / "relative.png"
    absolute = tmp_path / "absolute.png"
    user = user_home / "user.png"
    pulsara = pulsara_home / "state.png"
    for path in (relative, absolute, user, pulsara):
        path.write_bytes(payload)
    tool = ViewImageTool(
        workspace,
        pulsara_home_resolution=PulsaraHomeResolution(
            PulsaraHomeDisposition.RESOLVED,
            path=pulsara_home.resolve(),
        ),
        user_home_resolution=UserHomeResolution(
            PulsaraHomeDisposition.RESOLVED,
            path=user_home.resolve(),
        ),
    )

    requested = (
        "relative.png",
        str(absolute),
        "~/user.png",
        "${PULSARA_HOME}/state.png",
    )
    for index, path in enumerate(requested):
        result = tool.read_bounded(
            ToolCall(f"call:{index}", "view_image", {"path": path}),
            maximum_bytes=len(payload),
        )
        assert isinstance(result, LocalImageReadCandidate)
        assert result.payload == payload

    with pytest.raises(ValueError, match="escapes workspace root"):
        tool.read_bounded(
            ToolCall("call:escape", "view_image", {"path": "../absolute.png"}),
            maximum_bytes=len(payload),
        )


def test_view_image_rejects_non_regular_files_without_blocking(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "image.fifo"
    os.mkfifo(fifo)
    tool = ViewImageTool(tmp_path)

    for index, path in enumerate((tmp_path, fifo, Path("/dev/null"))):
        if not path.exists():
            continue
        result = tool.read_bounded(
            ToolCall(f"call:{index}", "view_image", {"path": str(path)}),
            maximum_bytes=100,
        )
        assert isinstance(result, ToolExecutionResult)
        assert json.loads(result.output)["error"] == "IMAGE_PATH_NOT_REGULAR"


def test_view_image_detects_growth_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "growing.png"
    path.write_bytes(b"abc")
    tool = ViewImageTool(tmp_path)
    original_read = os.read
    first = True

    def growing_read(descriptor: int, maximum: int) -> bytes:
        nonlocal first
        chunk = original_read(descriptor, maximum)
        if first:
            first = False
            with path.open("ab") as stream:
                stream.write(b"d")
        return chunk

    monkeypatch.setattr(os, "read", growing_read)
    result = tool.read_bounded(
        ToolCall("call:growing", "view_image", {"path": path.name}),
        maximum_bytes=3,
    )

    assert isinstance(result, ToolExecutionResult)
    assert json.loads(result.output)["error"] == "IMAGE_RESOURCE_EXCEEDED"
