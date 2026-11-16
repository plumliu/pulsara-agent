"""Current UTF-8 reads, exact text edits and atomic file publication."""

import errno
import os
from io import BytesIO
from pathlib import Path
from threading import Barrier, Thread

from PIL import Image
import pytest

from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    UserHomeResolution,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
    ViewImageTool,
    LocalImageReadCandidate,
)


def _call(tool, arguments, *, call_id="file-test"):
    return tool.execute(ToolCall(call_id, tool.name, arguments))


def text(result):
    return "".join(
        part.value for part in result.business_value.content if part.type == "text"
    )


def path(name):
    return {"kind": "PATH", "path": name}


@pytest.mark.parametrize(
    "raw",
    [b"", b"alpha", b"alpha\n", b"alpha\r\n", b"\xef\xbb\xbfalpha\n", b"alpha \n"],
)
def test_read_preserves_source_text_and_file_facts(tmp_path, raw):
    (tmp_path / "sample.txt").write_bytes(raw)
    result = _call(ReadFileTool(tmp_path), {"target": path("sample.txt")})
    assert result.status is ToolResultState.SUCCESS
    assert text(result) == raw.decode("utf-8-sig")
    assert result.business_value.public_fields["meta"]["file_size"] == len(raw)
    assert result.business_value.public_fields["meta"][
        "had_utf8_bom"
    ] == raw.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    "raw,reason", [(b"\xff\xfe", "UTF-8"), (b"alpha\x00beta", "NUL")]
)
def test_invalid_file_text_is_not_lossily_replaced(tmp_path, raw, reason):
    (tmp_path / "sample.txt").write_bytes(raw)
    result = _call(ReadFileTool(tmp_path), {"target": path("sample.txt")})
    assert result.status is ToolResultState.ERROR
    assert reason in text(result)
    assert (tmp_path / "sample.txt").read_bytes() == raw


def test_long_lines_and_tail_selection_preserve_complete_body_order(tmp_path):
    raw = "x" * 100_001 + "\r\nsecond\nlast"
    (tmp_path / "sample.txt").write_bytes(raw.encode())
    tool = ReadFileTool(tmp_path)
    full = _call(tool, {"target": path("sample.txt")})
    tail = _call(tool, {"target": path("sample.txt"), "offset": -2})
    assert text(full) == raw and full.status is ToolResultState.SUCCESS
    assert text(tail) == "second\nlast"
    assert (
        _call(tool, {"target": path("sample.txt"), "offset": 99}).business_value.content
        == ()
    )


def test_read_path_can_point_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "other.txt").write_text("outside")
    result = _call(ReadFileTool(root), {"target": path("../other.txt")})
    assert result.status is ToolResultState.SUCCESS and text(result) == "outside"


def test_exact_edit_uses_current_body_without_a_prior_read(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\n")
    result = _call(
        EditFileTool(tmp_path),
        {"target": path("sample.txt"), "old_text": "two", "new_text": "第二行"},
    )
    assert result.status is ToolResultState.SUCCESS
    assert target.read_text() == "one\n第二行\n"
    assert "-two" in text(result) and "+第二行" in text(result)


@pytest.mark.parametrize("old,reason", [("missing", "not found"), ("one", "multiple")])
def test_missing_or_ambiguous_match_does_not_write(tmp_path, old, reason):
    target = tmp_path / "sample.txt"
    target.write_text("one one\n")
    result = _call(
        EditFileTool(tmp_path),
        {"target": path("sample.txt"), "old_text": old, "new_text": "changed"},
    )
    assert result.status is ToolResultState.ERROR and reason in text(result)
    assert target.read_text() == "one one\n"


@pytest.mark.parametrize("raw", [b"one\r\ntwo", b"\xef\xbb\xbfone\r\ntwo"])
def test_exact_edit_preserves_bom_newlines_and_mode(tmp_path, raw):
    target = tmp_path / "sample.txt"
    target.write_bytes(raw)
    target.chmod(0o640)
    result = _call(
        EditFileTool(tmp_path),
        {"target": path("sample.txt"), "old_text": "two", "new_text": "three"},
    )
    assert result.status is ToolResultState.SUCCESS
    assert target.read_bytes() == raw.replace(b"two", b"three")
    assert target.stat().st_mode & 0o777 == 0o640


def test_whole_replacement_can_fill_and_clear_empty_file(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("")
    tool = EditFileTool(tmp_path)
    assert (
        _call(
            tool, {"target": path("sample.txt"), "old_text": "", "new_text": "你好\n"}
        ).status
        is ToolResultState.SUCCESS
    )
    assert target.read_bytes() == "你好\n".encode()
    assert (
        _call(tool, {"target": path("sample.txt"), "content": ""}).status
        is ToolResultState.SUCCESS
    )
    assert target.read_bytes() == b""


def test_unchanged_edit_has_no_diff_and_does_not_publish(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    target.write_text("one\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged edit attempted publication")

    monkeypatch.setattr(os, "replace", forbidden)
    for args in ({"old_text": "one", "new_text": "one"}, {"content": "one\n"}):
        result = _call(EditFileTool(tmp_path), {"target": path("sample.txt"), **args})
        assert (
            result.status is ToolResultState.SUCCESS
            and result.business_value.content == ()
        )
        assert result.business_value.public_fields["target"] == path("sample.txt")


def test_atomic_edit_failure_preserves_original_and_cleans_temporary_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.txt"
    target.write_text("one\n")

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "publication failed")

    monkeypatch.setattr(os, "replace", fail)
    result = _call(
        EditFileTool(tmp_path),
        {"target": path("sample.txt"), "old_text": "one", "new_text": "two"},
    )
    assert result.status is ToolResultState.ERROR and "publication failed" in text(
        result
    )
    assert target.read_bytes() == b"one\n"
    assert list(tmp_path.glob(".sample.txt.*.tmp")) == []


def test_competing_exact_edits_serialize_against_current_body(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("one\n")
    barrier = Barrier(2)
    results = []

    def invoke(value):
        barrier.wait()
        results.append(
            _call(
                EditFileTool(tmp_path),
                {"target": path("sample.txt"), "old_text": "one", "new_text": value},
            )
        )

    threads = [Thread(target=invoke, args=(value,)) for value in ("two", "three")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r.status is ToolResultState.SUCCESS for r in results) == 1
    assert "not found" in text(
        next(r for r in results if r.status is ToolResultState.ERROR)
    )
    assert target.read_text() in {"two\n", "three\n"}


@pytest.mark.parametrize("name,body", [("image.png", "text"), ("nul.txt", "a\x00b")])
def test_content_creation_rejects_binary_targets_and_nul(tmp_path, name, body):
    result = _call(WriteFileTool(tmp_path), {"target": path(name), "content": body})
    assert result.status is ToolResultState.ERROR
    assert not (tmp_path / name).exists()


@pytest.mark.parametrize("content", ["", "你好\n"])
def test_write_file_creates_empty_or_unicode_content_with_missing_parents(
    tmp_path: Path, content: str
) -> None:
    result = _call(
        WriteFileTool(tmp_path),
        {"target": {"kind": "PATH", "path": "one/two/new.txt"}, "content": content},
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

    result = _call(
        WriteFileTool(tmp_path),
        {"target": {"kind": "PATH", "path": "occupied"}, "content": "new"},
    )

    assert result.status is ToolResultState.ERROR
    assert "cannot overwrite" in text(result)
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
            _call(
                WriteFileTool(tmp_path),
                {"target": {"kind": "PATH", "path": "race.txt"}, "content": content},
            )
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
    def unsupported(_source: str, _target: Path, **_binding) -> None:
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(os, "link", unsupported)
    result = _call(
        WriteFileTool(tmp_path),
        {"target": {"kind": "PATH", "path": "new.txt"}, "content": "x"},
    )
    assert result.status is ToolResultState.ERROR
    assert "does not support atomic no-clobber" in text(result)
    assert not (tmp_path / "new.txt").exists()
    assert list(tmp_path.glob(".new.txt.*.tmp")) == []


def test_view_image_reads_a_regular_file_by_content_not_extension(
    tmp_path: Path,
) -> None:
    output = BytesIO()
    Image.new("RGB", (3, 2), (12, 34, 56)).save(output, "PNG")
    payload = output.getvalue()
    mismatched = tmp_path / "actual-png.jpg"
    mismatched.write_bytes(payload)
    tool = ViewImageTool(tmp_path)
    call = ToolCall(
        "call:image",
        "view_image",
        {"target": {"kind": "PATH", "path": mismatched.name}},
    )

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
    call = ToolCall(
        "call:image", "view_image", {"target": {"kind": "PATH", "path": path.name}}
    )

    result = tool.read_bounded(call, maximum_bytes=3)

    assert isinstance(result, ToolExecutionResult)
    assert result.status is ToolResultState.ERROR
    assert "IMAGE_RESOURCE_EXCEEDED" in text(result)


def test_view_image_reuses_the_frozen_read_path_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "user-home"
    workspace.mkdir()
    user_home.mkdir()
    output = BytesIO()
    Image.new("RGB", (3, 2), (12, 34, 56)).save(output, "PNG")
    payload = output.getvalue()
    relative = workspace / "relative.png"
    absolute = tmp_path / "absolute.png"
    user = user_home / "user.png"
    for path in (relative, absolute, user):
        path.write_bytes(payload)
    tool = ViewImageTool(
        workspace,
        user_home_resolution=UserHomeResolution(
            PulsaraHomeDisposition.RESOLVED,
            path=user_home.resolve(),
        ),
    )

    requested = (
        "relative.png",
        "../absolute.png",
        str(absolute),
        "~/user.png",
    )
    for index, path in enumerate(requested):
        result = tool.read_bounded(
            ToolCall(
                f"call:{index}",
                "view_image",
                {"target": {"kind": "PATH", "path": path}},
            ),
            maximum_bytes=len(payload),
        )
        assert isinstance(result, LocalImageReadCandidate)
        assert result.payload == payload


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
            ToolCall(
                f"call:{index}",
                "view_image",
                {"target": {"kind": "PATH", "path": str(path)}},
            ),
            maximum_bytes=100,
        )
        assert isinstance(result, ToolExecutionResult)
        assert "IMAGE_PATH_NOT_REGULAR" in text(result)


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
        ToolCall(
            "call:growing",
            "view_image",
            {"target": {"kind": "PATH", "path": path.name}},
        ),
        maximum_bytes=3,
    )

    assert isinstance(result, ToolExecutionResult)
    assert "IMAGE_RESOURCE_EXCEEDED" in text(result)
