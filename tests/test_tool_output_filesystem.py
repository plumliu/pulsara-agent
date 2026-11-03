from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
from dataclasses import replace
import errno
import json
from pathlib import Path

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.ports.tool_output import ToolOutputBodyView, ToolOutputContentError
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.tools.builtins import filesystem
from pulsara_agent.tools.builtins.filesystem import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
)
from pulsara_agent.tools.builtins.workspace import WritePathScope
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
)
from pulsara_agent.primitives.run_permission import (
    PermissionMode,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)


class BodyPort:
    def __init__(self, content=b"retained text", *, snapshot=False, session_id=None):
        self.reads = 0
        self.target = {
            "kind": "SESSION_ENTRY",
            "session_id": session_id or random_id("session"),
            "entry_id": random_id("entry"),
        }
        self.view = ToolOutputBodyView(
            self.target,
            content,
            "text/plain",
            "utf-8",
            ToolOutputSourceCoverage.RETAINED_SNAPSHOT
            if snapshot
            else ToolOutputSourceCoverage.COMPLETE,
            ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP if snapshot else None,
            (random_id("memory"),),
        )

    def read_body(self, target):
        self.reads += 1
        if target.get("entry_id") != self.target["entry_id"] or (
            "session_id" in target and target["session_id"] != self.target["session_id"]
        ):
            raise KeyError(target)
        return self.view


def call(path="output.txt", *, port=None, **extra):
    port = port or _current_port
    return ToolCall(
        random_id("call"),
        "write_file",
        {"target": {"kind": "PATH", "path": str(path)}, "source": port.target, **extra},
    )


_current_port = BodyPort()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbfA\r\n\x00end",
        '{"n":1e99,"n":2,"s":"中文🙂"}'.encode(),
        b"a" * 100_000,
    ],
)
@pytest.mark.parametrize("snapshot", [False, True])
def test_source_copy_preserves_exact_bytes_and_coverage_without_exposure(
    tmp_path, content, snapshot
):
    port = BodyPort(content, snapshot=snapshot)
    result = WriteFileTool(tmp_path, result_source_reader=port).execute(
        call("nested/result.bin", port=port)
    )
    payload = json.loads(result.output)
    assert result.status is ToolResultState.SUCCESS and result.page_complete
    assert (tmp_path / "nested/result.bin").read_bytes() == content
    assert (tmp_path / "nested/result.bin").stat().st_mode & 0o777 == 0o600
    assert payload["bytes_written"] == len(content)
    assert payload["source"] == port.target
    assert payload["source_coverage"] == port.view.source_coverage.value
    assert result.model_visible_memory_fact_ids == () and port.reads == 1


def test_saved_result_read_is_lossless_across_long_lines_and_retains_original_target(
    tmp_path,
):
    text = '头\\"' * 20_000 + "\r\nsecond\n\nlast"
    port = BodyPort(text.encode())
    arguments = {"target": port.target}
    pieces = []
    while True:
        request = ToolCall(random_id("call"), "read_file", arguments)
        result = ReadFileTool(tmp_path, result_source_reader=port).execute(request)
        value = json.loads(result.output)
        assert (
            result.page_complete
            and result.model_visible_memory_fact_ids
            == port.view.model_visible_memory_fact_ids
        )
        assert ToolResultPageBudget(tool_call_id=request.id).fits(
            result.output,
            model_visible_memory_fact_ids=result.model_visible_memory_fact_ids,
        )
        pieces.append(value["content"])
        if value["next"] is None:
            break
        assert value["next"]["arguments"]["target"] == port.target
        arguments = value["next"]["arguments"]
    assert "".join(pieces) == text
    assert not list(tmp_path.iterdir())


def test_unavailable_or_unauthorized_source_never_creates_destination(tmp_path):
    port = BodyPort()
    tool = WriteFileTool(tmp_path, result_source_reader=port)
    request = call(port=port)
    bad = dict(request.arguments["source"], entry_id=random_id("entry"))
    result = tool.execute(
        replace(request, arguments={**request.arguments, "source": bad})
    )
    assert json.loads(result.output)["status"] == "not_found"

    class Broken:
        def read_body(self, target):
            raise ToolOutputContentError("output_body_digest_failed")

    result = WriteFileTool(tmp_path, result_source_reader=Broken()).execute(request)
    assert json.loads(result.output)["status"] == "content_error"
    assert not list(tmp_path.iterdir())


def test_receipt_budget_is_checked_before_creating_parent_or_file(tmp_path):
    port = BodyPort()
    request = call("new/parent/result.txt", port=port)
    request = replace(
        request,
        page_budget=ToolResultPageBudget(
            tool_call_id=request.id,
            accepts_provider_body=lambda body, provenance: False,
        ),
    )
    result = WriteFileTool(tmp_path, result_source_reader=port).execute(request)
    assert json.loads(result.output)["status"] == "resource_boundary"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "x", "source": _current_port.target},
        {"source": {"kind": "PATH", "path": "x"}},
        {"content": "x", "overwrite": True},
        {},
    ],
)
def test_write_has_exact_content_source_xor(tmp_path, arguments):
    with pytest.raises(ValueError):
        WriteFileTool(tmp_path, result_source_reader=_current_port).execute(
            ToolCall(
                random_id("call"),
                "write_file",
                {"target": {"kind": "PATH", "path": "out"}, **arguments},
            )
        )
    assert not list(tmp_path.iterdir())


def test_source_write_has_existing_host_local_authorization_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace, result_source_reader=_current_port)
    request = call(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="escapes"):
        tool.execute(request)
    result = tool.execute(request, write_scope=WritePathScope.HOST_LOCAL)
    assert result.status is ToolResultState.SUCCESS
    assert (tmp_path / "outside.txt").read_bytes() == b"retained text"


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("outside", [False, True])
def test_source_write_retains_existing_file_write_permission_modes(
    tmp_path, mode, outside
):
    destination = (
        tmp_path.parent / "outside.txt" if outside else tmp_path / "result.txt"
    )
    permission = build_run_permission_snapshot(
        snapshot_id=random_id("permission"),
        requested_mode=mode,
        effective_mode=mode,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    decision = asyncio.run(
        DefaultToolDispatchAuthorizationPolicy().decide(
            ToolDispatchAuthorizationRequest(
                "write_file",
                random_id("call"),
                {
                    "target": {"kind": "PATH", "path": str(destination)},
                    "source": _current_port.target,
                },
                random_id("turn"),
                random_id("entry"),
                permission,
                tmp_path,
            )
        )
    )
    expected = (
        "DENY"
        if mode is PermissionMode.READ_ONLY
        else "ALLOW"
        if mode is PermissionMode.BYPASS_PERMISSIONS
        or (mode is PermissionMode.ACCEPT_EDITS and not outside)
        else "REQUIRE_CONFIRMATION"
    )
    assert decision.kind.value == expected


def test_large_location_metadata_cannot_write_before_physical_quote(
    tmp_path, monkeypatch
):
    path = tmp_path / ("a" * 40_000)
    monkeypatch.setattr(WriteFileTool, "_resolve_path", lambda *args, **kwargs: path)
    monkeypatch.setattr(filesystem, "_requested_leaf_path", lambda *args: path)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    writes = []
    monkeypatch.setattr(
        filesystem, "_atomic_create_bytes", lambda *args: writes.append(args)
    )
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(
        call(path)
    )
    assert json.loads(result.output)["status"] == "resource_boundary"
    assert not writes and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_existing_destination_is_never_replaced(tmp_path, kind):
    target = tmp_path / "output.txt"
    if kind == "file":
        target.write_text("original")
    elif kind == "directory":
        target.mkdir()
    else:
        other = tmp_path / "other.txt"
        if kind == "symlink":
            other.write_text("original")
        target.symlink_to(other)
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(call())
    assert json.loads(result.output)["error"] == "FILE_ALREADY_EXISTS"
    if kind == "file":
        assert target.read_text() == "original"
    elif kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_symlink()
        if kind == "symlink":
            assert target.read_text() == "original"
        else:
            assert not target.exists()


def test_same_target_concurrent_exports_one_winner(tmp_path):
    def export(content):
        port = BodyPort(content)
        return WriteFileTool(tmp_path, result_source_reader=port).execute(
            call(port=port)
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(export, (b"one", b"two")))
    assert sum(r.status is ToolResultState.SUCCESS for r in results) == 1
    assert tmp_path.joinpath("output.txt").read_bytes() in {b"one", b"two"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output.txt"]


def test_publication_ack_failure_does_not_claim_no_file(tmp_path, monkeypatch):
    actual_link = filesystem.os.link

    def failed_ack(*args, **kwargs):
        actual_link(*args, **kwargs)
        raise OSError(errno.EIO, "publication acknowledgement lost")

    monkeypatch.setattr(filesystem.os, "link", failed_ack)
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(call())
    assert json.loads(result.output)["error"] == "FILE_PUBLICATION_UNCONFIRMED"
    assert (tmp_path / "output.txt").read_bytes() == b"retained text"
    assert not list(tmp_path.glob(".pulsara-*.tmp"))


def test_final_close_failure_keeps_publication_fact_and_closes_both_fds(
    tmp_path, monkeypatch
):
    actual_open = filesystem.open_or_create_absolute_directory_nofollow
    actual_close = filesystem.os.close
    parent = []
    closed_after_failure = []

    def bind(path):
        descriptor = actual_open(path)
        parent.append(descriptor)
        return descriptor

    def close(descriptor):
        actual_close(descriptor)
        if closed_after_failure:
            closed_after_failure.append(descriptor)
        elif parent and descriptor == parent[0]:
            closed_after_failure.append(descriptor)
            raise OSError(errno.EIO, "close acknowledgement lost")

    monkeypatch.setattr(filesystem, "open_or_create_absolute_directory_nofollow", bind)
    monkeypatch.setattr(filesystem.os, "close", close)
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(call())
    assert json.loads(result.output)["error"] == "FILE_PUBLICATION_UNCONFIRMED"
    assert (tmp_path / "output.txt").read_bytes() == b"retained text"
    assert len(closed_after_failure) == 2
    assert not list(tmp_path.glob(".pulsara-*.tmp"))


@pytest.mark.parametrize("published", [False, True])
def test_io_failure_preserves_publication_fact(tmp_path, monkeypatch, published):
    actual_link = filesystem.os.link

    def link(*args, **kwargs):
        if not published:
            raise OSError(errno.ENOSPC, "disk full")
        return actual_link(*args, **kwargs)

    monkeypatch.setattr(filesystem.os, "link", link)
    if published:

        def fail_sync(fd):
            raise OSError(errno.EIO, "sync failed")

        monkeypatch.setattr(filesystem, "_fsync_directory_fd", fail_sync)
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(call())
    assert result.status is ToolResultState.ERROR
    assert json.loads(result.output)["error"] == (
        "FILE_PUBLICATION_UNCONFIRMED" if published else "FILE_CREATE_FAILED"
    )
    assert tmp_path.joinpath("output.txt").exists() is published
    assert not list(tmp_path.glob(".pulsara-*.tmp"))


@pytest.mark.parametrize("after_publish", [False, True])
def test_parent_rebinding_never_writes_substitute_or_claims_success(
    tmp_path, monkeypatch, after_publish
):
    parent = tmp_path / "parent"
    parent.mkdir()
    relocated = tmp_path / "relocated"
    other = tmp_path / "other"
    other.mkdir()
    if after_publish:
        actual_link = filesystem.os.link

        def link(*args, **kwargs):
            result = actual_link(*args, **kwargs)
            parent.rename(relocated)
            parent.symlink_to(other, target_is_directory=True)
            return result

        monkeypatch.setattr(filesystem.os, "link", link)
    else:
        actual_open = filesystem.open_or_create_absolute_directory_nofollow

        def bind(path):
            parent.rename(relocated)
            parent.symlink_to(other, target_is_directory=True)
            return actual_open(path)

        monkeypatch.setattr(
            filesystem, "open_or_create_absolute_directory_nofollow", bind
        )
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(
        call("parent/output.txt")
    )
    assert result.status is ToolResultState.ERROR
    assert not (other / "output.txt").exists()
    assert (relocated / "output.txt").exists() is after_publish
    assert json.loads(result.output)["error"] == (
        "FILE_PUBLICATION_UNCONFIRMED" if after_publish else "FILE_CREATE_FAILED"
    )
    assert not list(relocated.glob(".pulsara-*.tmp"))


def test_large_edit_freezes_complete_diff_but_native_receipt_fits_before_effect(
    tmp_path,
):
    from pulsara_agent.conversation_kernel.tool_result_display import (
        project_native_tool_result,
    )

    path = tmp_path / "edit.txt"
    path.write_text("original\n")
    read = ReadFileTool(tmp_path).execute(
        ToolCall(
            random_id("call"),
            "read_file",
            {"target": {"kind": "PATH", "path": "edit.txt"}},
        )
    )
    revision = json.loads(read.output)["content_revision"]
    request = ToolCall(
        random_id("call"),
        "edit_file",
        {
            "target": {"kind": "PATH", "path": "edit.txt"},
            "base_revision": revision,
            "operations": [{"kind": "replace_file", "content": '中\\"' * 30_000}],
        },
    )
    target = {
        "kind": "SESSION_ENTRY",
        "session_id": random_id("session"),
        "entry_id": random_id("entry"),
    }
    budget = ToolResultPageBudget(
        tool_call_id=request.id,
        result_target=target,
        accepts_provider_body=lambda body, provenance: len(body.encode()) <= 1400,
    )
    result = EditFileTool(tmp_path).execute(replace(request, page_budget=budget))
    assert result.status is ToolResultState.SUCCESS
    value = json.loads(result.output)
    assert result.output_body_candidate.text == value["diff"]
    assert len(value["diff"].encode()) > 40_000
    display, omitted = project_native_tool_result(
        public_output=result.output,
        native_output=result.native_output,
        page_complete=result.page_complete,
        required_facts=result.required_facts,
        output_available=True,
        output_unavailability_reason=None,
        page_budget=budget,
    )
    assert omitted and budget.fits(display)
    assert json.loads(display)["read"]["arguments"]["target"] == target
    assert path.read_text() == '中\\"' * 30_000


def test_edit_receipt_boundary_leaves_original_file_unchanged(tmp_path):
    path = tmp_path / "edit.txt"
    path.write_text("original")
    read = ReadFileTool(tmp_path).execute(
        ToolCall(
            random_id("call"),
            "read_file",
            {"target": {"kind": "PATH", "path": "edit.txt"}},
        )
    )
    request = ToolCall(
        random_id("call"),
        "edit_file",
        {
            "target": {"kind": "PATH", "path": "edit.txt"},
            "base_revision": json.loads(read.output)["content_revision"],
            "operations": [{"kind": "replace_file", "content": "changed"}],
        },
    )
    budget = ToolResultPageBudget(
        tool_call_id=request.id, accepts_provider_body=lambda *args: False
    )
    result = EditFileTool(tmp_path).execute(replace(request, page_budget=budget))
    assert json.loads(result.output)["status"] == "resource_boundary"
    assert path.read_text() == "original"
