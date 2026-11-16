"""Complete result export preserves file permission and atomic publication boundaries."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import errno
import pytest
from pulsara_agent.conversation_kernel.tool_outputs import project_tool_result_export
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.ports.tool_output import ToolResultView, ToolOutputContentError
from pulsara_agent.tools.builtins import filesystem
from pulsara_agent.tools.builtins.filesystem import WriteFileTool, EditFileTool
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
    def __init__(self, content="retained text"):
        self.reads = 0
        self.target = {"kind": "SESSION_ENTRY", "entry_id": random_id("entry")}
        self.view = ToolResultView(
            self.target,
            {
                "meta": {"status": "SUCCESS"},
                "content": [{"type": "text", "value": content}],
            },
        )

    def read_result(self, target):
        self.reads += 1
        if target != self.target:
            raise KeyError(target)
        return self.view


_current_port = BodyPort()


def call(path="output.txt", *, port=None, **extra):
    port = port or _current_port
    return ToolCall(
        random_id("call"),
        "write_file",
        {
            "target": {"kind": "PATH", "path": str(path)},
            "tool_result_target": port.target,
            **extra,
        },
    )


def text(result):
    return "".join(p.value for p in result.business_value.content if p.type == "text")


@pytest.mark.parametrize(
    "content",
    ["", "\ufeffA\r\n\x00end", '{"n":1e99,"n":2,"s":"中文🙂"}', "a" * 100_000],
)
def test_complete_text_export_has_exact_utf8_bytes_and_private_mode(tmp_path, content):
    port = BodyPort(content)
    result = WriteFileTool(tmp_path, result_source_reader=port).execute(
        call("nested/result.bin", port=port)
    )
    assert result.status is ToolResultState.SUCCESS
    assert (tmp_path / "nested/result.bin").read_bytes() == project_tool_result_export(
        port.view.value
    )
    assert (
        (tmp_path / "nested/result.bin").read_bytes().endswith(content.encode("utf8"))
    )
    assert (tmp_path / "nested/result.bin").stat().st_mode & 0o777 == 0o600
    assert result.business_value.public_fields["meta"]["bytes_written"] == len(
        project_tool_result_export(port.view.value)
    )
    assert port.reads == 1


@pytest.mark.parametrize(
    "failure",
    [KeyError("foreign entry"), ToolOutputContentError("complete result unavailable")],
)
def test_unavailable_or_unauthorized_source_never_creates_destination(
    tmp_path, failure
):
    class Broken:
        def read_result(self, target):
            raise failure

    result = WriteFileTool(tmp_path, result_source_reader=Broken()).execute(
        call("new/result.txt")
    )
    assert result.status is ToolResultState.ERROR
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "x", "tool_result_target": _current_port.target},
        {"tool_result_target": {"kind": "PATH", "path": "x"}},
        {"content": "x", "overwrite": True},
        {},
    ],
)
def test_write_has_exact_content_source_xor(tmp_path, arguments):
    request = ToolCall(
        random_id("call"),
        "write_file",
        {"target": {"kind": "PATH", "path": "out"}, **arguments},
    )
    if arguments == {"tool_result_target": {"kind": "PATH", "path": "x"}}:
        result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(
            request
        )
        assert result.status is ToolResultState.ERROR
    else:
        with pytest.raises(ValueError):
            WriteFileTool(tmp_path, result_source_reader=_current_port).execute(request)
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
    assert (tmp_path / "outside.txt").read_bytes() == project_tool_result_export(
        _current_port.view.value
    )


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
                    "tool_result_target": _current_port.target,
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
    assert result.status is ToolResultState.ERROR and "cannot overwrite" in text(result)
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
        results = list(pool.map(export, ("one", "two")))
    assert sum(r.status is ToolResultState.SUCCESS for r in results) == 1
    assert tmp_path.joinpath("output.txt").read_bytes() in {
        project_tool_result_export(BodyPort(x).view.value) for x in ("one", "two")
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output.txt"]


def test_publication_ack_failure_does_not_claim_no_file(tmp_path, monkeypatch):
    actual_link = filesystem.os.link

    def failed_ack(*args, **kwargs):
        actual_link(*args, **kwargs)
        raise OSError(errno.EIO, "publication acknowledgement lost")

    monkeypatch.setattr(filesystem.os, "link", failed_ack)
    result = WriteFileTool(tmp_path, result_source_reader=_current_port).execute(call())
    assert (
        result.status is ToolResultState.ERROR
        and "publication is unconfirmed" in text(result)
    )
    assert (tmp_path / "output.txt").read_bytes() == project_tool_result_export(
        _current_port.view.value
    )
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
    assert (
        result.status is ToolResultState.ERROR
        and "publication is unconfirmed" in text(result)
    )
    assert (tmp_path / "output.txt").read_bytes() == project_tool_result_export(
        _current_port.view.value
    )
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
    assert (
        "publication is unconfirmed"
        if published
        else "creation failed before publication"
    ) in text(result)
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
    assert (
        "publication is unconfirmed"
        if after_publish
        else "creation failed before publication"
    ) in text(result)
    assert not list(relocated.glob(".pulsara-*.tmp"))


def test_large_edit_freezes_the_complete_diff_after_effect(tmp_path):
    path = tmp_path / "edit.txt"
    path.write_text("original\n")
    replacement = '中\\"' * 30_000
    result = EditFileTool(tmp_path).execute(
        ToolCall(
            random_id("call"),
            "edit_file",
            {"target": {"kind": "PATH", "path": "edit.txt"}, "content": replacement},
        )
    )
    assert result.status is ToolResultState.SUCCESS
    assert path.read_text() == replacement
    assert len(text(result).encode()) > 40_000 and replacement in text(result)
